#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HT2000 Monitor — три индикатора в системном трее (CO2, температура, влажность).

Данные берутся с CO2-монитора SLAB HT2000 (USB HID, см. ht2000_reader.py):

- Три отдельные иконки в трее: CO2, T°C и RH%, каждая со своим графиком
  (история 32 точки, обновление раз в секунду).
- Цвета берутся с панели часов, как в trayinfo: цвет1 — фон панели (заливка
  иконки), цвет2 — текст панели (акцент линий). Линия CO2 дополнительно
  красится по уровню: зелёный < 800 ppm, жёлтый 800–1200, красный > 1200.
- Раз в минуту текущие значения (дата, время, CO2, T, RH) пишутся в CSV-файл
  data/ht2000_YYYY-MM.csv в каталоге проекта.
- Контекстное меню «Выход» на каждой иконке.
- Если системный трей недоступен — сообщение в stderr и выход с кодом 1.
"""

import csv
import os
import sys
import time
from datetime import datetime

from PyQt5.QtCore import QRectF, QTimer, Qt
from PyQt5.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ht2000_reader import HT2000Error, HT2000Reader

ICON_SIZE = 64
HISTORY = 32
UPDATE_MS = 1000        # обновление иконок
LOG_INTERVAL_S = 60     # запись в файл раз в минуту
MARGIN = 4
PANEL_SAMPLE_Y = 18

OUTLINE_RADIUS = 2
FILL_ALPHA = 77

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Цветовые пороги CO2 (стандартная шкала: зелёный/жёлтый/красный).
CO2_GREEN = QColor("#00c853")
CO2_YELLOW = QColor("#ffd600")
CO2_RED = QColor("#ff2d00")
CO2_WARN_HI = 1200
CO2_WARN_LO = 800

# Зона съёма панели часов: правый край панели (часы всегда в углу).
CLOCK_ZONE_WIDTH = 140
CLOCK_ZONE_TOP = 2
CLOCK_ZONE_HEIGHT = 28
CLOCK_QUANT_SHIFT = 4
CLOCK_DIST_MIN = 70
CLOCK_WEIGHT_MIN = 15
CLOCK_BG_TOLERANCE = 40

PANEL_STRIP_H = 30
PANEL_STRIP_STEP = 8
CO2_MAX_SCALE = 3000    # шкала графика CO2, ppm


class HT2000Logger(object):
    """Пишет снимки в CSV-файл data/ht2000_YYYY-MM.csv (раз в минуту)."""

    def __init__(self, dataDir=DATA_DIR):
        self.dataDir = dataDir
        self.sPath = None
        self.oFile = None
        self.oWriter = None
        self.nLastMinute = None

    def log(self, oData, oWhen=None):
        """Пишет строку, если минута сменилась (или файл ещё не создан)."""
        if oWhen is None:
            oWhen = datetime.now()
        nMinute = oWhen.year * 12 * 31 * 24 * 60 + oWhen.month * 31 * 24 * 60 \
            + oWhen.day * 24 * 60 + oWhen.hour * 60 + oWhen.minute
        if nMinute == self.nLastMinute:
            return
        self.nLastMinute = nMinute
        self._ensure_file(oWhen)
        if self.oWriter is None:
            return
        self.oWriter.writerow([
            oWhen.strftime("%Y-%m-%d %H:%M:%S"),
            "{0:d}".format(oData.co2),
            "{0:.1f}".format(oData.temperature),
            "{0:.1f}".format(oData.humidity),
        ])
        self.oFile.flush()

    def _ensure_file(self, oWhen):
        sNewPath = os.path.join(self.dataDir,
                                "ht2000_{0:04d}-{1:02d}.csv".format(oWhen.year, oWhen.month))
        if sNewPath == self.sPath:
            return
        self.close()
        try:
            os.makedirs(self.dataDir, exist_ok=True)
            bNew = not os.path.exists(sNewPath)
            self.oFile = open(sNewPath, "a", newline="")
            self.oWriter = csv.writer(self.oFile, delimiter=";")
            if bNew:
                self.oWriter.writerow(["datetime", "co2_ppm", "temp_c", "rh_percent"])
            self.sPath = sNewPath
        except OSError as oExc:
            sys.stderr.write("Ошибка записи лога {0}: {1}\n".format(sNewPath, oExc))
            self.oFile = None
            self.oWriter = None
            self.sPath = None

    def close(self):
        if self.oFile is not None:
            try:
                self.oFile.close()
            except OSError:
                pass
            self.oFile = None
            self.oWriter = None


def gb(nBytes):
    return nBytes / (1024.0 ** 3)


def _sampleStrip(nY, oScreen):
    oPix = oScreen.grabWindow(0, 0, nY, oScreen.geometry().width(), PANEL_STRIP_H)
    if oPix.isNull():
        return None
    oImage = oPix.toImage()
    nW = oImage.width()
    nH = oImage.height()
    dWeights = {}
    for nRow in range(0, nH, PANEL_STRIP_STEP // 4):
        for nCol in range(0, nW, PANEL_STRIP_STEP):
            oColor = oImage.pixelColor(nCol, nRow)
            if not oColor.isValid():
                continue
            nRed, nGreen, nBlue = oColor.red(), oColor.green(), oColor.blue()
            if nRed > 245 and nGreen > 245 and nBlue > 245:
                continue
            oKey = (nRed >> CLOCK_QUANT_SHIFT, nGreen >> CLOCK_QUANT_SHIFT, nBlue >> CLOCK_QUANT_SHIFT)
            aStats = dWeights.get(oKey)
            if aStats is None:
                aStats = [0, 0, 0, 0]
                dWeights[oKey] = aStats
            aStats[0] += 1
            aStats[1] += nRed
            aStats[2] += nGreen
            aStats[3] += nBlue
    if not dWeights:
        return None
    aBest = max(dWeights, key=lambda oKey: dWeights[oKey][0])
    aStats = dWeights[aBest]
    return aStats[1] // aStats[0], aStats[2] // aStats[0], aStats[3] // aStats[0], aStats[0]


def _sampleClockZone(oImage):
    dWeights = {}
    nW = oImage.width()
    nH = oImage.height()
    for nY in range(nH):
        for nX in range(nW):
            oColor = oImage.pixelColor(nX, nY)
            if not oColor.isValid():
                continue
            oKey = (oColor.red() >> CLOCK_QUANT_SHIFT, oColor.green() >> CLOCK_QUANT_SHIFT, oColor.blue() >> CLOCK_QUANT_SHIFT)
            aStats = dWeights.get(oKey)
            if aStats is None:
                aStats = [0, 0, 0, 0]
                dWeights[oKey] = aStats
            aStats[0] += 1
            aStats[1] += oColor.red()
            aStats[2] += oColor.green()
            aStats[3] += oColor.blue()
    if not dWeights:
        return None
    aBgKey = max(dWeights, key=lambda oKey: dWeights[oKey][0])
    aBg = dWeights[aBgKey]
    nBgR, nBgG, nBgB = aBg[1] // aBg[0], aBg[2] // aBg[0], aBg[3] // aBg[0]
    aBest = None
    for oKey, aStats in dWeights.items():
        if aStats[0] < CLOCK_WEIGHT_MIN:
            continue
        nR, nG, nB = aStats[1] // aStats[0], aStats[2] // aStats[0], aStats[3] // aStats[0]
        dDist = ((nR - nBgR) ** 2 + (nG - nBgG) ** 2 + (nB - nBgB) ** 2) ** 0.5
        if dDist < CLOCK_DIST_MIN:
            continue
        if aBest is None or dDist > aBest[6]:
            aBest = (aStats[0], nR, nG, nB, nR - nBgR, nG - nBgG, dDist)
    if aBest is None:
        return None
    return QColor(nBgR, nBgG, nBgB), QColor(aBest[1], aBest[2], aBest[3]), aBest[0]


def detectClockColors():
    oPanel = detectPanelColor()
    oFallback = computeClockTextColor(oPanel)
    try:
        oScreen = QGuiApplication.primaryScreen()
        if oScreen is None:
            return oPanel, oFallback
        oGeometry = oScreen.geometry()
        nW = oGeometry.width()
        nH = oGeometry.height()
        aZones = []
        if nH >= CLOCK_ZONE_TOP + CLOCK_ZONE_HEIGHT:
            oPix = oScreen.grabWindow(0, nW - CLOCK_ZONE_WIDTH, CLOCK_ZONE_TOP,
                                      CLOCK_ZONE_WIDTH, CLOCK_ZONE_HEIGHT)
            if not oPix.isNull():
                aZones.append(oPix.toImage())
            oPix = oScreen.grabWindow(0, nW - CLOCK_ZONE_WIDTH,
                                      nH - CLOCK_ZONE_TOP - CLOCK_ZONE_HEIGHT,
                                      CLOCK_ZONE_WIDTH, CLOCK_ZONE_HEIGHT)
            if not oPix.isNull():
                aZones.append(oPix.toImage())
        if not aZones:
            return oPanel, oFallback
        aResults = []
        for oImage in aZones:
            oRes = _sampleClockZone(oImage)
            if oRes is None:
                continue
            oBg, oText, nMass = oRes
            nDist = abs(oBg.red() - oPanel.red()) + abs(oBg.green() - oPanel.green()) \
                + abs(oBg.blue() - oPanel.blue())
            if nDist > CLOCK_BG_TOLERANCE:
                continue
            aResults.append(oRes)
        if not aResults:
            return oPanel, oFallback
        oBg, oText, _ = max(aResults, key=lambda oEntry: oEntry[2])
        return oPanel, oText
    except Exception:
        return oPanel, oFallback


def detectPanelColor():
    fallback = QGuiApplication.palette().window().color()
    try:
        oScreen = QGuiApplication.primaryScreen()
        if oScreen is None:
            return fallback
        oGeometry = oScreen.geometry()
        if oGeometry.height() <= PANEL_STRIP_H + PANEL_SAMPLE_Y + 1:
            return fallback
        topStripe = _sampleStrip(PANEL_SAMPLE_Y, oScreen)
        if topStripe is not None:
            return QColor(topStripe[0], topStripe[1], topStripe[2])
        bottomStripe = _sampleStrip(oGeometry.height() - PANEL_STRIP_H - PANEL_SAMPLE_Y,
                                    oScreen)
        if bottomStripe is not None:
            return QColor(bottomStripe[0], bottomStripe[1], bottomStripe[2])
        return fallback
    except Exception:
        return fallback


def computeClockTextColor(oBackground):
    """Цвет текста панели часов из цвета её фона (логика trayinfo)."""
    dLuma = (0.299 * oBackground.red() + 0.587 * oBackground.green()
             + 0.114 * oBackground.blue()) / 255.0
    bLight = dLuma >= 0.5
    nHue, nSat1, nVal1, _ = oBackground.getHsv()
    if nHue < 0:
        nHue = 0
    dSat1 = nSat1 / 255.0 * 100.0
    dVal1 = nVal1 / 255.0 * 100.0
    dSat2 = min(dSat1, 35)
    dVal2 = dVal1 - 40 if bLight else dVal1 + 40
    dVal2 = max(min(dVal2, 90), 15)
    if abs(dVal2 - dVal1) < 30:
        dVal2 = (dVal1 + 30) if dVal2 >= dVal1 else (dVal1 - 30)
        dVal2 = max(min(dVal2, 97), 3)
    oText = QColor()
    oText.setHsv(nHue, int(round(dSat2 / 100.0 * 255)), int(round(dVal2 / 100.0 * 255)))
    return oText


def co2Color(nPpm):
    """Цвет линии CO2 по уровню: зелёный < 800, жёлтый 800–1200, красный > 1200."""
    if nPpm >= CO2_WARN_HI:
        return QColor(CO2_RED)
    if nPpm >= CO2_WARN_LO:
        return QColor(CO2_YELLOW)
    return QColor(CO2_GREEN)


class HT2000TrayMonitor(object):
    def __init__(self):
        self.bConnected = False
        self.oLastData = None
        self.co2History = [0.0] * HISTORY
        self.tempHistory = [0.0] * HISTORY
        self.rhHistory = [0.0] * HISTORY
        self.oReader = HT2000Reader()
        self.oLogger = HT2000Logger()

        self.pCo2Tray = QSystemTrayIcon()
        self.pTempTray = QSystemTrayIcon()
        self.pRhTray = QSystemTrayIcon()

        menu = QMenu()
        actExit = menu.addAction("Выход")
        actExit.triggered.connect(QApplication.instance().quit)
        for oTray in (self.pCo2Tray, self.pTempTray, self.pRhTray):
            oTray.setContextMenu(menu)

        self.tick()

        for oTray in (self.pCo2Tray, self.pTempTray, self.pRhTray):
            oTray.show()

        self.pTimer = QTimer()
        self.pTimer.timeout.connect(self.tick)
        self.pTimer.start(UPDATE_MS)

    def tick(self):
        oData = None
        if not self.oReader.is_open():
            try:
                self.oReader.open()
            except HT2000Error:
                pass
        if self.oReader.is_open():
            try:
                oData = self.oReader.read()
            except HT2000Error:
                self.oReader.close()
                self.oReader.path = None     # при следующем тике — поиск заново
        self.bConnected = oData is not None
        if oData is not None:
            self.oLastData = oData
        if self.oLastData is not None:
            oData = self.oLastData

        if oData is None:
            self.co2History.append(0.0)
            self.tempHistory.append(0.0)
            self.rhHistory.append(0.0)
            del self.co2History[:-HISTORY]
            del self.tempHistory[:-HISTORY]
            del self.rhHistory[:-HISTORY]
        else:
            self.co2History.append(float(oData.co2))
            self.tempHistory.append(oData.temperature)
            self.rhHistory.append(oData.humidity)
            del self.co2History[:-HISTORY]
            del self.tempHistory[:-HISTORY]
            del self.rhHistory[:-HISTORY]
            self.oLogger.log(oData)

        # Цвета берутся с панели часов (как trayinfo).
        oBg, oAccent = detectClockColors()

        sSuffix = "" if self.bConnected else " (нет связи)"
        self.pCo2Tray.setToolTip("CO2: {0:4d} ppm{1}".format(
            int(round(oData.co2)) if oData else 0, sSuffix))
        self.pTempTray.setToolTip("T: {0:4.1f} °C{1}".format(
            oData.temperature if oData else 0.0, sSuffix))
        self.pRhTray.setToolTip("RH: {0:4.1f} %{1}".format(
            oData.humidity if oData else 0.0, sSuffix))

        oCo2Color = co2Color(oData.co2) if oData else oAccent
        self.pCo2Tray.setIcon(QIcon(
            self._renderIcon(self.co2History, oBg, oCo2Color, CO2_MAX_SCALE)))
        self.pTempTray.setIcon(QIcon(
            self._renderIcon(self.tempHistory, oBg, oAccent, 100.0)))
        self.pRhTray.setIcon(QIcon(
            self._renderIcon(self.rhHistory, oBg, oAccent, 100.0)))

    def _renderIcon(self, values, oBg, oAccent, dMax):
        pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
        pixmap.fill(oBg)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(MARGIN, MARGIN, ICON_SIZE - 2 * MARGIN, ICON_SIZE - 2 * MARGIN)
        self._plot(painter, values, oAccent, rect, dMax)
        painter.end()
        return pixmap

    def _plot(self, painter, values, color, rect, dMax):
        # Линия нормируется на свою шкалу: CO2 0..3000 ppm, T/RH 0..100%.
        n = len(values)
        path = QPainterPath()
        for i, value in enumerate(values):
            x = rect.left() + i * (rect.width() / (n - 1))
            y = rect.bottom() - min(value, dMax) / dMax * rect.height()
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        areaPath = QPainterPath(path)
        areaPath.lineTo(rect.right(), rect.bottom())
        areaPath.lineTo(rect.left(), rect.bottom())
        areaPath.closeSubpath()
        oFill = QColor(color)
        oFill.setAlpha(FILL_ALPHA)
        painter.fillPath(areaPath, oFill)

        painter.setPen(QPen(color, 1))
        painter.drawPath(path)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, 1))
        painter.drawRoundedRect(rect, OUTLINE_RADIUS, OUTLINE_RADIUS)


def main():
    app = QApplication(sys.argv)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        sys.stderr.write("Ошибка: системный трей недоступен (QSystemTrayIcon.isSystemTrayAvailable() == False)\n")
        return 1

    # Три независимых монитора: каждый со своей иконкой и историей.
    # Проще, чем один класс с тремя слогами: у CO2 своя шкала и цвета.
    app._ht2000 = HT2000TrayMonitor()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())