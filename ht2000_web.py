#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HT2000 Web — мини веб-сервер для отображения последних измерений.

Стандартная библиотека (http.server, threading), без внешних зависимостей:

- `/?range=hour|day|week` — HTML-страница: текущие значения (CO2, температура,
  влажность) и SVG-графики из CSV-файла данных за последний час/сутки/неделю.
  Страница обновляется JS-ом через `/data` и `/graphs` (без перезагрузки);
- `/data` — текущие данные в JSON;
- `/graphs?range=...` — фрагмент графиков в JSON (для фонового обновления).

Возможности страницы: тёмная тема (локально сохраняется), favicon по цвету
CO2, индикатор «данные N минут назад», линия «сейчас», синхронный crosshair
(тултип с временем и значением точки сразу на всех трёх графиках), под
графиками — сводка мин/сред/макс, вертикальные линии проветривания
(11:00, 13:00, 14:30, 16:00) с подсветкой интервала после проветривания
и легендой.

Сервер стартует в отдельном фоновом потоке (daemon), слушает все интерфейсы
(0.0.0.0) на первом свободном порту из диапазона 8080..8089. Снимок текущих
значений запрашивается у владельца через колбек-функцию. Модуль покрыт
unittest.
"""

import base64
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
REFRESH_MS = REFRESH_SECONDS * 1000

# Слушаем все интерфейсы, чтобы страница была доступна по локальной сети.
BIND_ADDRESS = "0.0.0.0"

RANGES = ("hour", "day", "week")
RANGE_HOURS = {"hour": 1, "day": 24, "week": 24 * 7}

# Время проветривания: вертикальные линии на графиках (часы, минуты).
VENTILATION_TIMES = ((11, 0), (13, 0), (14, 30), (16, 0))
VENTILATION_LABEL = "проветривание"
VENTILATION_INTERVAL_MIN = 30       # подсветка интервала после проветривания

CO2_WARN_LO = 800
CO2_WARN_HI = 1200
CO2_COLOR_GREEN = "#00c853"
CO2_COLOR_YELLOW = "#ffd600"
CO2_COLOR_RED = "#ff2d00"

GRAPH_CO2_COLOR = "#1565c0"
GRAPH_TEMP_COLOR = "#ef6c00"
GRAPH_RH_COLOR = "#2e7d32"
NOW_LINE_COLOR = "#90a4ae"
VENT_FILL = "rgba(144,164,174,0.16)"
# Линия «сейчас» рисуется, пока данные не старше этого допуска
# (запись идёт раз в минуту, обрыв/пауза ~10 минут не гасят линию).
NOW_LINE_TOLERANCE_MIN = 10

# SVG-геометрия графиков. Значения X0/Y0/Y1 должны совпадать с JS на странице.
SVG_WIDTH = 640
SVG_HEIGHT = 170
SVG_LEFT = 52
SVG_RIGHT = 10
SVG_TOP = 8
SVG_BOTTOM = 28

PAGE_TEMPLATE = u"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>HT2000</title>
<link id="ht-favicon" rel="icon" href="{favicon}">
<style>
 :root{{--bg:#f5f5f5;--card:#fff;--text:#555;--muted:#999;--status:#aaa;--link:#1565c0;
       --shadow:rgba(0,0,0,.15);--sw:#b0bec5;--h2:#666;--line:#b0bec5}}
 body.dark{{--bg:#171a1f;--card:#242a33;--text:#aeb7c3;--muted:#6d7683;--status:#5f6672;
       --link:#7fb3ff;--shadow:rgba(0,0,0,.5);--sw:#45515e;--h2:#8b95a3;--line:#45515e}}
 body{{font-family:Verdana,sans-serif;background:var(--bg);margin:0;padding:24px;text-align:center;
       color:var(--text)}}
 h1{{font-size:15px;color:var(--text);margin:0 0 14px;font-weight:normal}}
 table{{margin:0 auto;border-collapse:collapse;background:var(--card);box-shadow:0 1px 6px var(--shadow)}}
 td,th{{padding:7px 18px;font-size:15px}}
 th{{color:var(--muted);font-weight:normal;text-align:right}}
 td{{min-width:120px}}
 .co2_g{{color:{green};font-weight:bold}}
 .co2_y{{color:{yellow};font-weight:bold}}
 .co2_r{{color:{red};font-weight:bold}}
 .status{{color:var(--status);margin-top:12px;font-size:12px}}
 .status.warn{{color:{red};font-weight:bold}}
 .range{{margin:18px auto 6px;font-size:14px}}
 .range a{{color:var(--link);text-decoration:none;padding:2px 10px;border-radius:10px}}
 .range a.active{{background:var(--link);color:#fff;font-weight:bold}}
 .sep{{color:var(--muted);padding:0 4px}}
 .themebtn{{position:fixed;top:10px;right:10px;border:0;background:var(--card);
       color:var(--text);font-size:15px;cursor:pointer;border-radius:12px;
       box-shadow:0 1px 4px var(--shadow);padding:4px 8px}}
 .graph{{background:var(--card);border-radius:6px;box-shadow:0 1px 6px var(--shadow);
       margin:12px auto;padding:10px 12px 6px;max-width:{svgw}px;text-align:left}}
 .graph h2{{font-size:13px;color:var(--h2);margin:0 0 4px;font-weight:normal}}
 .graph svg{{width:100%;height:auto;display:block}}
 .graph text{{font-family:Verdana,sans-serif;font-size:10px;fill:var(--muted)}}
 .graph .empty{{color:var(--muted);font-size:13px;padding:30px 0;text-align:center}}
 .stats{{font-size:11px;color:var(--muted);margin-top:1px}}
 .legend{{font-size:12px;color:var(--muted);max-width:{svgw}px;margin:0 auto;text-align:left;padding:0 12px}}
 .legend .sw{{display:inline-block;width:26px;border-top:2px dashed var(--sw);margin-right:8px;vertical-align:middle}}
 #ht-tip{{position:fixed;background:rgba(40,45,50,.92);color:#fff;font:11px Verdana,sans-serif;
       padding:4px 8px;border-radius:4px;pointer-events:none;display:none;z-index:10;white-space:nowrap}}
 .ht-lv,.ht-lh{{stroke:var(--line);stroke-width:1;stroke-dasharray:4,3}}
 .ht-lv{{stroke-dasharray:4,3}}
 .ht-c{{fill:#fff;stroke:#333;stroke-width:1.5}}
</style>
</head>
<body>
<button id="ht-theme" class="themebtn" title="тема">🌙</button>
<h1>HT2000 · качество воздуха</h1>
<table>
<tr><th>CO₂</th><td class="{co2class}" id="ht-co2">{co2}</td></tr>
<tr><th>Температура</th><td id="ht-temp">{temp}</td></tr>
<tr><th>Влажность</th><td id="ht-hum">{hum}</td></tr>
</table>
<div class="status" id="ht-status">{status}</div>
<div class="range">
 <a href="/?range=hour" class="{cls_hour}">час</a><span class="sep">·</span>
 <a href="/?range=day" class="{cls_day}">сутки</a><span class="sep">·</span>
 <a href="/?range=week" class="{cls_week}">неделя</a>
</div>
<div class="graphs" id="ht-graphs">
{legend}
{graphs}
</div>
<div id="ht-tip"></div>
<script>
(function(){{
  var X0 = {sx0}, X1 = {sx1}, Y0 = {sy0}, Y1 = {sy1}; /* границы области графика в viewBox */
  var REFRESH_MS = {refresh_ms};
  var NS = 'http://www.w3.org/2000/svg';
  var tip = document.getElementById('ht-tip');
  var graphs = document.getElementById('ht-graphs');
  var m = location.search.match(/range=([a-z]+)/);
  var range = (m && ['hour','day','week'].indexOf(m[1]) >= 0) ? m[1] : 'hour';

  /* Тёмная тема */
  function applyTheme(dark){{
    document.body.classList.toggle('dark', dark);
    document.getElementById('ht-theme').textContent = dark ? '☀️' : '🌙';
  }}
  var themeBtn = document.getElementById('ht-theme');
  themeBtn.addEventListener('click', function(){{
    var dark = !document.body.classList.contains('dark');
    applyTheme(dark);
    try {{ localStorage.setItem('ht2000_theme', dark ? 'dark' : 'light'); }} catch(e) {{}}
  }});
  var saved = null;
  try {{ saved = localStorage.getItem('ht2000_theme'); }} catch(e) {{}}
  applyTheme(saved === 'dark');

  /* Favicon по цвету CO2 */
  function co2Color(v){{
    if (v >= 1200) return '{red}';
    if (v >= 800) return '{yellow}';
    return '{green}';
  }}
  var favicon = document.getElementById('ht-favicon');
  function setFavicon(v){{
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">' +
              '<circle cx="16" cy="16" r="13" fill="' + co2Color(v) + '"/></svg>';
    favicon.setAttribute('href', 'data:image/svg+xml;base64,' + btoa(svg));
  }}

  /* Crosshair: один контейнер для тултипа, линия на всех графиках */
  function clearCross(){{
    tip.style.display = 'none';
    var gs = graphs.querySelectorAll('g.ht-cross');
    for (var i = 0; i < gs.length; i++) gs[i].innerHTML = '';
  }}

  function onMove(ev){{
    var svg = ev.currentTarget;
    var pts = JSON.parse(svg.getAttribute('data-pts'));
    if (pts.length < 1) return;
    var r = svg.getBoundingClientRect();
    var vb = svg.viewBox.baseVal;
    var px = (ev.clientX - r.left) / r.width * vb.width;
    var py = (ev.clientY - r.top) / r.height * vb.height;
    var best = pts[0], bestD = Infinity;
    for (var i = 0; i < pts.length; i++) {{
      var d = Math.abs(pts[i][0] - px);
      if (d < bestD) {{ bestD = d; best = pts[i]; }}
    }}
    var all = graphs.querySelectorAll('svg[data-pts]');
    for (var s = 0; s < all.length; s++) {{
      var cur = all[s];
      var g = cur.querySelector('g.ht-cross');
      if (!g) {{
        g = document.createElementNS(NS, 'g');
        g.setAttribute('class', 'ht-cross');
        cur.appendChild(g);
      }}
      g.innerHTML = '<line class="ht-lv" x1="' + best[0] + '" y1="' + Y0 + '" x2="' + best[0] + '" y2="' + Y1 + '"/>';
      if (cur === svg) {{
        g.innerHTML += '<line class="ht-lh" x1="' + X0 + '" y1="' + best[1] + '" x2="' + best[0] + '" y2="' + best[1] + '"/>' +
                       '<circle class="ht-c" cx="' + best[0] + '" cy="' + best[1] + '" r="3.5"/>';
      }}
    }}
    tip.textContent = best[2] + ' · ' + best[3];
    tip.style.left = (r.left + best[0] / vb.width * r.width + 14) + 'px';
    tip.style.top = (r.top + best[1] / vb.height * r.height - 34) + 'px';
    tip.style.display = 'block';
  }}

  function initGraphs(){{
    var all = graphs.querySelectorAll('svg[data-pts]');
    for (var i = 0; i < all.length; i++) {{
      all[i].addEventListener('mousemove', onMove);
      all[i].addEventListener('mouseleave', clearCross);
    }}
    graphs.addEventListener('mouseleave', clearCross);
  }}

  /* Обновление без перезагрузки: /data (таблица) + /graphs (графики) */
  function parseAge(sTime){{
    if (!sTime) return null;
    var t = Date.parse(sTime);
    return isNaN(t) ? null : Date.now() - t;
  }}

  function fmtAge(ms){{
    if (ms < 60000) return Math.round(ms / 1000) + ' с назад';
    return Math.round(ms / 60000) + ' мин назад';
  }}

  function refresh(){{
    fetch('/data').then(function(r){{ return r.json(); }}).then(function(d){{
      var cell = document.getElementById('ht-co2');
      if (d.co2 !== undefined && d.co2 !== null) {{
        cell.textContent = d.co2 + ' ppm';
        cell.className = co2Color(d.co2) === '{red}' ? 'co2_r' :
                         co2Color(d.co2) === '{yellow}' ? 'co2_y' : 'co2_g';
      }}
      var t = document.getElementById('ht-temp');
      if (d.temperature !== undefined && d.temperature !== null)
        t.textContent = d.temperature.toFixed(1) + ' °C';
      var h = document.getElementById('ht-hum');
      if (d.humidity !== undefined && d.humidity !== null)
        h.textContent = d.humidity.toFixed(1) + ' %';
      var st = document.getElementById('ht-status');
      var age = parseAge(d.time);
      if (d.connected && age !== null && age < 180000) {{
        st.textContent = 'обновлено: ' + d.time.substring(0, 19).replace('T', ' ') + ' (' + fmtAge(age) + ')';
        st.className = 'status';
      }} else if (age !== null) {{
        st.textContent = 'связь потеряна · данные ' + fmtAge(age);
        st.className = 'status warn';
      }} else {{
        st.textContent = 'нет связи с устройством';
        st.className = 'status warn';
      }}
      if (d.co2) setFavicon(d.co2);
    }}).catch(function(){{}});
    fetch('/graphs?range=' + range).then(function(r){{ return r.json(); }}).then(function(d){{
      if (d.html) {{ graphs.innerHTML = d.html; initGraphs(); }}
    }}).catch(function(){{}});
  }}

  initGraphs();
  setInterval(refresh, REFRESH_MS);
  refresh();
}})(window);
</script>
</body>
</html>
"""

GRAPH_TEMPLATE = u"""<div class="graph">
<h2>{title}</h2>
{body}
<div class="stats">{stats}</div>
</div>"""

LEGEND_TEMPLATE = u"""<div class="legend"><span class="sw"></span>{label}</div>"""


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
    """Метка времени на оси: часы для 'hour', с датой для 'day'/'week'."""
    if sRange == "hour":
        return oDt.strftime("%H:%M")
    return oDt.strftime("%d.%m %H:%M")


def render_graph(sTitle, aRows, nValueIndex, sColor, sValueFormat, sRange,
                 aVerticals=None, oNow=None, oNowLabel=None):
    """SVG-график одной величины из строк read_recent.

    aRows — список (datetime, co2, temp, rh); nValueIndex — индекс величины;
    aVerticals — временные отметки (datetime) проветривания: вертикальные
    пунктирные линии + подсветка интервала после каждой;
    oNow — момент "сейчас": вертикальная линия, если попал в окно графика."""
    if len(aRows) < 2:
        return GRAPH_TEMPLATE.format(
            title=sTitle, body='<div class="empty">нет данных за период</div>',
            stats="")
    aVals = [oRow[nValueIndex] for oRow in aRows]
    dMin = min(aVals)
    dMax = max(aVals)
    dAvg = sum(aVals) / len(aVals)
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

    # Подсветка интервалов после проветривания (поверх фона, под линией).
    sVentRects = ""
    if aVerticals:
        oStep = timedelta(minutes=VENTILATION_INTERVAL_MIN)
        for oDt in aVerticals:
            oEnd = oDt + oStep
            dA = max(fX(oDt), dX0)
            dB = min(fX(oEnd), dX1)
            if oEnd <= oT0 or oDt >= oT1 or dB <= dA:
                continue
            sVentRects += '<rect x="{0:.1f}" y="{1}" width="{2:.1f}" height="{3}" ' \
                          'fill="{4}"/>'.format(dA, dY0, dB - dA, dY1 - dY0, VENT_FILL)

    aPts = ["{0:.1f},{1:.1f}".format(fX(oRow[0]), fY(oRow[nValueIndex])) for oRow in aRows]
    sLine = "<polyline fill=\"none\" stroke=\"{0}\" stroke-width=\"1.5\" " \
            "points=\"{1}\"/>".format(sColor, " ".join(aPts))

    # Маркеры точек: не чаще одной точки на 60 отсчётов.
    nStep = max(1, len(aRows) // 60)
    sMarks = ""
    for nIndex in range(0, len(aRows), nStep):
        oRow = aRows[nIndex]
        sMarks += '<circle cx="{0:.1f}" cy="{1:.1f}" r="2.2" fill="{2}"/>'.format(
            fX(oRow[0]), fY(oRow[nValueIndex]), sColor)
    if (len(aRows) - 1) % nStep != 0:
        oRow = aRows[-1]
        sMarks += '<circle cx="{0:.1f}" cy="{1:.1f}" r="2.2" fill="{2}"/>'.format(
            fX(oRow[0]), fY(oRow[nValueIndex]), sColor)

    # Вертикальные линии проветривания.
    sVerticals = ""
    if aVerticals:
        for oDt in aVerticals:
            if oT0 <= oDt <= oT1:
                dX = fX(oDt)
                sVerticals += '<line x1="{0:.1f}" y1="{1}" x2="{0:.1f}" y2="{2}" ' \
                              'stroke="{3}" stroke-width="1" ' \
                              'stroke-dasharray="4,3"/>'.format(
                    dX, dY0, dY1, NOW_LINE_COLOR)

    # Линия «сейчас».
    sNow = ""
    if oNow is not None and oT0 <= oNow \
            and (oNow - oT1).total_seconds() <= NOW_LINE_TOLERANCE_MIN * 60:
        dX = fX(oNow)
        sNow = '<line x1="{0:.1f}" y1="{1}" x2="{0:.1f}" y2="{2}" ' \
               'stroke="{3}" stroke-width="1.5"/>'.format(dX, dY0, dY1, NOW_LINE_COLOR)
        sNow += '<text x="{0:.1f}" y="{1}" text-anchor="middle">{2}</text>'.format(
            dX, SVG_TOP + 12, oNowLabel or "сейчас")

    aData = []
    for oRow in aRows:
        dV = oRow[nValueIndex]
        aData.append([round(fX(oRow[0]), 1), round(fY(dV), 1),
                      _format_time(oRow[0], sRange),
                      ("{0:" + sValueFormat + "}").format(dV)])
    sDataAttr = " data-pts='{0}'".format(json.dumps(aData, ensure_ascii=False))

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
            'xmlns="http://www.w3.org/2000/svg"{6}>{2}{3}{4}{5}{7}</svg>').format(
        SVG_WIDTH, SVG_HEIGHT, sVentRects, sVerticals, sNow, sLine,
        sDataAttr, sMarks + sLabels)
    sStats = "мин {0} · сред {1} · макс {2}".format(
        ("{0:" + sValueFormat + "}").format(min(aVals)),
        ("{0:" + sValueFormat + "}").format(dAvg),
        ("{0:" + sValueFormat + "}").format(max(aVals)))
    return GRAPH_TEMPLATE.format(title=sTitle, body=sSvg, stats=sStats)


def render_graphs(aRows, sRange, aVerticals=None, oNow=None):
    """Три SVG-графика (CO2, температура, влажность) для страницы."""
    sCo2 = render_graph("CO₂, ppm", aRows, 1, GRAPH_CO2_COLOR, ".0f", sRange,
                        aVerticals, oNow)
    sTemp = render_graph("Температура, °C", aRows, 2, GRAPH_TEMP_COLOR, ".1f",
                         sRange, aVerticals, oNow)
    sRh = render_graph("Влажность, %", aRows, 3, GRAPH_RH_COLOR, ".1f",
                       sRange, aVerticals, oNow)
    return sCo2 + sTemp + sRh


def _empty_graphs():
    aEmpty = ""
    for sTitle in ("CO₂, ppm", "Температура, °C", "Влажность, %"):
        aEmpty += GRAPH_TEMPLATE.format(
            title=sTitle, body='<div class="empty">нет данных за период</div>',
            stats="")
    return aEmpty


def graphs_fragment(sDataDir, sRange, oNow=None):
    """HTML-фрагмент секции графиков (легенда + три графика).

    Возвращает строку для вставки в страницу или в JSON /graphs."""
    if sRange not in RANGES:
        sRange = "hour"
    if oNow is None:
        oNow = datetime.now()
    aRows = []
    if sDataDir:
        aRows = read_recent(sDataDir, RANGE_HOURS[sRange])
    aVerticals = ventilation_lines(oNow)
    sLegend = ""
    if aRows:
        oT0 = aRows[0][0]
        oT1 = aRows[-1][0]
        if any(oT0 <= oDt <= oT1 for oDt in aVerticals):
            sLegend = LEGEND_TEMPLATE.format(label=VENTILATION_LABEL)
    sGraphs = render_graphs(aRows, sRange, aVerticals, oNow) if aRows \
        else _empty_graphs()
    return sLegend + sGraphs


def ventilation_lines(oRef):
    """Метки времени проветривания за день oRef (datetime)."""
    return [oRef.replace(hour=nH, minute=nM, second=0, microsecond=0)
            for nH, nM in VENTILATION_TIMES]


def favicon_data_url(nCo2):
    """data-URL favicon: круг, окрашенный по уровню CO2."""
    sColor = co2_color(nCo2)
    sSvg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<circle cx="16" cy="16" r="13" fill="{0}"/></svg>').format(sColor)
    return "data:image/svg+xml;base64," + base64.b64encode(
        sSvg.encode("utf-8")).decode("ascii")


def co2_css_class(nPpm):
    """CSS-класс цвета CO2 по уровню (зелёный/жёлтый/красный)."""
    if nPpm >= CO2_WARN_HI:
        return "co2_r"
    if nPpm >= CO2_WARN_LO:
        return "co2_y"
    return "co2_g"


def co2_color(nPpm):
    """Цвет CO2 по уровню (для favicon и линий)."""
    if nPpm >= CO2_WARN_HI:
        return CO2_COLOR_RED
    if nPpm >= CO2_WARN_LO:
        return CO2_COLOR_YELLOW
    return CO2_COLOR_GREEN


# -- Страница ---------------------------------------------------------------


def format_page(oSnapshot=None, sRange="hour", sDataDir=None, oWhen=None):
    """Собирает полную HTML-страницу.

    Параметры:
        oSnapshot — dict (co2/temperature/humidity/connected/time) или None;
        sRange — 'hour' | 'day' | 'week' — период графиков;
        sDataDir — каталог CSV-файлов данных (None — графики пустые);
        oWhen — момент "сейчас" (datetime) для вертикалей/линии «сейчас».
    Возвращает bytes (UTF-8)."""
    if sRange not in RANGES:
        sRange = "hour"
    if oWhen is None:
        oWhen = datetime.now()
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

    sFragment = graphs_fragment(sDataDir, sRange, oWhen)

    return PAGE_TEMPLATE.format(
        favicon=favicon_data_url(oSnapshot["co2"] if oSnapshot else 0),
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
        cls_week="active" if sRange == "week" else "",
        legend="",
        graphs=sFragment,
        svgw=SVG_WIDTH,
        sx0=SVG_LEFT,
        sx1=SVG_WIDTH - SVG_RIGHT,
        sy0=SVG_TOP,
        sy1=SVG_HEIGHT - SVG_BOTTOM,
        refresh_ms=REFRESH_MS,
    ).encode("utf-8")


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
    """Обработчик GET: '/' — страница, '/graphs' — фрагмент, '/data' — JSON."""

    def do_GET(self):
        oSnapshot = self.server.fnSnapshot()
        oUrl = urlsplit(self.path)
        if oUrl.path in ("/", "/index.html"):
            sRange = self._range(oUrl)
            self._send(200, "text/html; charset=utf-8",
                       format_page(oSnapshot, sRange=sRange,
                                   sDataDir=self.server.sDataDir))
        elif oUrl.path == "/graphs":
            sRange = self._range(oUrl)
            sHtml = graphs_fragment(self.server.sDataDir, sRange)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"html": sHtml}).encode("utf-8"))
        elif oUrl.path == "/data":
            self._send(200, "application/json; charset=utf-8", format_json(oSnapshot))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _range(self, oUrl):
        aRanges = parse_qs(oUrl.query).get("range")
        if aRanges and aRanges[0] in RANGES:
            return aRanges[0]
        return "hour"

    def log_message(self, sFormat, *aArgs):
        sys.stderr.write("[ht2000-web] {0} {1}\n".format(
            self.address_string(), sFormat % aArgs))

    def _send(self, nCode, sContentType, oBody):
        self.send_response(nCode)
        self.send_header("Content-Type", sContentType)
        self.send_header("Content-Length", str(len(oBody)))
        self.end_headers()
        self.wfile.write(oBody)