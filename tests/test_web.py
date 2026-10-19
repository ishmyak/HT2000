#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты HT2000 Web: HTML/JSON-ответы, поведение сервера, чтение CSV,
SVG-графики и выбор диапазона."""

import json
import os
import sys
import tempfile
import urllib.request
import urllib.error
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ht2000_web import (HT2000WebError, HT2000WebServer, format_json, format_page,
                        read_recent, render_graph, render_graphs,
                        ventilation_lines)

SNAPSHOT = {
    "co2": 737,
    "temperature": 25.9,
    "humidity": 30.1,
    "connected": True,
    "time": "2026-10-19 12:45:00",
}

CSV_HEADER = "datetime;co2_ppm;temp_c;rh_percent\n"


def start_server(fnSnapshot=None, sDataDir=None):
    o = HT2000WebServer(fnSnapshot or (lambda: SNAPSHOT), sDataDir=sDataDir)
    o.start()
    return o


def fetch(o, sPath):
    with urllib.request.urlopen("http://127.0.0.1:{0}{1}".format(o.nPort, sPath),
                                timeout=5) as oResp:
        return oResp.status, oResp.read(), oResp.headers.get("Content-Type", "")


def write_csv(oDir, sBody):
    sPath = os.path.join(oDir, "ht2000_2026-10-19.csv")
    with open(sPath, "w") as oFile:
        oFile.write(CSV_HEADER + sBody)
    return sPath


class TestFormatPage(unittest.TestCase):
    def test_page_contains_values(self):
        sHtml = format_page(SNAPSHOT).decode("utf-8")
        self.assertIn("737 ppm", sHtml)
        self.assertIn("25.9 °C", sHtml)
        self.assertIn("30.1 %", sHtml)
        self.assertIn('http-equiv="refresh"', sHtml)
        self.assertIn("обновлено: 2026-10-19 12:45:00", sHtml)

    def test_page_color_classes(self):
        oGreen = dict(SNAPSHOT, co2=500)
        oYellow = dict(SNAPSHOT, co2=900)
        oRed = dict(SNAPSHOT, co2=1500)
        self.assertIn("co2_g", format_page(oGreen).decode("utf-8"))
        self.assertIn("co2_y", format_page(oYellow).decode("utf-8"))
        self.assertIn("co2_r", format_page(oRed).decode("utf-8"))

    def test_page_no_data(self):
        sHtml = format_page(None).decode("utf-8")
        self.assertIn("нет связи", sHtml)
        self.assertIn("—", sHtml)

    def test_range_links(self):
        sHtml = format_page(SNAPSHOT).decode("utf-8")
        self.assertIn('?range=hour', sHtml)
        self.assertIn('?range=day', sHtml)

    def test_invalid_range_falls_back_to_hour(self):
        sHtml = format_page(SNAPSHOT, sRange="week").decode("utf-8")
        self.assertIn('class="active"', sHtml)
        # активна ссылка часового диапазона
        nHour = sHtml.index('href="/?range=hour" class="active"')
        self.assertGreater(nHour, 0)

    def test_format_json(self):
        oData = json.loads(format_json(SNAPSHOT).decode("utf-8"))
        self.assertEqual(oData["co2"], 737)


class TestReadCsv(unittest.TestCase):
    def setUp(self):
        self.oDir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.oDir.cleanup()

    def test_reads_recent_hour(self):
        oNow = datetime.now()
        sBody = ""
        for nIndex in range(3):
            oDt = oNow - timedelta(minutes=6 - 2 * nIndex)   # -6, -4, -2 мин
            sBody += "{0};{1};{2:.1f};{3:.1f}\n".format(
                oDt.strftime("%Y-%m-%d %H:%M:%S"), 700 + nIndex,
                25.0 + nIndex, 30.0)
        write_csv(self.oDir.name, sBody)
        aRows = read_recent(self.oDir.name, 1)
        self.assertEqual(len(aRows), 3)
        self.assertEqual(aRows[0][1], 700.0)    # самая старая строка
        self.assertEqual(aRows[-1][1], 702.0)   # самая свежая
        self.assertEqual(aRows[0][2], 25.0)

    def test_skips_old_and_broken_rows(self):
        oNow = datetime.now()
        sBody = ("not a row;x;y;z\n"
                 "2020-01-01 00:00:00;100;20.0;40.0\n"
                 "2020-01-01 00:00:01;broken;20.0;40.0\n")
        sBody += "{0};999;26.5;31.0\n".format(
            (oNow - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"))
        write_csv(self.oDir.name, sBody)
        aRows = read_recent(self.oDir.name, 1)
        self.assertEqual(len(aRows), 1)
        self.assertEqual(aRows[0][1], 999.0)

    def test_empty_dir(self):
        self.assertEqual(read_recent(self.oDir.name, 1), [])


class TestGraphs(unittest.TestCase):
    def setUp(self):
        self.oNow = datetime.now()
        self.aRows = []
        for nIndex in range(5):
            oDt = self.oNow - timedelta(minutes=5 - nIndex)
            self.aRows.append((oDt, 700.0 + nIndex, 25.0, 30.0))

    def test_render_graph_has_points_and_labels(self):
        sSvg = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour")
        self.assertIn("<polyline", sSvg)
        self.assertIn("points=\"", sSvg)
        self.assertIn("700", sSvg)          # метка минимума
        self.assertIn("704", sSvg)          # метка максимума
        self.assertIn("<svg", sSvg)

    def test_render_graph_has_markers(self):
        sSvg = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour")
        self.assertEqual(sSvg.count("<circle"), 5)   # все точки короткого ряда

    def test_render_graph_data_points_for_tooltip(self):
        sSvg = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour")
        self.assertIn("data-pts='", sSvg)
        # JSON внутри атрибута: координаты, время и значение точки.
        self.assertIn('"700"', sSvg)                 # значение первой точки
        sData = sSvg[sSvg.index("data-pts='") + len("data-pts='"):]
        sData = sData[:sData.index("'")]
        aPts = json.loads(sData)
        self.assertEqual(len(aPts), 5)
        self.assertEqual(len(aPts[0]), 4)
        self.assertEqual(aPts[0][3], "700")
        self.assertIn(":", aPts[0][2])               # время в формате ЧЧ:ММ

    def test_render_graph_ventilation_lines_inside_window(self):
        # Вертикаль ровно в момент строки — гарантированно внутри окна.
        aVerticals = [self.aRows[2][0]]
        sSvg = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour",
                            aVerticals)
        self.assertEqual(sSvg.count("<line"), 1)
        self.assertIn("stroke-dasharray=\"4,3\"", sSvg)

    def test_render_graph_ventilation_lines_outside_window(self):
        sSvg = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour",
                            aVerticals=[])
        self.assertNotIn("<line", sSvg)

    def test_render_graph_few_rows_empty(self):
        sSvg = render_graph("CO2, ppm", self.aRows[:1], 1, "#123", ".0f", "hour")
        self.assertIn("нет данных", sSvg)

    def test_render_graphs_three_charts(self):
        sAll = render_graphs(self.aRows, "hour")
        self.assertEqual(sAll.count("<polyline"), 3)
        self.assertIn("CO₂", sAll)
        self.assertIn("Температура", sAll)
        self.assertIn("Влажность", sAll)

    def test_page_includes_graphs_from_data_dir(self):
        with tempfile.TemporaryDirectory() as sDir:
            sBody = ""
            for nIndex in range(10):
                oDt = self.oNow - timedelta(minutes=10 - nIndex)
                sBody += "{0};{1};{2};{3}\n".format(
                    oDt.strftime("%Y-%m-%d %H:%M:%S"), 800 + nIndex, 26.0, 40.0)
            write_csv(sDir, sBody)
            sHtml = format_page(SNAPSHOT, sRange="hour", sDataDir=sDir).decode("utf-8")
            self.assertEqual(sHtml.count("<polyline"), 3)
            self.assertIn('stroke="#1565c0"', sHtml)      # график CO2
            self.assertIn('stroke="#ef6c00"', sHtml)      # график температуры
            self.assertIn("810</text>", sHtml)            # метка максимума CO2

    def test_page_ventilation_legend_visibility(self):
        # Суточное окно вокруг 17:00 19.10 покрывает сегодняшние вертикали
        # 11:00..16:00 — легенда должна появиться.
        oNow = datetime(2026, 10, 19, 17, 0, 0)
        with tempfile.TemporaryDirectory() as sDir:
            sBody = ""
            for nIndex in range(6):
                oDt = oNow - timedelta(hours=5 - nIndex)
                sBody += "{0};{1};{2};{3}\n".format(
                    oDt.strftime("%Y-%m-%d %H:%M:%S"), 800, 26.0, 40.0)
            write_csv(sDir, sBody)
            sHtml = format_page(SNAPSHOT, sRange="day", sDataDir=sDir,
                                oWhen=oNow).decode("utf-8")
            self.assertIn("проветривание", sHtml)
            self.assertGreater(sHtml.count("<line x1="), 0)   # вертикали в SVG

        # Часовое окно в 02:00 — ни одной вертикали, легенды нет.
        oNight = datetime(2026, 10, 19, 2, 0, 0)
        with tempfile.TemporaryDirectory() as sDir:
            sBody = ""
            for nIndex in range(6):
                oDt = oNight - timedelta(minutes=5 - nIndex)
                sBody += "{0};{1};{2};{3}\n".format(
                    oDt.strftime("%Y-%m-%d %H:%M:%S"), 800, 26.0, 40.0)
            write_csv(sDir, sBody)
            sHtml = format_page(SNAPSHOT, sRange="hour", sDataDir=sDir,
                                oWhen=oNight).decode("utf-8")
            self.assertNotIn("проветривание", sHtml)
            self.assertNotIn("<line x1=", sHtml)      # вертикалей в SVG нет

    def test_ventilation_lines_times(self):
        oRef = datetime(2026, 10, 19, 17, 0, 0)
        aLines = ventilation_lines(oRef)
        self.assertEqual([(o.hour, o.minute) for o in aLines],
                         [(11, 0), (13, 0), (14, 30), (16, 0)])


class TestServer(unittest.TestCase):
    def tearDown(self):
        if self.oServer is not None:
            self.oServer.stop()

    def setUp(self):
        self.oServer = None

    def test_html_endpoint(self):
        self.oServer = start_server()
        nCode, oBody, sType = fetch(self.oServer, "/")
        self.assertEqual(nCode, 200)
        self.assertIn("737 ppm".encode("utf-8"), oBody)
        self.assertIn("text/html", sType)

    def test_range_query_switches_graphs(self):
        with tempfile.TemporaryDirectory() as sDir:
            oNow = datetime.now()
            sBody = ""
            for nIndex in range(10):
                oDt = oNow - timedelta(minutes=nIndex)
                sBody += "{0};{1};{2};{3}\n".format(
                    oDt.strftime("%Y-%m-%d %H:%M:%S"), 800 + nIndex, 26.0, 40.0)
            write_csv(sDir, sBody)
            self.oServer = start_server(sDataDir=sDir)
            nCode, oBody, _ = fetch(self.oServer, "/?range=day")
            self.assertEqual(nCode, 200)
            self.assertIn(b'class="active"', oBody)
            nDayIdx = oBody.index(b'href="/?range=day" class="active"')
            self.assertGreater(nDayIdx, 0)

    def test_json_endpoint(self):
        self.oServer = start_server()
        nCode, oBody, sType = fetch(self.oServer, "/data")
        self.assertEqual(nCode, 200)
        self.assertIn("application/json", sType)
        oData = json.loads(oBody.decode("utf-8"))
        self.assertEqual(oData["co2"], 737)
        self.assertAlmostEqual(oData["temperature"], 25.9, places=1)

    def test_unknown_path_404(self):
        self.oServer = start_server()
        with self.assertRaises(urllib.error.HTTPError) as oCtx:
            fetch(self.oServer, "/nope")
        self.assertEqual(oCtx.exception.code, 404)

    def test_fallback_port(self):
        oOccupied = start_server()
        try:
            self.oServer = HT2000WebServer(lambda: SNAPSHOT,
                                           nStartPort=oOccupied.nPort)
            self.oServer.start()
            self.assertEqual(self.oServer.nPort, oOccupied.nPort + 1)
            nCode, oBody, _ = fetch(self.oServer, "/data")
            self.assertEqual(nCode, 200)
        finally:
            oOccupied.stop()

    def test_stop_then_start_again(self):
        self.oServer = start_server()
        self.oServer.stop()
        self.oServer.start()
        nCode, oBody, _ = fetch(self.oServer, "/")
        self.assertEqual(nCode, 200)

    def test_all_ports_busy_raises(self):
        oOccupied = start_server()
        try:
            oSecond = HT2000WebServer(lambda: None,
                                      nStartPort=oOccupied.nPort,
                                      nMaxAttempts=1)
            with self.assertRaises(HT2000WebError):
                oSecond.start()
            self.assertFalse(oSecond.is_running())
        finally:
            oOccupied.stop()


if __name__ == "__main__":
    unittest.main()