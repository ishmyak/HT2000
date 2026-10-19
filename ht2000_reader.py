#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HT2000 Reader — чтение измерений с CO2-монитора SLAB HT2000.

Устройство: USB HID, VID 0x10c4, PID 0x82cd (hidraw). Общение построено на
HID feature reports (ioctl HIDIOCSFEATURE / HIDIOCGFEATURE), а не на обычных
write/read — поэтому прямой записи в hidraw устройство не отвечает.

Протокол (реверс-инжиниринг, https://github.com/tomvanbraeckel/slab_ht2000):
1. Set Feature, report 0x05, данные [0xff, 0xff, 0xff] — запрос свежего снимка;
2. Get Feature, report 0x05 — ответ длиной >= 30 байт:
       байты [7:9]   big-endian  — температура, (v - 400) / 10  °C;
       байты [9:11]  big-endian  — влажность,   v / 10          %;
       байты [24:26] big-endian  — CO2,         v               ppm.

Модуль не зависит от GUI и тестируется без устройства (unittest).
"""

import ctypes
import glob
import os
import re

from collections import namedtuple

VID = 0x10c4
PID = 0x82cd

# Linux ioctl-коды HIDIOCSFEATURE / HIDIOCGFEATURE (собраны вручную, чтобы
# не зависеть от версии linux-headers). Директория 'H', nr 0x06/0x07.
_IOC_WRITE = 1
_IOC_READ = 2
_IOC_DIRSHIFT = 30
_IOC_TYPESHIFT = 8
_IOC_NRSHIFT = 0
_IOC_SIZESHIFT = 16

_HIDIOCSFEATURE_LEN4 = ((_IOC_WRITE | _IOC_READ) << _IOC_DIRSHIFT) | \
    (ord('H') << _IOC_TYPESHIFT) | (0x06 << _IOC_NRSHIFT) | (4 << _IOC_SIZESHIFT)
_HIDIOCGFEATURE_LEN256 = ((_IOC_WRITE | _IOC_READ) << _IOC_DIRSHIFT) | \
    (ord('H') << _IOC_TYPESHIFT) | (0x07 << _IOC_NRSHIFT) | (256 << _IOC_SIZESHIFT)

FEATURE_REPORT_ID = 0x05

# Минимальная длина ответа Get Feature, при которой есть синхронизированные
# данные (в референсном C-клиенте — 30 байт).
MIN_RESPONSE_LEN = 30

Ht2000Data = namedtuple("Ht2000Data", "temperature humidity co2")


class HT2000Error(Exception):
    """Ошибка работы с устройством (нет связи, короткий ответ и т.п.)."""


class HT2000Reader(object):
    """Читатель HT2000: открытие hidraw-устройства, запрос и разбор снимка.

    Потокобезопасен для использования из GUI-таймера (каждый read —
    один set + один get ioctl, без промежуточного состояния).
    """

    def __init__(self, path=None):
        self.path = path
        self._fd = None

    # -- Поиск устройства ---------------------------------------------------

    @staticmethod
    def find_device():
        """Ищет hidraw-устройство Silicon Labs HT2000 в /dev.

        Сопоставление по uevent (HID_ID=0003:000010C4:000082CD), а не по
        номеру устройства: hidraw2 сегодня, hidrawN завтра. Возвращает путь
        или None, если устройство не подключено."""
        for oPath in glob.glob("/dev/hidraw*"):
            sUevent = os.path.join("/sys/class/hidraw",
                                   os.path.basename(oPath), "device", "uevent")
            try:
                with open(sUevent, "rt") as oFile:
                    sText = oFile.read()
            except (OSError, IOError):
                continue
            oMatch = re.search(r"HID_ID=0003:(?P<vid>[0-9A-Fa-f]{8}):(?P<pid>[0-9A-Fa-f]{8})",
                               sText)
            if oMatch is None:
                continue
            if int(oMatch.group("vid"), 16) == VID and int(oMatch.group("pid"), 16) == PID:
                return oPath
        return None

    # -- Открытие/закрытие --------------------------------------------------

    def open(self):
        """Открывает устройство O_RDWR|O_NONBLOCK.

        Если путь не задан — устройство ищется автоматически. При неудаче
        бросает HT2000Error."""
        if self.path is None:
            self.path = self.find_device()
        if self.path is None:
            raise HT2000Error("устройство HT2000 не найдено в /dev/hidraw*")
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as oExc:
            self._fd = None
            raise HT2000Error("не удалось открыть {0}: {1}".format(self.path, oExc))
        return self

    def is_open(self):
        return self._fd is not None

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    # -- Чтение -------------------------------------------------------------

    def read(self):
        """Запрашивает и возвращает свежий снимок Ht2000Data.

        Бросает HT2000Error при любой ошибке (устройство отключено, ответ
        короче MIN_RESPONSE_LEN, ioctl-ошибка)."""
        return self.parse_buffer(self._query())

    def _query(self):
        if self._fd is None:
            raise HT2000Error("устройство не открыто")
        try:
            import fcntl
            fcntl.ioctl(self._fd, _HIDIOCSFEATURE_LEN4,
                        self._feature_buffer(b"\xff\xff\xff"))
            oResponse = self._feature_buffer(b"")
            nLen = fcntl.ioctl(self._fd, _HIDIOCGFEATURE_LEN256, oResponse)
        except OSError as oExc:
            raise HT2000Error("ошибка ioctl: {0}".format(oExc))
        return oResponse.raw[:nLen]

    @staticmethod
    def _feature_buffer(sPayload):
        """Создаёт ctypes-буфер feature report: [0] = id, дальше payload."""
        oBuffer = ctypes.create_string_buffer(256)
        oBuffer[0] = FEATURE_REPORT_ID
        for nIndex, nByte in enumerate(sPayload):
            oBuffer[nIndex + 1] = nByte
        return oBuffer

    # -- Разбор ответа (чистая функция, покрыта тестами) --------------------

    @staticmethod
    def parse_buffer(oData):
        """Разбирает ответ Get Feature в Ht2000Data(t, rh, co2).

        Бросает HT2000Error, если ответ короче MIN_RESPONSE_LEN."""
        if len(oData) < MIN_RESPONSE_LEN:
            raise HT2000Error(
                "короткий ответ устройства: {0} байт (нужно >= {1})".format(
                    len(oData), MIN_RESPONSE_LEN))
        nTempRaw = (oData[7] << 8) | oData[8]
        nHumRaw = (oData[9] << 8) | oData[10]
        nCo2Raw = (oData[24] << 8) | oData[25]
        return Ht2000Data(
            temperature=(nTempRaw - 400) / 10.0,
            humidity=nHumRaw / 10.0,
            co2=nCo2Raw,
        )

    # -- Контекстный менеджер -----------------------------------------------

    def __enter__(self):
        return self.open()

    def __exit__(self, oType, oValue, oTraceback):
        self.close()
        return False