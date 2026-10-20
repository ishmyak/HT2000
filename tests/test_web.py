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

from ht2000_web import (HT2000WebError, HT2000WebServer, comfort_badge,
                        format_json, format_page, graphs_fragment,
                        lan_web_addresses, read_recent, render_graph,
                        render_graphs, snapshot_with_comfort, ventilation_lines)

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
        self.assertIn("обновлено: 2026-10-19 12:45:00", sHtml)

    def test_page_status_label_has_no_relative_time(self):
        # Требование: из лейбла «обновлено» убрано относительное время
        # («N с/мин назад»); остаётся только абсолютная метка времени.
        sHtml = format_page(SNAPSHOT).decode("utf-8")
        # В JS-ветке подключения остаётся только абсолютное время.
        self.assertIn("обновлено: ", sHtml)
        self.assertIn("d.time.substring(0, 19)", sHtml)
        # Относительное время больше не приклеивается к лейблу «обновлено».
        self.assertNotIn("+ ' (' + fmtAge(age) + ')'", sHtml)
        # Серверная подстановка тоже без относительного времени («назад»).
        self.assertNotIn("обновлено: N мин назад", sHtml)

    def test_page_no_refresh_meta(self):
        # Обновление страницы — через JS (fetch), без перезагрузки.
        sHtml = format_page(SNAPSHOT).decode("utf-8")
        self.assertNotIn('http-equiv="refresh"', sHtml)
        self.assertIn('id="ht-graphs"', sHtml)
        self.assertIn("ht-tip", sHtml)

    def test_page_theme_and_favicon(self):
        sHtml = format_page(SNAPSHOT).decode("utf-8")
        self.assertIn("ht-theme", sHtml)                    # кнопка темы
        self.assertIn("localStorage", sHtml)                # сохранение темы
        self.assertIn("ht-favicon", sHtml)                  # favicon
        self.assertIn("data:image/svg+xml;base64,", sHtml)

    def test_favicon_color_matches_level(self):
        oRed = dict(SNAPSHOT, co2=1500)
        sHtml = format_page(oRed).decode("utf-8")
        self.assertIn("ff2d00", sHtml)                      # красный favicon

    def test_page_comfort_badges(self):
        # 25.9 °C выше допустимой зоны (20–25, холодный сезон), 30.1 %
        # ниже нормы влажности — стабильно независимо от сезона.
        sHtml = format_page(SNAPSHOT).decode("utf-8")
        self.assertIn('class="st_bad"', sHtml)              # T вне допустимой
        self.assertIn("выше допустимой 20–25 °C", sHtml)
        self.assertIn('class="st_warn"', sHtml)             # RH ниже нормы
        self.assertIn("ниже нормы 40–60 %", sHtml)
        self.assertNotIn('class="st_ok"', sHtml)

    def test_page_comfort_badges_ok(self):
        oOk = dict(SNAPSHOT, temperature=23.0, humidity=50.0)
        sHtml = format_page(oOk).decode("utf-8")
        self.assertIn('class="st_ok"', sHtml)
        self.assertIn("норма ", sHtml)

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
        self.assertIn('?range=quarter', sHtml)          # режим 4 часа
        self.assertIn('?range=day', sHtml)
        self.assertIn('?range=week', sHtml)             # недельный диапазон

    def test_page_quarter_range_active(self):
        sHtml = format_page(SNAPSHOT, sRange="quarter").decode("utf-8")
        self.assertIn('href="/?range=quarter" class="active"', sHtml)
        self.assertNotIn('href="/?range=hour" class="active"', sHtml)

    def test_quarter_is_valid_render(self):
        # «4 часа» — валидный диапазон: графики рендерятся без ошибок.
        sHtml = format_page(SNAPSHOT, sRange="quarter").decode("utf-8")
        self.assertIn("4 часа", sHtml)

    def test_graph_up_button_present(self):
        # Кнопка подъёма графика (стрелка ▲) есть на каждой карточке.
        sHtml = format_page(SNAPSHOT, sRange="hour", sDataDir=None).decode("utf-8")
        self.assertIn("gh-up", sHtml)
        self.assertGreater(sHtml.count("gh-up"), 0)

    def test_invalid_range_falls_back_to_hour(self):
        sHtml = format_page(SNAPSHOT, sRange="invalid").decode("utf-8")
        self.assertIn('class="active"', sHtml)
        # активна ссылка часового диапазона
        nHour = sHtml.index('href="/?range=hour" class="active"')
        self.assertGreater(nHour, 0)

    def test_format_json(self):
        oData = json.loads(format_json(SNAPSHOT).decode("utf-8"))
        self.assertEqual(oData["co2"], 737)


class TestLanAddresses(unittest.TestCase):
    """URL веб-сервера для локальной сети."""

    def test_lan_web_addresses_format(self):
        aAddrs = lan_web_addresses(8080)
        for sUrl in aAddrs:
            self.assertRegex(sUrl, r"^http://\d{1,3}(\.\d{1,3}){3}:8080/$")
        self.assertTrue(aAddrs)

    def test_lan_web_addresses_no_loopback(self):
        # Петлевые адреса для других компьютеров бесполезны — их быть не должно.
        for sUrl in lan_web_addresses(8085):
            self.assertNotIn("127.0.0.1", sUrl)

    def test_lan_web_addresses_port(self):
        aAddrs = lan_web_addresses(8090)
        self.assertTrue(all(sUrl.endswith(":8090/") for sUrl in aAddrs))


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

    def test_render_graph_has_stats(self):
        # 5 точек: значения 700..704 -> мин 700, сред 702, макс 704.
        sSvg = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour")
        self.assertIn('class="stats"', sSvg)
        self.assertIn("мин 700", sSvg)
        self.assertIn("сред 702", sSvg)
        self.assertIn("макс 704", sSvg)

    def test_render_graph_no_now_line(self):
        # Линия «сейчас» убрана по требованию пользователя.
        sSvg = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour")
        self.assertNotIn("сейчас", sSvg)
        self.assertNotIn('stroke="#90a4ae"', sSvg)   # теперь только вертикали

    def test_render_graph_ventilation_interval_highlight(self):
        oAt = self.aRows[2][0]              # внутри окна
        aVert = [oAt]
        sSvg = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour",
                            aVerticals=aVert)
        self.assertIn("<rect", sSvg)        # подсветка интервала 30 минут

    def test_render_graph_band_norm(self):
        # Зоны нормы (оптимальная и допустимая) рисуются, когда данные
        # попадают в диапазон, а сводка получает текст норм даже вне шкалы.
        aRows = [(oDt, dCo2, 23.0, dRh) for oDt, dCo2, _, dRh in self.aRows]
        sSvg = render_graph("Температура, °C", aRows, 2, "#123", ".1f",
                            "hour", aBand=(22.0, 24.0), aBandAcc=(20.0, 25.0))
        self.assertIn("rgba(46,125,50,0.12)", sSvg)   # BAND_FILL
        self.assertIn("rgba(46,125,50,0.05)", sSvg)   # BAND_ACC_FILL
        self.assertIn("<rect", sSvg)
        self.assertIn("норма 22–24 (допустимо 20–25)", sSvg)
        sSvg2 = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour")
        self.assertNotIn("rgba(46,125,50", sSvg2)
        self.assertNotIn("норма ", sSvg2)

    def test_render_graph_band_outside_scale(self):
        # Зоны ниже шкалы (T=25 при оптимуме 22–24) — полос нет,
        # норма в сводке остаётся.
        sSvg = render_graph("Температура, °C", self.aRows, 2, "#123", ".1f",
                            "hour", aBand=(22.0, 24.0), aBandAcc=(20.0, 25.0))
        self.assertNotIn("height=\"0", sSvg)
        self.assertIn("норма 22–24", sSvg)

    def test_render_graphs_have_bands(self):
        # Температура: две зоны (оптимальная + допустимая), влажность: одна.
        aRows = [(oDt, dCo2, 23.0, 50.0) for oDt, dCo2, _, _ in self.aRows]
        sHtml = render_graphs(aRows, "hour")
        self.assertGreaterEqual(sHtml.count("rgba(46,125,50,0.12)"), 2)
        self.assertGreaterEqual(sHtml.count("rgba(46,125,50,0.05)"), 1)

    def test_render_graph_has_no_markers(self):
        # По требованию точки на линиях графиков убраны (маркеров нет).
        sSvg = render_graph("CO2, ppm", self.aRows, 1, "#123", ".0f", "hour")
        self.assertEqual(sSvg.count("<circle"), 0)   # маркеров точек нет
        self.assertIn("<polyline", sSvg)

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
                         [(8, 0), (11, 0), (13, 0), (14, 30), (16, 0)])


class TestComfortBadge(unittest.TestCase):
    def test_badge_classes(self):
        sOk = comfort_badge("23.0 °C", "ok", "22–24 °C", "20–25 °C")
        self.assertIn("st_ok", sOk)
        self.assertIn("✓", sOk)
        self.assertIn('title="норма 22–24 °C"', sOk)
        sWarn = comfort_badge("24.5 °C", "warn_high", "22–24 °C", "20–25 °C")
        self.assertIn("st_warn", sWarn)
        self.assertIn("выше оптимальной 22–24 °C", sWarn)
        self.assertIn("допустимо 20–25 °C", sWarn)
        sBad = comfort_badge("19.5 °C", "bad_low", "22–24 °C", "20–25 °C")
        self.assertIn("st_bad", sBad)
        self.assertIn("ниже допустимой 20–25 °C", sBad)
        # Одна зона (влажность): ниже — жёлтый, выше — красный.
        sHumLow = comfort_badge("30.1 %", "low", "40–60 %")
        self.assertIn("st_warn", sHumLow)
        self.assertIn("ниже нормы 40–60 %", sHumLow)
        sHumHigh = comfort_badge("65.0 %", "high", "40–60 %")
        self.assertIn("st_bad", sHumHigh)
        self.assertIn("выше нормы 40–60 %", sHumHigh)

    def test_snapshot_with_comfort(self):
        oRes = snapshot_with_comfort(SNAPSHOT)
        self.assertEqual(oRes["temp_status"], "bad_high")   # 25.9 > 25
        self.assertEqual(oRes["rh_status"], "low")          # 30.1 %
        self.assertEqual(oRes["temp_range_txt"], "22–24 °C")   # октябрь
        self.assertEqual(oRes["temp_acc_range_txt"], "20–25 °C")
        self.assertEqual(oRes["rh_range_txt"], "40–60 %")
        self.assertEqual(oRes["co2"], SNAPSHOT["co2"])      # остальное сохранено
        self.assertIsNone(snapshot_with_comfort(None))


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

    def test_graphs_endpoint(self):
        with tempfile.TemporaryDirectory() as sDir:
            oNow = datetime.now()
            sBody = ""
            for nIndex in range(10):
                oDt = oNow - timedelta(minutes=10 - nIndex)
                sBody += "{0};{1};{2};{3}\n".format(
                    oDt.strftime("%Y-%m-%d %H:%M:%S"), 800 + nIndex, 26.0, 40.0)
            write_csv(sDir, sBody)
            self.oServer = start_server(sDataDir=sDir)
            nCode, oBody, sType = fetch(self.oServer, "/graphs?range=day")
            self.assertEqual(nCode, 200)
            self.assertIn("application/json", sType)
            oData = json.loads(oBody.decode("utf-8"))
            self.assertIn("<polyline", oData["html"])
            self.assertIn('class="stats"', oData["html"])
            self.assertIn("rgba(46,125,50", oData["html"])  # зона нормы

    def test_data_endpoint_comfort_fields(self):
        self.oServer = start_server()
        try:
            nCode, oBody, sType = fetch(self.oServer, "/data")
            oData = json.loads(oBody.decode("utf-8"))
            self.assertEqual(nCode, 200)
            self.assertIn("temp_status", oData)
            self.assertIn("rh_status", oData)
            self.assertIn("temp_range_txt", oData)
            self.assertIn("temp_acc_range_txt", oData)
            self.assertIn("rh_range_txt", oData)
            # 25.9 °C выше допустимых 25 °C, 30.1 % ниже нормы — в любом сезоне.
            self.assertEqual(oData["temp_status"], "bad_high")
            self.assertEqual(oData["rh_status"], "low")
        finally:
            self.oServer.stop()

    def test_events_endpoint(self):
        # SSE: text/event-stream; первое событие snapshot с JSON-снимком,
        # затем graphs с фрагментом графиков (данные из CSV).
        with tempfile.TemporaryDirectory() as sDir:
            oNow = datetime.now()
            sBody = ""
            for nIndex in range(10):
                oDt = oNow - timedelta(minutes=10 - nIndex)
                sBody += "{0};{1};{2};{3}\n".format(
                    oDt.strftime("%Y-%m-%d %H:%M:%S"), 800 + nIndex, 26.0, 40.0)
            write_csv(sDir, sBody)
            self.oServer = start_server(sDataDir=sDir)
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:{0}/events?range=hour".format(
                            self.oServer.nPort), timeout=10) as oResp:
                    self.assertEqual(oResp.status, 200)
                    self.assertIn("text/event-stream",
                                  oResp.headers.get("Content-Type", ""))
                    # Собираем event.data по правилам EventSource: браузер
                    # учитывает только строки с префиксом data:.
                    sEvent = ""
                    aData = []
                    bSnapshot = False
                    for oLine in oResp:
                        sLine = oLine.decode("utf-8").rstrip("\r\n")
                        if sLine.startswith("event: "):
                            sEvent = sLine[7:]
                        elif sLine.startswith("data: "):
                            aData.append(sLine[6:])
                        elif not sLine:
                            sData = "\n".join(aData)
                            if sEvent == "snapshot":
                                self.assertEqual(json.loads(sData)["co2"], 737)
                                bSnapshot = True
                            elif sEvent == "graphs" and sData:
                                self.assertTrue(bSnapshot)
                                with self.oServer._condition:
                                    sExpected = self.oServer._cache["graphs"]["hour"][1]
                                self.assertEqual(sData, sExpected)
                                self.assertEqual(sData.count("<svg"), 3)
                                self.assertEqual(sData.count("<polyline"), 3)
                                break
                            sEvent = ""
                            aData = []
                    else:
                        self.fail("SSE-поток завершился без графиков")
            finally:
                self.oServer.stop()

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