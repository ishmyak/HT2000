#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверки минутной медианы, пропусков, смены суток и закрытия CSV."""

import csv
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ht2000_logger import HT2000Logger
from ht2000_web import read_recent


def sample(nCo2, dTemp=24.0, dRh=50.0):
    """Создаёт независимый от GUI и устройства снимок."""
    return SimpleNamespace(co2=nCo2, temperature=dTemp, humidity=dRh)


class TestLogger(unittest.TestCase):
    """Проверяет реальные CSV во временном каталоге."""

    def setUp(self):
        """Создаёт изолированный накопитель."""
        self.oTemp = tempfile.TemporaryDirectory()
        self.oLogger = HT2000Logger(self.oTemp.name)
        self.oMinute = datetime(2026, 10, 20, 12, 0)

    def tearDown(self):
        """Закрывает накопитель и удаляет временные данные."""
        self.oLogger.close()
        self.oTemp.cleanup()

    def rows(self, sDate="2026-10-20"):
        """Читает дневной CSV, включая заголовок."""
        sPath = os.path.join(self.oTemp.name, 'ht2000_' + sDate + '.csv')
        if not os.path.exists(sPath):
            return []
        with open(sPath, newline='') as oFile:
            return list(csv.reader(oFile, delimiter=';'))

    def test_medians_reject_short_spike(self):
        """Краткий пик не меняет медиану каждого из трёх параметров."""
        for nSecond, oSample in enumerate([
                sample(800, 22, 60), sample(9999, 50, 99), sample(810, 24, 40)]):
            self.oLogger.log(oSample, self.oMinute + timedelta(seconds=nSecond))
        self.assertEqual(self.rows(), [])
        self.oLogger.log(sample(900), self.oMinute + timedelta(minutes=1))
        self.assertEqual(self.rows()[1], ['2026-10-20 12:00:00', '810', '24.0', '60.0'])

    def test_even_samples_and_rounding(self):
        """Для чётного числа чтений берётся середина двух центральных."""
        self.oLogger.log(sample(800, 22, 40), self.oMinute)
        self.oLogger.log(sample(801, 24, 60), self.oMinute + timedelta(seconds=1))
        self.oLogger.log(None, self.oMinute + timedelta(minutes=1))
        self.assertEqual(self.rows()[1][1:], ['801', '23.0', '50.0'])

    def test_disconnection_flushes_previous_minute_only(self):
        """Потеря связи завершает прошлую минуту без повторения старых данных."""
        self.oLogger.log(sample(800), self.oMinute)
        for nMinute in range(1, 4):
            self.oLogger.log(None, self.oMinute + timedelta(minutes=nMinute))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[1][0], '2026-10-20 12:00:00')
        self.oLogger.log(sample(900), self.oMinute + timedelta(minutes=4))
        self.oLogger.close()
        self.assertEqual([aRow[0] for aRow in self.rows()[1:]],
                         ['2026-10-20 12:00:00', '2026-10-20 12:04:00'])

    def test_partial_minute_and_repeated_close(self):
        """Закрытие сохраняет неполную минуту ровно один раз."""
        self.oLogger.log(sample(850), self.oMinute + timedelta(seconds=47))
        self.oLogger.close()
        self.oLogger.close()
        self.assertEqual(self.rows()[1], ['2026-10-20 12:00:00', '850', '24.0', '50.0'])
        self.assertEqual(len(self.rows()), 2)

    def test_empty_minutes_create_no_file(self):
        """Без успешных измерений CSV не создаётся."""
        self.oLogger.log(None, self.oMinute)
        self.oLogger.log(None, self.oMinute + timedelta(minutes=2))
        self.oLogger.close()
        self.assertEqual(self.rows(), [])

    def test_pause_does_not_add_samples(self):
        """Пауза передаёт None; частичная минута содержит только реальные чтения."""
        self.oLogger.log(sample(800), self.oMinute)
        self.oLogger.log(None, self.oMinute + timedelta(seconds=20))
        self.oLogger.log(sample(900), self.oMinute + timedelta(seconds=50))
        self.oLogger.log(None, self.oMinute + timedelta(minutes=1))
        self.oLogger.log(None, self.oMinute + timedelta(minutes=2))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[1][1], '850')

    def test_midnight_uses_measured_day(self):
        """Последняя минута месяца записывается в файл измеренного дня."""
        oEnd = datetime(2026, 10, 31, 23, 59, 59)
        self.oLogger.log(sample(800), oEnd)
        self.oLogger.log(sample(900), oEnd + timedelta(seconds=1))
        self.assertEqual(self.rows('2026-10-31')[1][0], '2026-10-31 23:59:00')
        self.assertEqual(self.rows('2026-11-01'), [])
        self.oLogger.close()
        self.assertEqual(self.rows('2026-11-01')[1][0], '2026-11-01 00:00:00')

    def test_retry_after_open_failure(self):
        """Ошибка открытия не теряет накопленную минуту и не смешивает минуты."""
        self.oLogger.log(sample(800), self.oMinute)
        with patch('ht2000_logger.open', side_effect=OSError('test')), patch('sys.stderr'):
            self.oLogger.log(sample(9999), self.oMinute + timedelta(minutes=1))
        self.assertEqual(len(self.oLogger.aSamples), 1)
        self.oLogger.log(sample(900), self.oMinute + timedelta(minutes=1, seconds=1))
        self.oLogger.close()
        self.assertEqual([aRow[1] for aRow in self.rows()[1:]], ['800', '900'])

    def test_existing_data_and_web_reader(self):
        """Старые строки сохраняются, новые понимает прежний читатель графиков."""
        oNow = datetime.now().replace(second=0, microsecond=0)
        sDate = oNow.strftime('%Y-%m-%d')
        sPath = os.path.join(self.oTemp.name, 'ht2000_' + sDate + '.csv')
        sOld = 'datetime;co2_ppm;temp_c;rh_percent\n{};700;23.0;45.0\n'.format(
            (oNow - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S'))
        with open(sPath, 'w') as oFile:
            oFile.write(sOld)
        self.oLogger.log(sample(800), oNow)
        self.oLogger.close()
        with open(sPath) as oFile:
            self.assertTrue(oFile.read().startswith(sOld))
        aRows = read_recent(self.oTemp.name, 1)
        self.assertEqual([oRow[1] for oRow in aRows], [700, 800])

    def test_clock_backwards_does_not_mix_buckets(self):
        """Перевод часов назад не смешивает разные минутные интервалы."""
        self.oLogger.log(sample(800), self.oMinute)
        self.oLogger.log(sample(9999), self.oMinute - timedelta(minutes=1))
        self.oLogger.close()
        self.assertEqual(self.rows()[1][1], '800')
