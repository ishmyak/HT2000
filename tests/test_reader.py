#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты HT2000 Reader: разбор ответа устройства и поиск устройства."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ht2000_reader import HT2000Error, HT2000Reader

# Живой снимок устройства (запись probe-скрипта, 32 байта):
# 05 00 ff ff 17 08 cd 02 91 01 2c 01 90 ... 02 cb ...
LIVE_SNAPSHOT = bytes.fromhex(
    "05 00 ff ff 17 08 cd 02 91 01 2c 01 90 03 20 00 64 03 b6 b0 03 00 fd 02 02 cb 00 00 07 d0 05 a7")


def make_snapshot(temp_raw=0x0291, hum_raw=0x012c, co2_raw=0x02cb, length=32):
    """Собирает валидный ответ устройства с заданными значениями."""
    data = bytearray([0x05] + [0x00] * (length - 1))
    data[7:9] = temp_raw.to_bytes(2, "big")
    data[9:11] = hum_raw.to_bytes(2, "big")
    data[24:26] = co2_raw.to_bytes(2, "big")
    return bytes(data)


class TestParseBuffer(unittest.TestCase):
    def test_live_snapshot(self):
        oData = HT2000Reader.parse_buffer(LIVE_SNAPSHOT)
        self.assertAlmostEqual(oData.temperature, 25.7, places=5)
        self.assertAlmostEqual(oData.humidity, 30.0, places=5)
        self.assertEqual(oData.co2, 715)

    def test_typical_values(self):
        # temp_raw 0x0190 = 400 -> (400-400)/10 = 0.0 °C; hum 0x01F4 = 50.0%;
        # co2 0x03E8 = 1000 ppm.
        oData = HT2000Reader.parse_buffer(make_snapshot(0x0190, 0x01F4, 0x03E8))
        self.assertAlmostEqual(oData.temperature, 0.0, places=5)
        self.assertAlmostEqual(oData.humidity, 50.0, places=5)
        self.assertEqual(oData.co2, 1000)

    def test_negative_temperature(self):
        # temp_raw 0x00FA = 250 -> (250-400)/10 = -15.0 °C.
        oData = HT2000Reader.parse_buffer(make_snapshot(0x00FA, 0x01F4, 0x03E8))
        self.assertAlmostEqual(oData.temperature, -15.0, places=5)

    def test_high_co2(self):
        oData = HT2000Reader.parse_buffer(make_snapshot(0x01FA, 0x00C8, 0x1388))
        self.assertEqual(oData.co2, 5000)
        self.assertAlmostEqual(oData.humidity, 20.0, places=5)

    def test_short_response_raises(self):
        with self.assertRaises(HT2000Error):
            HT2000Reader.parse_buffer(b"\x05" * 20)

    def test_empty_response_raises(self):
        with self.assertRaises(HT2000Error):
            HT2000Reader.parse_buffer(b"")


class TestFindDevice(unittest.TestCase):
    def test_no_such_device_dir(self):
        # В /dev по определению есть hidraw-девайсы, но HT2000 может быть
        # не подключён. Не должно быть исключений, только None или путь.
        sPath = HT2000Reader.find_device()
        if sPath is not None:
            self.assertTrue(sPath.startswith("/dev/hidraw"))


if __name__ == "__main__":
    unittest.main()