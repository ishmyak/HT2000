#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты диагностики пропусков данных мониторинга (data_gaps.py).

Покрывают чтение CSV, поиск пропусков, пороговые значения, границы окна и
сводную статистику. Модуль и тесты — только стандартная библиотека.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_gaps import (DEFAULT_MAX_GAP_MIN, find_gaps, load_rows,
                       summarize_gaps)

CSV_HEADER = "datetime;co2_ppm;temp_c;rh_percent\n"

D0 = datetime(2026, 10, 20, 0, 0)


def row_line(oDt):
    """Формирует строку CSV для заданного момента (значения фиксированы)."""
    return "{0};700;25.3;40.0\n".format(oDt.strftime("%Y-%m-%d %H:%M:%S"))


def make_dir(aTimes):
    """Создаёт временный каталог с одним CSV, содержащим заданные строки."""
    oDir = tempfile.TemporaryDirectory()
    sPath = os.path.join(oDir.name, "ht2000_2026-10-20.csv")
    with open(sPath, "w", newline="") as oFile:
        oFile.write(CSV_HEADER)
        for oT in aTimes:
            oFile.write(row_line(oT))
    return oDir


def dt(nMinute):
    """Минута nMinute от начала суток 2026-10-20."""
    return D0 + timedelta(minutes=nMinute)


class TestLoadRows(unittest.TestCase):
    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as sDir:
            self.assertEqual(load_rows(sDir), [])

    def test_skips_header_and_malformed(self):
        oDir = make_dir([dt(0), dt(1)])
        try:
            sPath = os.path.join(oDir.name, "ht2000_2026-10-20_bad.csv")
            with open(sPath, "w") as oFile:
                oFile.write("мусор\n0;1\n")
            aRows = load_rows(oDir.name)
            # Значения фиксированы: co2=700 из row_line.
            self.assertEqual(len(aRows), 2)
            self.assertEqual(aRows[0][0], dt(0))
            self.assertEqual(aRows[0][1], 700)
        finally:
            oDir.cleanup()

    def test_sorted_across_files(self):
        oDir = tempfile.TemporaryDirectory()
        try:
            sPath1 = os.path.join(oDir.name, "ht2000_2026-10-20.csv")
            with open(sPath1, "w") as oFile:
                oFile.write(CSV_HEADER)
                oFile.write(row_line(dt(5)))
            sPath2 = os.path.join(oDir.name, "ht2000_2026-10-20z.csv")
            with open(sPath2, "w") as oFile:
                oFile.write(CSV_HEADER)
                oFile.write(row_line(dt(1)))  # раньше — сортировка нужна.
            aRows = load_rows(oDir.name)
            self.assertEqual(aRows[0][0], dt(1))
            self.assertEqual(aRows[1][0], dt(5))
        finally:
            oDir.cleanup()


class TestFindGaps(unittest.TestCase):
    def test_continuous_no_gaps(self):
        oDir = make_dir([dt(i) for i in range(10)])
        try:
            aGaps = find_gaps(load_rows(oDir.name))
            self.assertEqual(aGaps, [])
        finally:
            oDir.cleanup()

    def test_single_gap_detected(self):
        # Данные в 00:00..00:09, затем прыжок в 00:15 -> пропуск 00:10..00:14.
        oDir = make_dir([dt(i) for i in range(10)] + [dt(15)])
        try:
            aGaps = find_gaps(load_rows(oDir.name))
            self.assertEqual(len(aGaps), 1)
            self.assertEqual(aGaps[0]["from"], dt(10))
            self.assertEqual(aGaps[0]["to"], dt(14))
            self.assertEqual(aGaps[0]["minutes"], 5)
        finally:
            oDir.cleanup()

    def test_gap_shorter_than_threshold_ignored(self):
        # Пропуск 1-й минуты: 00:10..00:11 (одна минута) — меньше порога 2.
        oDir = make_dir([dt(0)] + [dt(2)])
        try:
            aGaps = find_gaps(load_rows(oDir.name), nMaxGapMin=2)
            self.assertEqual(aGaps, [])
        finally:
            oDir.cleanup()

    def test_threshold_respected(self):
        # Пропуск 3 минут (00:01..00:03). Правило: сообщать о пропуске,
        # если пропущенных минут СТРОГО больше порога. Значит:
        #   порог 2 или 3 -> 3 > 2 (да) и 3 > 3 (нет, равно) -> 1 и 0;
        #   порог 4 -> 3 > 4 (нет) -> 0.
        oDir = make_dir([dt(0), dt(4)])
        try:
            self.assertEqual(len(find_gaps(load_rows(oDir.name), 2)), 1)
            self.assertEqual(len(find_gaps(load_rows(oDir.name), 3)), 0)
            self.assertEqual(find_gaps(load_rows(oDir.name), 4), [])
        finally:
            oDir.cleanup()

    def test_pattern_60min_gap(self):
        # Воспроизводит реальный сбой: данные в 00:48 и 01:48 (после паузы
        # 60 мин) -> пропуск 00:49..01:47 длиной 59 минут.
        oDir = make_dir([dt(48), dt(48 + 60)])
        try:
            aGaps = find_gaps(load_rows(oDir.name))
            self.assertEqual(len(aGaps), 1)
            self.assertEqual(aGaps[0]["from"], dt(49))
            self.assertEqual(aGaps[0]["to"], dt(48 + 59))
            self.assertEqual(aGaps[0]["minutes"], 59)
        finally:
            oDir.cleanup()

    def test_subminute_rows_round_to_minute(self):
        # Строка в 00:48:30 принадлежит минуте 00:48 — пропуск от неё к
        # 01:48 равен 59 минутам, а не 60.
        oDir = tempfile.TemporaryDirectory()
        try:
            sPath = os.path.join(oDir.name, "ht2000_2026-10-20.csv")
            with open(sPath, "w") as oFile:
                oFile.write(CSV_HEADER)
                oFile.write("2026-10-20 00:48:30;700;25.3;40.0\n")
                oFile.write("2026-10-20 01:48:30;700;25.3;40.0\n")
            aGaps = find_gaps(load_rows(oDir.name))
            self.assertEqual(aGaps[0]["minutes"], 59)
        finally:
            oDir.cleanup()

    def test_window_limits_to_range(self):
        oDir = make_dir([dt(0), dt(5), dt(10)])
        try:
            # Окно [dt(2)..dt(4)] — внутри данных нет, это пропуск.
            aGaps = find_gaps(load_rows(oDir.name),
                              oStart=dt(2), oEnd=dt(4))
            self.assertEqual(len(aGaps), 1)
            self.assertEqual(aGaps[0]["minutes"], 3)
        finally:
            oDir.cleanup()

    def test_reversed_window_empty(self):
        oDir = make_dir([dt(0), dt(1)])
        try:
            aGaps = find_gaps(load_rows(oDir.name),
                              oStart=dt(5), oEnd=dt(1))
            self.assertEqual(aGaps, [])
        finally:
            oDir.cleanup()


class TestSummarizeGaps(unittest.TestCase):
    def test_stats_totals(self):
        # Два пропуска по 1 минуте в окне из 5 минут -> пропущено 2/5.
        # Для подсчёта одиночных минут задаём нулевой порог (иначе они
        # отфильтруются как «нормальный» 1-минутный джиттер).
        oDir = make_dir([dt(0), dt(2), dt(4)])
        try:
            aRows = load_rows(oDir.name)
            aGaps = find_gaps(aRows, nMaxGapMin=0, oStart=dt(0), oEnd=dt(4))
            aStats = summarize_gaps(aGaps, oStart=dt(0), oEnd=dt(4))
            self.assertEqual(aStats["count"], 2)
            self.assertEqual(aStats["minutes"], 2)
            self.assertEqual(aStats["window_minutes"], 5)
            self.assertEqual(aStats["window_present"], 3)
            self.assertEqual(aStats["percent"], 40.0)
        finally:
            oDir.cleanup()

    def test_stats_no_gaps(self):
        oDir = make_dir([dt(0), dt(1), dt(2)])
        try:
            aStats = summarize_gaps(find_gaps(load_rows(oDir.name)),
                                    oStart=dt(0), oEnd=dt(2))
            self.assertEqual(aStats["count"], 0)
            self.assertEqual(aStats["minutes"], 0)
            self.assertEqual(aStats["percent"], 0.0)
        finally:
            oDir.cleanup()


if __name__ == "__main__":
    unittest.main()