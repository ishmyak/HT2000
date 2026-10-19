#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HT2000 Web — мини веб-сервер для отображения последних измерений.

Стандартная библиотека (http.server, threading), без внешних зависимостей:

- `/?range=hour|day` — HTML-страница: текущие значения CO2, температуры и
  влажности (автообновление каждые 5 секунд, meta refresh) и ниже —
  графики, построенные из CSV-файла данных (за последний час или сутки);
- `/data` — текущие данные в JSON (для внешнего мониторинга).

Графики рисуются на сервере как SVG из файла данных (data/ht2000_*.csv),
который пишет приложение. Часовой график — последние 60 минут, суточный —
последние 24 часа (при смене даты читается и вчерашний, и сегодняшний файлы).

Сервер стартует в отдельном фоновом потоке (daemon), слушает все интерфейсы
(0.0.0.0) на первом свободном порту из диапазона 8080..8089. Снимок текущих
значений запрашивается у владельца через колбек-функцию, поэтому модуль
независим от GUI. Модуль покрыт unittest.
"""

import csv
import glob
import json
import os
import socket
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

START_PORT = 8080
MAX_PORT_ATTEMPTS = 10
REFRESH_SECONDS = 5

# Слушаем все интерфейсы, чтобы страница была доступна по локальной сети.
BIND_ADDRESS = "0.0.0.0"

RANGES = ("hour", "day")        # доступные диапазоны графиков
RANGE_HOURS = {"hour": 1, "day": 24}

CO2_WARN_LO = 800
CO2_WARN_HI = 1200
CO2_COLOR_GREEN = "#00c853"
CO2_COLOR_YELLOW = "#ffd600"
CO2_COLOR_RED = "#ff2d00"

GRAPH_CO2_COLOR = "#1565c0"
GRAPH_TEMP_COLOR = "#ef6c00"
GRAPH_RH_COLOR = "#2e7d32"

# SVG-геометрия графиков.
SVG_WIDTH = 640
SVG_HEIGHT = 170
SVG_LEFT = 52            # поле слева под метки значений
SVG_RIGHT = 10
SVG_TOP = 8
SVG_BOTTOM = 28          # поле снизу под метки времени

PAGE_TEMPLATE = u"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>HT2000</title>
<meta http-equiv="refresh" content="{refresh}">
<style>
 body{{font-family:Verdana,sans-serif;background:#f5f5f5;margin:0;padding:24px;text-align:center}}
 h1{{font-size:15px;color:#555;margin:0 0 14px}}
 table{{margin:0 auto;border-collapse:collapse;background:#fff;box-shadow:0 1px 6px rgba(0,0,0,.15)}}
 td,th{{padding:7px 18px;font-size:15px}}
 th{{color:#888;font-weight:normal;text-align:right}}
 td{{min-width:120px}}
 .co2_g{{color:{green};font-weight:bold}}
 .co2_y{{color:{yellow};font-weight:bold}}
 .co2_r{{color:{red};font-weight:bold}}
 .status{{color:#aaa;margin-top:12px;font-size:12px}}
 .range{{margin:18px auto 6px;font-size:14px}}
 .range a{{color:#1565c0;text-decoration:none;padding:2px 10px;border-radius:10px}}
 .range a.active{{background:#1565c0;color:#fff;font-weight:bold}}
 .sep{{color:#bbb;padding:0 4px}}
 .graph{{background:#fff;border-radius:6px;box-shadow:0 1px 6px rgba(0,0,0,.12);margin:12px auto;padding:10px 12px 4px;max-width:{svgw}px;text-align:left}}
 .graph h2{{font-size:13px;color:#666;margin:0 0 4px;font-weight:normal}}
 .graph svg{{width:100%;height:auto;display:block}}
 .graph text{{font-family:Verdana,sans-serif;font-size:10px;fill:#999}}
 .graph .empty{{color:#bbb;font-size:13px;padding:30px 0;text-align:center}}
</style>
</head>
<body>
<h1>HT2000 · качество воздуха</h1>
<table>
<tr><th>CO₂</th><td class="{co2class}">{co2}</td></tr>
<tr><th>Температура</th><td>{temp}</td></tr>
<tr><th>Влажность</th><td>{hum}</td></tr>
</table>
<div class="status">{status}</div>
<div class="range">
 <a href="/?range=hour" class="{cls_hour}">часовой</a><span class="sep">·</span>
 <a href="/?range=day" class="{cls_day}">суточный</a>
</div>
<div class="graphs">
{graphs}
</div>
</body>
</html>
"""

GRAPH_TEMPLATE = u"""<div class="graph">
<h2>{title}</h2>
{body}
</div>"""


def co2_css_class(nPpm):
    """CSS-класс цвета CO2 по уровню (зелёный/жёлтый/красный)."""
    if nPpm >= CO2_WARN_HI:
        return "co2_r"
    if nPpm >= CO2_WARN_LO:
        return "co2_y"
    return "co2_g"


# -- Чтение файла данных ----------------------------------------------------


def read_recent(sDataDir, nHoursBack):
    """Читает CSV-файлы data/ht2000_*.csv за последние nHoursBack часов.

    Возвращает список кортежей (datetime, co2, temp, rh), отсортированный
    по времени. Битые строки и недоступные файлы пропускаются."""
    oNow = datetime.now()
    oCutoff = oNow - timedelta(hours=nHoursBack)
    aRows = []
    for sPath in sorted(glob.glob(os.path.join(sDataDir, "ht2000_*.csv"))):
        try:
            with open(sPath, newline="") as oFile:
                for aLine in csv.reader(oFile, delimiter=";"):
                    if len(aLine) < 4:
                        continue
                    try:
                        oDt = datetime.strptime(aLine[0], "%Y-%m-%d %H:%M:%S")
                        dCo2 = float(aLine[1])
                        dTemp = float(aLine[2])
                        dRh = float(aLine[3])
                    except ValueError:
                        continue
                    if oCutoff <= oDt <= oNow + timedelta(minutes=2):
                        aRows.append((oDt, dCo2, dTemp, dRh))
        except OSError:
            continue
    aRows.sort(key=lambda oRow: oRow[0])
    return aRows


# -- SVG-графики ------------------------------------------------------------


def _format_time(oDt, sRange):
    """Метка времени на оси: часы для 'hour', с датой для 'day'."""
    if sRange == "day":
        return oDt.strftime("%d.%m %H:%M")
    return oDt.strftime("%H:%M")


def render_graph(sTitle, aRows, nValueIndex, sColor, sValueFormat, sRange):
    """SVG-график одной величины из строк read_recent.

    aRows — список (datetime, co2, temp, rh); nValueIndex — индекс величины;
    sValueFormat — fmt для меток значений (например '.0f' для CO2)."""
    if len(aRows) < 2:
        return GRAPH_TEMPLATE.format(
            title=sTitle, body='<div class="empty">нет данных за период</div>')
    aVals = [oRow[nValueIndex] for oRow in aRows]
    dMin = min(aVals)
    dMax = max(aVals)
    if dMax - dMin < 1e-9:
        dMax = dMin + 1.0
    dPad = (dMax - dMin) * 0.08
    dMin -= dPad
    dMax += dPad

    oT0 = aRows[0][0]
    oT1 = aRows[-1][0]
    dSpan = (oT1 - oT0).total_seconds() or 1.0
    dX0 = SVG_LEFT
    dX1 = SVG_WIDTH - SVG_RIGHT
    dY0 = SVG_TOP
    dY1 = SVG_HEIGHT - SVG_BOTTOM

    def fX(oDt):
        return dX0 + (oDt - oT0).total_seconds() / dSpan * (dX1 - dX0)

    def fY(dV):
        return dY1 - (dV - dMin) / (dMax - dMin) * (dY1 - dY0)

    aPts = ["{0:.1f},{1:.1f}".format(fX(oRow[0]), fY(oRow[nValueIndex])) for oRow in aRows]
    sLine = "<polyline fill=\"none\" stroke=\"{0}\" stroke-width=\"1.5\" " \
            "points=\"{1}\"/>".format(sColor, " ".join(aPts))

    # Метки времени (5 шт) и значений (3 шт).
    sLabels = ""
    for nIndex in range(5):
        oDt = oT0 + (oT1 - oT0) * nIndex / 4.0
        sLabels += '<text x="{0:.1f}" y="{1}" text-anchor="middle">{2}</text>'.format(
            fX(oDt), SVG_HEIGHT - SVG_BOTTOM + 16, _format_time(oDt, sRange))
    for dV in (dMax, (dMin + dMax) / 2.0, dMin):
        sLabels += '<text x="{0}" y="{1:.1f}" text-anchor="end">{2}</text>'.format(
            dX0 - 6, fY(dV) + 3.5, ("{0:" + sValueFormat + "}").format(dV))

    sTitle += " · {0} — {1}".format(_format_time(oT0, sRange), _format_time(oT1, sRange))
    sSvg = ('<svg viewBox="0 0 {0} {1}" preserveAspectRatio="xMidYMid meet" '
            'xmlns="http://www.w3.org/2000/svg">{2}{3}</svg>').format(
        SVG_WIDTH, SVG_HEIGHT, sLine, sLabels)
    return GRAPH_TEMPLATE.format(title=sTitle, body=sSvg)


def render_graphs(aRows, sRange):
    """Три SVG-графика (CO2, температура, влажность) для страницы."""
    sCo2 = render_graph("CO₂, ppm", aRows, 1, GRAPH_CO2_COLOR, ".0f", sRange)
    sTemp = render_graph("Температура, °C", aRows, 2, GRAPH_TEMP_COLOR, ".1f", sRange)
    sRh = render_graph("Влажность, %", aRows, 3, GRAPH_RH_COLOR, ".1f", sRange)
    return sCo2 + sTemp + sRh


# -- Страница ---------------------------------------------------------------


def format_page(oSnapshot=None, sRange="hour", sDataDir=None):
    """Собирает HTML-страницу.

    Параметры:
        oSnapshot — dict с ключами co2/temperature/humidity/connected/time
            (как отдаёт колбек владельца) или None;
        sRange — 'hour' | 'day' — период графиков;
        sDataDir — каталог CSV-файлов данных (None — графики пустые).
    Возвращает bytes (UTF-8)."""
    if sRange not in RANGES:
        sRange = "hour"
    if oSnapshot is None:
        sCo2 = "—"
        sClass = "co2_g"
        sTemp = "—"
        sHum = "—"
        sStatus = "нет связи с устройством"
    else:
        nCo2 = oSnapshot["co2"]
        sCo2 = "{0} ppm".format(nCo2)
        sClass = co2_css_class(nCo2)
        sTemp = "{0:.1f} °C".format(oSnapshot["temperature"])
        sHum = "{0:.1f} %".format(oSnapshot["humidity"])
        sStatus = "обновлено: {0}".format(oSnapshot.get("time", ""))

    aRows = []
    if sDataDir:
        aRows = read_recent(sDataDir, RANGE_HOURS[sRange])
    sGraphs = render_graphs(aRows, sRange) if aRows else _empty_graphs()

    return PAGE_TEMPLATE.format(
        refresh=REFRESH_SECONDS,
        green=CO2_COLOR_GREEN,
        yellow=CO2_COLOR_YELLOW,
        red=CO2_COLOR_RED,
        co2class=sClass,
        co2=sCo2,
        temp=sTemp,
        hum=sHum,
        status=sStatus,
        cls_hour="active" if sRange == "hour" else "",
        cls_day="active" if sRange == "day" else "",
        graphs=sGraphs,
        svgw=SVG_WIDTH,
    ).encode("utf-8")


def _empty_graphs():
    aEmpty = ""
    for sTitle in ("CO₂, ppm", "Температура, °C", "Влажность, %"):
        aEmpty += GRAPH_TEMPLATE.format(
            title=sTitle, body='<div class="empty">нет данных за период</div>')
    return aEmpty


def format_json(oSnapshot):
    """Снимок в JSON (bytes)."""
    return json.dumps(oSnapshot if oSnapshot is not None
                      else {"connected": False}).encode("utf-8")


# -- Сеть -------------------------------------------------------------------


def local_ip_addresses():
    """IP-адреса машины, доступные из локальной сети (не 127.0.0.x).

    Основной адрес берётся как адрес исходящего интерфейса от UDP-сокета,
    затем добавляются адреса хоста по имени. Всегда содержит минимум
    127.0.0.1 как запасной вариант."""
    aAddrs = []
    oSock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        oSock.connect(("8.8.8.8", 80))
        sIp = oSock.getsockname()[0]
        if sIp and not sIp.startswith("127."):
            aAddrs.append(sIp)
    except OSError:
        pass
    finally:
        oSock.close()
    try:
        for oInfo in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            sIp = oInfo[4][0]
            if sIp.startswith("127.") or sIp in aAddrs:
                continue
            aAddrs.append(sIp)
    except (socket.gaierror, OSError):
        pass
    return aAddrs or ["127.0.0.1"]


class HT2000WebError(Exception):
    """Ошибка запуска веб-сервера (все порты заняты и т.п.)."""


class HT2000WebServer(object):
    """HTTP-сервер последних измерений HT2000.

    Параметры:
        fnSnapshot — колбек без аргументов, возвращает dict с ключами
            co2/temperature/humidity/connected/time или None, если
            данных ещё не было;
        sDataDir — каталог CSV-файлов данных (для графиков на странице);
        nStartPort — первый порт для попытки захвата;
        nMaxAttempts — сколько портов перепробовать, прежде чем сдаться.

    Пример:
        o = HT2000WebServer(lambda: {...}, sDataDir="/data")
        o.start()          # -> http://127.0.0.1:8080/
        o.stop()
    """

    def __init__(self, fnSnapshot, sDataDir=None,
                 nStartPort=START_PORT, nMaxAttempts=MAX_PORT_ATTEMPTS):
        self.fnSnapshot = fnSnapshot
        self.sDataDir = sDataDir
        self.nStartPort = nStartPort
        self.nMaxAttempts = nMaxAttempts
        self.nPort = None
        self.httpd = None
        self.oThread = None

    # -- Жизненный цикл -----------------------------------------------------

    def start(self):
        """Поднимает сервер на первом свободном порту.

        Бросает HT2000WebError, если ни один порт из диапазона не удалось
        захватить. Повторный вызов без stop() — ошибка."""
        if self.httpd is not None:
            raise HT2000WebError("сервер уже запущен")
        for nAttempt in range(self.nMaxAttempts):
            nPort = self.nStartPort + nAttempt
            try:
                self.httpd = _HT2000Server((BIND_ADDRESS, nPort), _HT2000Handler,
                                           self.fnSnapshot, self.sDataDir)
            except OSError:
                continue    # порт занят — пробуем следующий
            self.nPort = nPort
            break
        if self.httpd is None:
            raise HT2000WebError(
                "не удалось занять порты {0}..{1}".format(
                    self.nStartPort, self.nStartPort + self.nMaxAttempts - 1))
        self.oThread = threading.Thread(target=self.httpd.serve_forever, name="ht2000-web")
        self.oThread.daemon = True
        self.oThread.start()
        return True

    def stop(self):
        """Останавливает сервер. Идемпотентна: повторный вызов безопасен."""
        if self.httpd is None:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        self.nPort = None
        self.oThread = None

    def is_running(self):
        return self.httpd is not None


class _HT2000Server(ThreadingHTTPServer):
    """ThreadingHTTPServer, несущий колбек снимка и каталог данных."""
    daemon_threads = True

    def __init__(self, oAddress, oHandlerClass, fnSnapshot, sDataDir):
        self.fnSnapshot = fnSnapshot
        self.sDataDir = sDataDir
        ThreadingHTTPServer.__init__(self, oAddress, oHandlerClass)


class _HT2000Handler(BaseHTTPRequestHandler):
    """Обработчик GET: '/' — страница с графиками, '/data' — JSON."""

    def do_GET(self):
        oSnapshot = self.server.fnSnapshot()
        oUrl = urlsplit(self.path)
        if oUrl.path in ("/", "/index.html"):
            sRange = "hour"
            aRanges = parse_qs(oUrl.query).get("range")
            if aRanges and aRanges[0] in RANGES:
                sRange = aRanges[0]
            self._send(200, "text/html; charset=utf-8",
                       format_page(oSnapshot, sRange=sRange,
                                   sDataDir=self.server.sDataDir))
        elif oUrl.path == "/data":
            self._send(200, "application/json; charset=utf-8", format_json(oSnapshot))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def log_message(self, sFormat, *aArgs):
        sys.stderr.write("[ht2000-web] {0} {1}\n".format(
            self.address_string(), sFormat % aArgs))

    def _send(self, nCode, sContentType, oBody):
        self.send_response(nCode)
        self.send_header("Content-Type", sContentType)
        self.send_header("Content-Length", str(len(oBody)))
        self.end_headers()
        self.wfile.write(oBody)