#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Минутные медианы успешных измерений HT2000 и запись дневных CSV."""

import csv
import os
import sys
from datetime import datetime
from statistics import median

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


class HT2000Logger(object):
    """Накапливает измерения и сохраняет медиану завершившейся минуты."""

    def __init__(self, dataDir=DATA_DIR):
        """Создаёт накопитель без открытия файла до первой записи."""
        self.dataDir = dataDir
        self.sPath = None
        self.oFile = None
        self.oWriter = None
        self.oMinute = None
        self.aSamples = []

    def log(self, oData, oWhen=None):
        """Добавляет успешное чтение; None только завершает прошлую минуту."""
        if oWhen is None:
            oWhen = datetime.now()
        oMinute = oWhen.replace(second=0, microsecond=0)
        if self.oMinute is not None and oMinute < self.oMinute:
            return
        if oMinute != self.oMinute:
            if not self._write_pending():
                return
            self.oMinute = oMinute
        if oData is not None:
            self.aSamples.append((oData.co2, oData.temperature, oData.humidity))

    def _write_pending(self):
        """Пишет одну медиану по каждому параметру; пустые минуты пропускает."""
        if not self.aSamples:
            return True
        if not self._ensure_file(self.oMinute):
            return False
        aMedians = [median(aValues) for aValues in zip(*self.aSamples)]
        try:
            self.oWriter.writerow([
                self.oMinute.strftime("%Y-%m-%d %H:%M:%S"),
                "{0:d}".format(int(aMedians[0] + 0.5)),
                "{0:.1f}".format(aMedians[1]),
                "{0:.1f}".format(aMedians[2]),
            ])
            self.oFile.flush()
        except OSError as oExc:
            sys.stderr.write("Ошибка записи лога {0}: {1}\n".format(self.sPath, oExc))
            return False
        self.aSamples = []
        return True

    def _ensure_file(self, oWhen):
        """Открывает дневной файл, сохраняя прежние столбцы и разделитель."""
        sNewPath = os.path.join(
            self.dataDir,
            "ht2000_{0:04d}-{1:02d}-{2:02d}.csv".format(
                oWhen.year, oWhen.month, oWhen.day))
        if sNewPath == self.sPath:
            return True
        self._close_file()
        try:
            os.makedirs(self.dataDir, exist_ok=True)
            bNew = not os.path.exists(sNewPath) or os.path.getsize(sNewPath) == 0
            self.oFile = open(sNewPath, "a", newline="")
            self.oWriter = csv.writer(self.oFile, delimiter=";")
            if bNew:
                self.oWriter.writerow(["datetime", "co2_ppm", "temp_c", "rh_percent"])
            self.sPath = sNewPath
            return True
        except OSError as oExc:
            sys.stderr.write("Ошибка записи лога {0}: {1}\n".format(sNewPath, oExc))
            self._close_file()
            return False

    def _close_file(self):
        """Закрывает файл без повторного сброса накопленных измерений."""
        if self.oFile is not None:
            try:
                self.oFile.close()
            except OSError:
                pass
        self.oFile = None
        self.oWriter = None
        self.sPath = None

    def close(self):
        """Сохраняет неполную последнюю минуту и закрывает файл."""
        self._write_pending()
        self._close_file()
