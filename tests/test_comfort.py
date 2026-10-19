# -*- coding: utf-8 -*-
"""Тесты норм комфорта (comfort.py): сезон, зоны, статусы, тултип, конфиг."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

import comfort
from comfort import (comfort_status, comfort_tooltip, configure,
                     configure_from_file, humidity_ranges, season,
                     temperature_ranges)

COLD = datetime(2026, 1, 15)
WARM = datetime(2026, 7, 15)


class TestSeason(unittest.TestCase):
    def test_warm_months(self):
        for oDt in (datetime(2026, 5, 1), datetime(2026, 9, 30),
                    datetime(2026, 7, 15)):
            self.assertEqual(season(oDt), "warm")

    def test_cold_months(self):
        for oDt in (datetime(2026, 1, 1), datetime(2026, 10, 1),
                    datetime(2026, 4, 30), datetime(2026, 12, 31)):
            self.assertEqual(season(oDt), "cold")


class TestRanges(unittest.TestCase):
    def tearDown(self):
        configure(None)

    def test_temperature_ranges_user_values(self):
        # Значения заданы пользователем.
        self.assertEqual(temperature_ranges(COLD),
                         ((22.0, 24.0), (20.0, 25.0)))
        self.assertEqual(temperature_ranges(WARM),
                         ((23.0, 25.0), (21.0, 28.0)))

    def test_humidity_range_always(self):
        for oDt in (COLD, WARM):
            self.assertEqual(humidity_ranges(oDt), ((40.0, 60.0), (40.0, 60.0)))


class TestStatus(unittest.TestCase):
    def tearDown(self):
        configure(None)

    def test_temperature_status_ok(self):
        for dT in (22.0, 23.0, 24.0):          # холодный оптимум 22–24
            self.assertEqual(comfort_status(COLD, dT, 50.0)["temperature"], "ok")
        for dT in (23.0, 24.0, 25.0):          # тёплый оптимум 23–25
            self.assertEqual(comfort_status(WARM, dT, 50.0)["temperature"], "ok")

    def test_temperature_warn(self):
        # Вне оптимума, но в допустимой зоне → warn (⚠ жёлтый).
        self.assertEqual(comfort_status(COLD, 20.5, 50.0)["temperature"], "warn_low")
        self.assertEqual(comfort_status(COLD, 24.5, 50.0)["temperature"], "warn_high")
        self.assertEqual(comfort_status(WARM, 21.5, 50.0)["temperature"], "warn_low")
        self.assertEqual(comfort_status(WARM, 27.5, 50.0)["temperature"], "warn_high")

    def test_temperature_bad(self):
        # Вне допустимой зоны → bad (красный).
        self.assertEqual(comfort_status(COLD, 19.9, 50.0)["temperature"], "bad_low")
        self.assertEqual(comfort_status(COLD, 25.1, 50.0)["temperature"], "bad_high")
        self.assertEqual(comfort_status(WARM, 20.9, 50.0)["temperature"], "bad_low")
        self.assertEqual(comfort_status(WARM, 28.1, 50.0)["temperature"], "bad_high")

    def test_humidity_status(self):
        self.assertEqual(comfort_status(COLD, 23.0, 50.0)["humidity"], "ok")
        self.assertEqual(comfort_status(COLD, 23.0, 39.9)["humidity"], "low")
        self.assertEqual(comfort_status(COLD, 23.0, 60.1)["humidity"], "high")


class TestTooltip(unittest.TestCase):
    def tearDown(self):
        configure(None)

    def test_tooltip_ok(self):
        sTip = comfort_tooltip(23.0, 50.0, COLD)
        self.assertIn("T: 23.0 °C ✓ (оптимум 22–24)", sTip)
        self.assertIn("RH: 50.0 % ✓ (норма 40–60)", sTip)

    def test_tooltip_warn_and_bad(self):
        sTip = comfort_tooltip(24.5, 50.0, COLD)
        self.assertIn("T: 24.5 °C ⚠ (выше оптимальной 22–24)", sTip)
        sTip = comfort_tooltip(25.1, 50.0, COLD)
        self.assertIn("T: 25.1 °C ⚠ (выше допустимой 25)", sTip)
        sTip = comfort_tooltip(19.9, 50.0, COLD)
        self.assertIn("T: 19.9 °C ⚠ (ниже допустимой 20)", sTip)

    def test_tooltip_default_now(self):
        # Без явного момента — сезон от текущей даты, без исключений.
        sTip = comfort_tooltip(23.0, 50.0)
        self.assertIn("T: 23.0 °C", sTip)
        self.assertIn("RH: 50.0 %", sTip)


class TestConfig(unittest.TestCase):
    def tearDown(self):
        configure(None)

    def test_configure_from_file_missing(self):
        sPrev = comfort._ranges
        self.assertIsNone(configure_from_file("/nonexistent/config.json"))
        self.assertEqual(comfort._ranges, sPrev)

    def test_configure_from_file_custom(self):
        with tempfile.TemporaryDirectory() as sDir:
            sPath = os.path.join(sDir, "config.json")
            with open(sPath, "w") as oFile:
                json.dump({"cold": {
                    "temperature_optimal": [17, 19],
                    "temperature_acceptable": [15, 21],
                }}, oFile)
            self.assertIsNotNone(configure_from_file(sPath))
            self.assertEqual(temperature_ranges(COLD),
                             ((17.0, 19.0), (15.0, 21.0)))
            # Не тронутые ключи — дефолты.
            self.assertEqual(temperature_ranges(WARM),
                             ((23.0, 25.0), (21.0, 28.0)))

    def test_configure_from_file_broken(self):
        with tempfile.TemporaryDirectory() as sDir:
            sPath = os.path.join(sDir, "config.json")
            with open(sPath, "w") as oFile:
                oFile.write("{broken json")
            self.assertIsNone(configure_from_file(sPath))

    def test_configure_reset(self):
        configure({"cold": {
            "temperature_optimal": (1.0, 2.0),
            "temperature_acceptable": (0.5, 2.5),
            "humidity_optimal": (10.0, 20.0),
            "humidity_acceptable": (10.0, 20.0),
        }, "warm": {
            "temperature_optimal": (1.0, 2.0),
            "temperature_acceptable": (0.5, 2.5),
            "humidity_optimal": (10.0, 20.0),
            "humidity_acceptable": (10.0, 20.0),
        }})
        self.assertEqual(temperature_ranges(COLD), ((1.0, 2.0), (0.5, 2.5)))
        configure(None)
        self.assertEqual(temperature_ranges(COLD),
                         ((22.0, 24.0), (20.0, 25.0)))


if __name__ == "__main__":
    unittest.main()