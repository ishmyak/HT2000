#!/usr/bin/env python3
"""
ht2000_reader.py — Чтение данных с датчика Silicon Labs HT2000 (CO2, температура, влажность).

Устройство подключается по USB как HID-устройство (VID:PID 10c4:82cd)
и видно в системе как /dev/hidrawN.

Данные читаются через HID Feature Report с Report ID 5
(«Status & Measurement Data», 31 байт) посредством ioctl HIDIOCGFEATURE
на hidraw-ноде. Никаких сторонних библиотек не требуется.

Формат отчёта Report ID 5 (значения little-endian):
    byte[0]   Report ID (0x05)
    byte[1]   Status (0x04 = данные готовы)
    byte[4:6] Температура, ед. = 0.01 °C      (напр. 2131 -> 21.31 °C)
    byte[10:12] Влажность, ед.  = 0.1 %RH     (напр. 300  -> 30.0 %RH)
    byte[24:26] CO2, ед. = ppm                 (напр. 690  -> 690 ppm)

Интерфейс модуля:
    HT2000Reader(dev_path='/dev/hidraw2')  — открывает устройство.
    .read()  -> HT2000Reading(co2_ppm, temp_c, rh_percent)
                читает последний отчёт и декодирует его.
    .close()
    HT2000Reading — dataclass с методом to_csv_row()/.

Для автономного запуска:
    python3 ht2000_reader.py            # прочитать один отчёт и вывести
    python3 ht2000_reader.py -r 5       # сделать 5 чтений подряд
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import sys
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# HID ioctl (см. /usr/include/linux/hidraw.h)
#   _IOC(dir, type, nr, size) = (dir << 30) | (type << 8) | (nr) | (size << 16)
#   dir = _IOC_WRITE(1) | _IOC_READ(2) = 3  для SFEATURE/GFEATURE
# ---------------------------------------------------------------------------
_IOC_WRITE = 0x1
_IOC_READ = 0x2


def _iowr(type_: str, nr: int, size: int) -> int:
    """Собрать код ioctl с направлением ВЫВОД+ВВОД (как HIDIOC*FEATURE)."""
    return (
        ((_IOC_WRITE | _IOC_READ) << 30)
        | (ord(type_) << 8)
        | (nr & 0xFF)
        | (size << 16)
    )


# HIDIOCGFEATURE — чтение feature-отчёта. Первый байт буфера — номер отчёта.
HIDIOCGFEATURE = _iowr("H", 0x07, 64)

# Отчёт с данными измерений
REPORT_ID_SENSOR = 0x05


@dataclass(frozen=True)
class HT2000Reading:
    """Декодированное показание датчика."""

    co2_ppm: int
    temp_c: float
    rh_percent: float
    status: int = 0

    def to_dict(self) -> dict:
        return {
            "co2_ppm": self.co2_ppm,
            "temp_c": round(self.temp_c, 2),
            "rh_percent": round(self.rh_percent, 1),
        }

    def to_csv_row(self) -> str:
        """Строка CSV без заголовка: co2_ppm,temp_c,rh_percent."""
        return f"{self.co2_ppm},{self.temp_c:.2f},{self.rh_percent:.1f}"


class HT2000Error(Exception):
    """Базовая ошибка работы с устройством HT2000."""


class HT2000Reader:
    """
    Читает измеренные данные с HT2000 через hidraw-ноду.

    Параметры
    ---------
    dev_path : str
        Путь к hidraw-устройству (по умолчанию /dev/hidraw2).
    """

    def __init__(self, dev_path: str = "/dev/hidraw2"):
        self.dev_path = dev_path
        try:
            self._fd = os.open(dev_path, os.O_RDWR)
        except OSError as exc:
            raise HT2000Error(
                f"Не удалось открыть {dev_path}: {exc}. "
                f"Запустите с правами root (sudo) или настройте udev-правило."
            ) from exc

    def read(self) -> HT2000Reading:
        """Читает один feature-отчёт (Report ID 5) и декодирует его."""
        buf = bytearray(64)
        buf[0] = REPORT_ID_SENSOR  # первый байт = номер отчёта
        try:
            # ioctl заполняет буфер данными отчёта от устройства
            fcntl.ioctl(self._fd, HIDIOCGFEATURE, buf)
        except OSError as exc:
            raise HT2000Error(
                f"Ошибка HID-обмена с {self.dev_path}: {exc}"
            ) from exc
        return self._decode(bytes(buf))

    @staticmethod
    def _decode(raw: bytes) -> HT2000Reading:
        """Декодирует 64-байтный raw-буфер отчёта в HT2000Reading."""
        if len(raw) < 26:
            raise HT2000Error(
                f"Слишком короткий отчёт: {len(raw)} байт (ожидалось >= 26)."
            )

        status = raw[1]

        # little-endian значения
        co2 = raw[24] | (raw[25] << 8)
        temp_raw = raw[4] | (raw[5] << 8)
        rh_raw = raw[10] | (raw[11] << 8)

        temp_c = temp_raw / 100.0        # ед. = 0.01 °C
        rh = rh_raw / 10.0               # ед. = 0.1 %RH

        return HT2000Reading(
            co2_ppm=co2, temp_c=temp_c, rh_percent=rh, status=status
        )

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass

    def __enter__(self) -> "HT2000Reader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _find_hidraw_device(required_vid: int = 0x10C4, required_pid: int = 0x82CD) -> Optional[str]:
    """
    Автоматически ищет hidraw-ноду устройства HT2000 по VID:PID в /sys/class/hidraw.
    Возвращает путь вида /dev/hidrawN либо None, если устройство не найдено.
    """
    for entry in sorted(os.listdir("/sys/class/hidraw")):
        uevent = os.path.join("/sys/class/hidraw", entry, "device/uevent")
        try:
            with open(uevent) as fh:
                text = fh.read()
        except OSError:
            continue
        # ищем строку вида HID_ID=0003:000010C4:000082CD
        for line in text.splitlines():
            if line.startswith("HID_ID="):
                try:
                    _, vid_hex, pid_hex = line.split("=")[1].split(":")
                    if int(vid_hex, 16) == required_vid and int(pid_hex, 16) == required_pid:
                        return f"/dev/{entry}"
                except ValueError:
                    continue
                break
    return None


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Чтение CO2, температуры и влажности с датчика Silicon Labs HT2000."
    )
    parser.add_argument(
        "-d", "--device",
        default=None,
        help="Путь к hidraw-устройству (по умолчанию: авто-поиск, затем /dev/hidraw2).",
    )
    parser.add_argument(
        "-r", "--repeats",
        type=int, default=1,
        help="Сколько раз прочитать показания (по умолчанию 1).",
    )
    parser.add_argument(
        "--interval",
        type=float, default=1.0,
        help="Задержка между повторами в секундах (по умолчанию 1.0).",
    )
    args = parser.parse_args()

    dev = args.device
    if dev is None:
        dev = _find_hidraw_device() or "/dev/hidraw2"

    import time

    try:
        with HT2000Reader(dev) as reader:
            for i in range(args.repeats):
                reading = reader.read()
                print(
                    f"CO2={reading.co2_ppm} ppm | "
                    f"T={reading.temp_c:.2f} C | "
                    f"RH={reading.rh_percent:.1f} %"
                )
                if i < args.repeats - 1:
                    time.sleep(args.interval)
    except HT2000Error as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())