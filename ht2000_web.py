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
CO2, индикатор «данные N минут назад», синхронный crosshair (тултип с
временем и значением точки сразу на всех трёх графиках), под графиками —
сводка мин/сред/макс, вертикальные линии проветривания (8:00, 11:00, 13:00,
14:30, 16:00) с подсветкой интервала после проветривания и легендой,
кнопка ▲ для перестановки графиков (поднять нижний наверх и расширить).

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
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from comfort import comfort_status, humidity_ranges, temperature_ranges

START_PORT = 8080
MAX_PORT_ATTEMPTS = 10
REFRESH_SECONDS = 5
REFRESH_MS = REFRESH_SECONDS * 1000
# SSE-пуш: снимок — каждую секунду, графики — каждые 5 секунд.
SSE_SNAPSHOT_SECONDS = 1.0
SSE_GRAPHS_SECONDS = 5.0

# Слушаем все интерфейсы, чтобы страница была доступна по локальной сети.
BIND_ADDRESS = "0.0.0.0"

RANGES = ("hour", "quarter", "day", "week")
RANGE_HOURS = {"hour": 1, "quarter": 4, "day": 24, "week": 24 * 7}

# Время проветривания: вертикальные линии на графиках (часы, минуты).
VENTILATION_TIMES = ((8, 0), (11, 0), (13, 0), (14, 30), (16, 0))
VENTILATION_LABEL = "проветривание"
VENTILATION_INTERVAL_MIN = 15       # подсветка интервала после проветривания

CO2_WARN_LO = 800
CO2_WARN_HI = 1200
CO2_COLOR_GREEN = "#00c853"
CO2_COLOR_YELLOW = "#ffd600"
CO2_COLOR_RED = "#ff2d00"

GRAPH_CO2_COLOR = "#1565c0"
GRAPH_TEMP_COLOR = "#ef6c00"
GRAPH_RH_COLOR = "#2e7d32"
# Цвет вертикальных пунктирных линий проветривания на графиках.
NOW_LINE_COLOR = "#90a4ae"
VENT_FILL = "rgba(144,164,174,0.16)"
# Зоны нормы (см. comfort.py) на графиках температуры и влажности:
# допустимая — бледная, оптимальная — ярче.
BAND_FILL = "rgba(46,125,50,0.12)"
BAND_ACC_FILL = "rgba(46,125,50,0.05)"

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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HT2000</title>
<link id="ht-favicon" rel="icon" href="{favicon}">
<style>
 :root{{--bg:#eef1f5;--card:#fff;--text:#222a37;--muted:#8a93a3;--status:#9aa3b2;
       --link:#1565c0;--shadow:rgba(25,35,60,.10);--sw:#b0bec5;--h2:#3d4857;
       --line:#dde3ea;--warn-bg:rgba(255,214,0,.14);--ok-bg:rgba(0,200,83,.12);
       --bad-bg:rgba(255,45,0,.12)}}
 body.dark{{--bg:#101318;--card:#1d222b;--text:#cfd6e2;--muted:#6f7886;--status:#5f6672;
       --link:#7fb3ff;--shadow:rgba(0,0,0,.45);--sw:#45515e;--h2:#9aa5b3;
       --line:#333c48;--warn-bg:rgba(255,214,0,.12);--ok-bg:rgba(0,200,83,.10);
       --bad-bg:rgba(255,45,0,.14)}}
 body{{font-family:"Segoe UI",system-ui,Verdana,sans-serif;background:var(--bg);
       margin:0;padding:26px 18px;color:var(--text)}}
 .wrap{{max-width:1180px;margin:0 auto}}
 .hdr{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}}
 .hdr h1{{font-size:19px;font-weight:650;margin:0;letter-spacing:.01em}}
 .status{{font-size:12px;color:var(--status)}}
 .status.warn{{color:{red};font-weight:bold}}
 .cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:20px}}
 .card{{background:var(--card);border-radius:16px;padding:16px 18px;
       box-shadow:0 2px 12px var(--shadow)}}
 .card .label{{font-size:11px;color:var(--muted);text-transform:uppercase;
       letter-spacing:.09em}}
 .card .value{{font-size:36px;font-weight:700;line-height:1.25;margin:4px 0 2px}}
 .card .note{{font-size:11px;color:var(--muted)}}
 .co2_g{{color:{green}}}
 .co2_y{{color:var(--yellow-c,#cf9f00)}}
 .co2_r{{color:{red}}}
 body.dark .co2_y{{color:{yellow};color:var(--yellow-c,#ffd600)}}
 .st_ok{{color:{green};font-weight:bold}}
 .st_warn{{color:var(--yellow-c,#cf9f00);font-weight:bold}}
 body.dark .st_warn{{color:{yellow}}}
 .st_bad{{color:{red};font-weight:bold}}
 .range{{display:inline-flex;background:var(--card);border:1px solid var(--line);
        border-radius:999px;padding:3px;margin-bottom:16px;
        box-shadow:0 1px 5px var(--shadow)}}
 .range a{{color:var(--link);text-decoration:none;padding:6px 20px;
        border-radius:999px;font-size:13px}}
 .range a.active{{background:var(--link);color:#fff;font-weight:650}}
.graphs{{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}}
  .graph{{background:var(--card);border-radius:16px;padding:14px 16px 10px;
        box-shadow:0 2px 12px var(--shadow);text-align:left;position:relative}}
  .g-wide{{grid-column:1 / -1}}
  .graph h2{{font-size:13px;color:var(--h2);margin:0 0 6px;font-weight:650;
        padding-right:26px}}
  .gh-up{{position:absolute;top:10px;right:10px;border:1px solid var(--line);
        background:var(--card);color:var(--muted);font-size:11px;line-height:1;
        cursor:pointer;border-radius:6px;padding:4px 6px}}
  .gh-up:hover{{color:var(--link);border-color:var(--link)}}
 .graph svg{{width:100%;height:auto;display:block}}
 .graph text{{font-family:inherit;font-size:10px;fill:var(--muted)}}
 .graph .empty{{color:var(--muted);font-size:13px;padding:30px 0;text-align:center}}
 .stats{{font-size:11px;color:var(--muted);margin-top:4px}}
 .legend{{grid-column:1 / -1;font-size:12px;color:var(--muted);text-align:left;padding:0 4px}}
 .legend .sw{{display:inline-block;width:26px;border-top:2px dashed var(--sw);
       margin-right:8px;vertical-align:middle}}
 .themebtn{{position:fixed;top:14px;right:14px;border:1px solid var(--line);
       background:var(--card);color:var(--text);font-size:15px;cursor:pointer;
       border-radius:999px;box-shadow:0 1px 6px var(--shadow);padding:7px 11px}}
 #ht-tip{{position:fixed;background:rgba(20,25,32,.94);color:#fff;
       font:11px "Segoe UI",Verdana,sans-serif;padding:4px 9px;border-radius:6px;
       pointer-events:none;display:none;z-index:10;white-space:nowrap}}
 .ht-lv,.ht-lh{{stroke:var(--line);stroke-width:1;stroke-dasharray:4,3}}
 .ht-c{{fill:#fff;stroke:#333;stroke-width:1.5}}
 @media (max-width:760px){{
   .cards{{grid-template-columns:1fr}}
   .graphs{{grid-template-columns:1fr}}
 }}
</style>
</head>
<body>
<button id="ht-theme" class="themebtn" title="тема">🌙</button>
<div class="wrap">
 <div class="hdr">
  <h1>HT2000 · качество воздуха</h1>
  <div class="status" id="ht-status">{status}</div>
 </div>
 <div class="cards">
  <div class="card">
   <div class="label">CO₂</div>
   <div class="value"><span id="ht-co2" class="{co2class}">{co2}</span></div>
   <div class="note">проветривайте, если более 800 ppm</div>
  </div>
  <div class="card">
   <div class="label">Температура</div>
   <div class="value" id="ht-temp">{temp}</div>
   <div class="note" id="ht-temp-range">{temp_range}</div>
  </div>
  <div class="card">
   <div class="label">Влажность</div>
   <div class="value" id="ht-hum">{hum}</div>
   <div class="note" id="ht-hum-range">{hum_range}</div>
  </div>
 </div>
<div class="range">
   <a href="/?range=hour" class="{cls_hour}">час</a>
   <a href="/?range=quarter" class="{cls_quarter}">4 часа</a>
   <a href="/?range=day" class="{cls_day}">сутки</a>
   <a href="/?range=week" class="{cls_week}">неделя</a>
  </div>
 <div class="graphs" id="ht-graphs">
 {legend}
 {graphs}
 </div>
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
  var range = (m && ['hour','quarter','day','week'].indexOf(m[1]) >= 0) ? m[1] : 'hour';

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

  /* Бейдж нормы: ✓ зелёный (норма) / ⚠ жёлтый (вне оптимума, в допустимой)
     / ⚠ красный (вне допустимой; для влажности: ниже — жёлтый, выше — красный) */
  function comfortSpan(status, rangeTxt, accTxt){{
    var sp = document.createElement('span');
    var mark = '⚠', title;
    if (status === 'ok') {{
      sp.className = 'st_ok'; mark = '✓';
      title = 'норма ' + rangeTxt;
    }} else {{
      var low = status.indexOf('low') >= 0;
      var where = low ? 'ниже' : 'выше';
      if (accTxt !== rangeTxt) {{
        if (status.indexOf('warn') === 0) {{
          sp.className = 'st_warn';
          title = where + ' оптимальной ' + rangeTxt + ' (допустимо ' + accTxt + ')';
        }} else {{
          sp.className = 'st_bad';
          title = where + ' допустимой ' + accTxt;
        }}
      }} else {{
        sp.className = low ? 'st_warn' : 'st_bad';
        title = where + ' нормы ' + rangeTxt;
      }}
    }}
    sp.textContent = mark;
    sp.title = title;
    return sp;
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
    initReorder();
    var all = graphs.querySelectorAll('svg[data-pts]');
    for (var i = 0; i < all.length; i++) {{
      all[i].addEventListener('mousemove', onMove);
      all[i].addEventListener('mouseleave', clearCross);
    }}
    graphs.addEventListener('mouseleave', clearCross);
  }}

  /* Перестановка графиков: верхняя карточка — на всю ширину, две нижние —
     рядом. Стрелка ▲ на карточке поднимает её наверх и растягивает; бывший
     верхний график опускается на её прежнее место. Порядок хранится локально
     (переживает SSE-обновления и перезагрузку страницы). */
  var topGraph = null;
  var svTop = null;
  try {{ svTop = localStorage.getItem('ht2000_top'); }} catch(e) {{}}
  if (svTop && ['g-co2','g-temp','g-rh'].indexOf(svTop) >= 0) topGraph = svTop;

  function promoKey(card){{
    return card.classList.contains('g-temp') ? 'g-temp' :
           card.classList.contains('g-rh') ? 'g-rh' : 'g-co2';
  }}

  /* Выставить одну полную ширину верхней карточке + скрыть её стрелку. */
  function applyGraphTop(){{
    var cards = graphs.querySelectorAll('.graph');
    for (var i = 0; i < cards.length; i++) cards[i].classList.remove('g-wide');
    if (cards.length) cards[0].classList.add('g-wide');
    for (var i = 0; i < cards.length; i++) {{
      var btn = cards[i].querySelector('.gh-up');
      if (btn) btn.style.visibility = (i === 0) ? 'hidden' : 'visible';
    }}
  }}

  /* Применить сохранённый порядок после подмены граф-секции (SSE/начало). */
  function restoreGraphOrder(){{
    if (!topGraph) {{ applyGraphTop(); return; }}
    var cards = graphs.querySelectorAll('.graph');
    for (var i = 0; i < cards.length; i++) {{
      if (cards[i].classList.contains(topGraph)) {{
        if (i !== 0) graphs.insertBefore(cards[i], cards[0]);
        break;
      }}
    }}
    applyGraphTop();
  }}

  function promoteGraph(btn){{
    var card = btn.closest('.graph');
    if (!card || card === graphs.querySelector('.graph')) return; // уже наверху
    topGraph = promoKey(card);
    try {{ localStorage.setItem('ht2000_top', topGraph); }} catch(e) {{}}
    restoreGraphOrder();
  }}

  /* Привязать стрелки перестановки после каждой подмены граф-секции. */
  function initReorder(){{
    var btns = graphs.querySelectorAll('.gh-up');
    for (var i = 0; i < btns.length; i++) {{
      btns[i].addEventListener('click', function(){{ promoteGraph(this); }});
    }}
    restoreGraphOrder();
  }}

  /* Обновление по SSE: сервер сам пушит события snapshot (1 с) и
     graphs (5 с) — клиент не делает периодических запросов. */
  function parseAge(sTime){{
    if (!sTime) return null;
    var t = Date.parse(sTime);
    return isNaN(t) ? null : Date.now() - t;
  }}

  function fmtAge(ms){{
    if (ms < 60000) return Math.round(ms / 1000) + ' с назад';
    return Math.round(ms / 60000) + ' мин назад';
  }}

  function applyData(d){{
    var cell = document.getElementById('ht-co2');
    if (d.co2 !== undefined && d.co2 !== null) {{
      cell.textContent = d.co2 + ' ppm';
      cell.className = co2Color(d.co2) === '{red}' ? 'co2_r' :
                       co2Color(d.co2) === '{yellow}' ? 'co2_y' : 'co2_g';
    }}
    var t = document.getElementById('ht-temp');
    if (d.temperature !== undefined && d.temperature !== null){{
      t.textContent = d.temperature.toFixed(1) + ' °C ';
      if (d.temp_status) t.appendChild(comfortSpan(d.temp_status, d.temp_range_txt, d.temp_acc_range_txt));
    }}
    var h = document.getElementById('ht-hum');
    if (d.humidity !== undefined && d.humidity !== null){{
      h.textContent = d.humidity.toFixed(1) + ' % ';
      if (d.rh_status) h.appendChild(comfortSpan(d.rh_status, d.rh_range_txt, d.rh_acc_range_txt));
    }}
    /* Строки норм под значениями карточек */
    var tn = document.getElementById('ht-temp-range');
    if (d.temp_range_txt){{
      tn.textContent = 'норма ' + d.temp_range_txt +
        (d.temp_range_txt !== d.temp_acc_range_txt ? ' · доп ' + d.temp_acc_range_txt : '');
    }}
    var hn = document.getElementById('ht-hum-range');
    if (d.rh_range_txt){{
      hn.textContent = 'норма ' + d.rh_range_txt +
        (d.rh_range_txt !== d.rh_acc_range_txt ? ' · доп ' + d.rh_acc_range_txt : '');
    }}
    var st = document.getElementById('ht-status');
    var age = parseAge(d.time);
    if (d.connected && age !== null && age < 180000) {{
      /* Абсолютное время, без относительного ("N мин назад"). */
      st.textContent = 'обновлено: ' + d.time.substring(0, 19).replace('T', ' ');
      st.className = 'status';
    }} else if (age !== null) {{
      st.textContent = 'связь потеряна · данные ' + fmtAge(age);
      st.className = 'status warn';
    }} else {{
      st.textContent = 'нет связи с устройством';
      st.className = 'status warn';
    }}
    if (d.co2) setFavicon(d.co2);
  }}

  function applyGraphs(sHtml){{
    if (!sHtml) return;
    graphs.innerHTML = sHtml;
    initGraphs();
  }}

  function applySnapshot(d){{
    if (d.html) {{ graphs.innerHTML = d.html; initGraphs(); }}
  }}

  function startEvents(){{
    var es = new EventSource('/events?range=' + range);
    es.addEventListener('snapshot', function(e){{
      applyData(JSON.parse(e.data));
    }});
    es.addEventListener('graphs', function(e){{
      applyGraphs(e.data);
    }});
  }}

  /* Страховка для окружений без SSE: старый опрос по таймеру. */
  function fallbackPoll(){{
    fetch('/data').then(function(r){{ return r.json(); }}).then(applyData).catch(function(){{}});
    fetch('/graphs?range=' + range).then(function(r){{ return r.json(); }}).then(applySnapshot).catch(function(){{}});
  }}

  initGraphs();
  if (typeof EventSource === 'undefined') {{
    /* Нет SSE — старый опрос по таймеру. */
    setInterval(fallbackPoll, REFRESH_MS);
    fallbackPoll();
  }} else {{
    startEvents();
  }}
}})(window);
</script>
</body>
</html>
"""

GRAPH_TEMPLATE = u"""<div class="graph{cls}">
<h2>{title}<button class="gh-up" type="button" title="поднять график вверх">▲</button></h2>
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
    """Метка времени на оси: часы для 'hour'/'quarter', с датой для 'day'/'week'."""
    if sRange in ("hour", "quarter"):
        return oDt.strftime("%H:%M")
    return oDt.strftime("%d.%m %H:%M")


def render_graph(sTitle, aRows, nValueIndex, sColor, sValueFormat, sRange,
                 aVerticals=None, aBand=None, aBandAcc=None, sCls=""):
    """SVG-график одной величины из строк read_recent.

    aRows — список (datetime, co2, temp, rh); nValueIndex — индекс величины;
    aVerticals — временные отметки (datetime) проветривания: вертикальные
    пунктирные линии + подсветка интервала после каждой;
    aBand — оптимальная зона нормы (нижняя, верхняя) в единицах величины;
    aBandAcc — допустимая зона нормы (бледная полоса позади оптимальной);
    sCls — дополнительный CSS-класс карточки графика (g-co2/g-temp/g-rh;
    g-wide — на всю ширину для верхней карточки).
    Зоны — по comfort.py."""
    if len(aRows) < 2:
        return GRAPH_TEMPLATE.format(
            cls=" " + sCls if sCls else "",
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

    # Зона нормы: допустимая — бледная полоса позади, оптимальная — ярче.
    sBand = ""

    def _band(dLow, dHigh, sFill):
        dBandTop = min(max(fY(dHigh), dY0), dY1)
        dBandBot = min(max(fY(dLow), dY0), dY1)
        if dBandBot - dBandTop > 0.5:
            return '<rect x="{0}" y="{1:.1f}" width="{2}" height="{3:.1f}" ' \
                   'fill="{4}"/>'.format(dX0, dBandTop, dX1 - dX0,
                                         dBandBot - dBandTop, sFill)
        return ""

    if aBandAcc is not None and aBandAcc != aBand:
        sBand += _band(aBandAcc[0], aBandAcc[1], BAND_ACC_FILL)
    if aBand is not None:
        sBand += _band(aBand[0], aBand[1], BAND_FILL)

    aPts = ["{0:.1f},{1:.1f}".format(fX(oRow[0]), fY(oRow[nValueIndex])) for oRow in aRows]
    sLine = "<polyline fill=\"none\" stroke=\"{0}\" stroke-width=\"1.5\" " \
            "points=\"{1}\"/>".format(sColor, " ".join(aPts))

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
            'xmlns="http://www.w3.org/2000/svg"{5}>{2}{3}{4}{6}</svg>').format(
        SVG_WIDTH, SVG_HEIGHT, sBand + sVentRects, sVerticals, sLine,
        sDataAttr, sLabels)
    sStats = "мин {0} · сред {1} · макс {2}".format(
        ("{0:" + sValueFormat + "}").format(min(aVals)),
        ("{0:" + sValueFormat + "}").format(dAvg),
        ("{0:" + sValueFormat + "}").format(max(aVals)))
    if aBand is not None:
        # Норма видна и тогда, когда зона не пересекает шкалу графика.
        if aBandAcc is not None and aBandAcc != aBand:
            sStats += " · норма {0:.0f}–{1:.0f} (допустимо {2:.0f}–{3:.0f})".format(
                aBand[0], aBand[1], aBandAcc[0], aBandAcc[1])
        else:
            sStats += " · норма {0:.0f}–{1:.0f}".format(aBand[0], aBand[1])
    return GRAPH_TEMPLATE.format(cls=" " + sCls if sCls else "",
                                 title=sTitle, body=sSvg, stats=sStats)


def render_graphs(aRows, sRange, aVerticals=None, oNow=None):
    """Три SVG-графика (CO2, температура, влажность) для страницы.

    Карточки получают классы g-co2 (на всю ширину), g-temp и g-rh
    (рядом, по половине). На графике температуры — зоны нормы
    (оптимальная и допустимая), на графике влажности — одна зона
    (см. comfort.py; сезон по oNow)."""
    if oNow is None:
        oNow = datetime.now()
    sCo2 = render_graph("CO₂, ppm", aRows, 1, GRAPH_CO2_COLOR, ".0f", sRange,
                        aVerticals, sCls="g-co2 g-wide")
    oTOpt, oTAcc = temperature_ranges(oNow)
    sTemp = render_graph("Температура, °C", aRows, 2, GRAPH_TEMP_COLOR, ".1f",
                         sRange, aVerticals,
                         aBand=oTOpt, aBandAcc=oTAcc, sCls="g-temp")
    oHOpt, oHAcc = humidity_ranges(oNow)
    sRh = render_graph("Влажность, %", aRows, 3, GRAPH_RH_COLOR, ".1f",
                       sRange, aVerticals,
                       aBand=oHOpt if oHOpt == oHAcc else oHAcc,
                       aBandAcc=None if oHOpt == oHAcc else oHOpt,
                       sCls="g-rh")
    return sCo2 + sTemp + sRh


def _empty_graphs():
    aEmpty = ""
    for sTitle, sCls in (("CO₂, ppm", "g-co2 g-wide"),
                         ("Температура, °C", "g-temp"),
                         ("Влажность, %", "g-rh")):
        aEmpty += GRAPH_TEMPLATE.format(
            cls=" " + sCls,
            title=sTitle, body='<div class="empty">нет данных за период</div>',
            stats="")
    return aEmpty


def graphs_fragment(sDataDir, sRange, oNow=None):
    """HTML-фрагмент секции графиков (легенда + три графика).

    Читает CSV и рендерит; для готовых строк есть graphs_from_rows().
    Возвращает строку для вставки в страницу или в JSON /graphs."""
    aRows = []
    if sDataDir:
        aRows = read_recent(sDataDir, RANGE_HOURS[sRange])
    return graphs_from_rows(aRows, sRange, oNow)


def graphs_from_rows(aRows, sRange, oNow=None):
    """Легенда + три графика по готовым строкам (одно чтение CSV на цикл).

    aRows — строки read_recent (уже отфильтрованные по окну); sRange —
    'hour' | 'day' | 'week'. Используется SSE-потоком для кэша графиков."""
    if sRange not in RANGES:
        sRange = "hour"
    if oNow is None:
        oNow = datetime.now()
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


def comfort_range_txt(oRange, sUnit):
    """Текст диапазона нормы: '22–24 °C' или '40–60 %'."""
    return "{0:.0f}–{1:.0f} {2}".format(oRange[0], oRange[1], sUnit)


def comfort_badge(sValue, sStatus, sOptTxt, sAccTxt=None):
    """Значение + бейдж статуса нормы ('24.5 °C <span …>⚠</span>').

    ✓ зелёный — ok; ⚠ жёлтый (st_warn) — вне оптимума, но в допустимой зоне
    (для влажности — ниже нормы); ⚠ красный (st_bad) — вне допустимой зоны
    (для влажности — выше нормы). Подсказка title — направление и зоны."""
    if sStatus == "ok":
        return ('{0} <span class="st_ok" title="норма {1}">✓</span>'.format(
            sValue, sOptTxt))
    bLow = sStatus.endswith("low")
    sWhere = "ниже" if bLow else "выше"
    if sStatus.startswith("warn"):
        sTitle = "{0} оптимальной {1}".format(sWhere, sOptTxt)
        if sAccTxt is not None and sAccTxt != sOptTxt:
            sTitle += " (допустимо {0})".format(sAccTxt)
        return ('{0} <span class="st_warn" title="{1}">⚠</span>'.format(
            sValue, sTitle))
    if sAccTxt is not None and sAccTxt != sOptTxt:
        sTitle = "{0} допустимой {1}".format(sWhere, sAccTxt)
        sClass = "st_bad"
    else:
        # Одна зона (влажность): ниже нормы — жёлтый, выше — красный.
        sTitle = "{0} нормы {1}".format(sWhere, sOptTxt)
        sClass = "st_warn" if sStatus == "low" else "st_bad"
    return ('{0} <span class="{1}" title="{2}">⚠</span>'.format(
        sValue, sClass, sTitle))


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
        sTempRng = ""
        sHumRng = ""
        sStatus = "нет связи с устройством"
    else:
        nCo2 = oSnapshot["co2"]
        sCo2 = "{0} ppm".format(nCo2)
        sClass = co2_css_class(nCo2)
        oStatus = comfort_status(oWhen, oSnapshot["temperature"],
                                 oSnapshot["humidity"])
        oTOpt, oTAcc = temperature_ranges(oWhen)
        oHOpt, oHAcc = humidity_ranges(oWhen)
        sTemp = comfort_badge(
            "{0:.1f} °C".format(oSnapshot["temperature"]),
            oStatus["temperature"],
            comfort_range_txt(oTOpt, "°C"),
            comfort_range_txt(oTAcc, "°C"))
        sHum = comfort_badge(
            "{0:.1f} %".format(oSnapshot["humidity"]),
            oStatus["humidity"],
            comfort_range_txt(oHOpt, "%"),
            comfort_range_txt(oHAcc, "%"))
        # Строки норм в карточках (JS обновляет их по /data каждый цикл).
        sTempRng = _norm_note(oTOpt, oTAcc, "°C")
        sHumRng = _norm_note(oHOpt, oHAcc, "%")
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
        temp_range=sTempRng,
        hum_range=sHumRng,
        status=sStatus,
        cls_hour="active" if sRange == "hour" else "",
        cls_quarter="active" if sRange == "quarter" else "",
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


def _norm_note(oOpt, oAcc, sUnit):
    """Строка норм для карточки: 'норма 22–24 °C · доп 20–25 °C'."""
    sNote = "норма {0}".format(comfort_range_txt(oOpt, sUnit))
    if oAcc != oOpt:
        sNote += " · доп {0}".format(comfort_range_txt(oAcc, sUnit))
    return sNote


def format_json(oSnapshot):
    """Снимок в JSON (bytes)."""
    return json.dumps(oSnapshot if oSnapshot is not None
                      else {"connected": False}).encode("utf-8")


def snapshot_with_comfort(oSnapshot):
    """Добавляет в снимок статусы норм T/RH и тексты зон норм.

    Статусы температуры: ok / warn_low / warn_high / bad_low / bad_high;
    влажности: ok / low / high. Сезон определяется по текущей дате —
    JS-обновление страницы каждые 5 секунд автоматически переключает
    нормы при смене сезона."""
    if not oSnapshot:
        return oSnapshot
    oWhen = datetime.now()
    oStatus = comfort_status(oWhen, oSnapshot["temperature"],
                             oSnapshot["humidity"])
    oTOpt, oTAcc = temperature_ranges(oWhen)
    oHOpt, oHAcc = humidity_ranges(oWhen)
    oRes = dict(oSnapshot)
    oRes["temp_status"] = oStatus["temperature"]
    oRes["rh_status"] = oStatus["humidity"]
    oRes["temp_range_txt"] = comfort_range_txt(oTOpt, "°C")
    oRes["temp_acc_range_txt"] = comfort_range_txt(oTAcc, "°C")
    oRes["rh_range_txt"] = comfort_range_txt(oHOpt, "%")
    oRes["rh_acc_range_txt"] = comfort_range_txt(oHAcc, "%")
    return oRes


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
        # SSE-кэш: последний снимок и фрагменты графиков по диапазонам.
        # Каждое значение — (версия, данные); версия меняется при обновлении.
        self._condition = threading.Condition()
        self._cache = {
            "snapshot": (0, None),
            "graphs": {sRange: (0, "") for sRange in RANGES},
        }
        self._pushThread = None
        self._pushStop = threading.Event()

    # -- Поток-генератор SSE ----------------------------------------------

    def _push_loop(self):
        """Фоновый поток: готовит снимок (1 с) и графики (5 с), будит /events.

        Графики рендерятся один раз на цикл (одно чтение CSV-файлов за
        неделю), затем нарезаются по окнам hour/day/week."""
        dNextSnapshot = time.monotonic()
        dNextGraphs = time.monotonic()
        while not self._pushStop.is_set():
            oTick = time.monotonic()
            try:
                if oTick >= dNextSnapshot:
                    dNextSnapshot = oTick + SSE_SNAPSHOT_SECONDS
                    oSnap = self.fnSnapshot()
                    if oSnap is not None:
                        oSnap = snapshot_with_comfort(oSnap)
                    with self._condition:
                        nVer = self._cache["snapshot"][0] + 1
                        self._cache["snapshot"] = (nVer, oSnap)
                        self._condition.notify_all()
                if oTick >= dNextGraphs and self.sDataDir:
                    dNextGraphs = oTick + SSE_GRAPHS_SECONDS
                    oNow = datetime.now()
                    aRows = read_recent(self.sDataDir, RANGE_HOURS["week"])
                    with self._condition:
                        for sRange in RANGES:
                            oCutoff = oNow - timedelta(hours=RANGE_HOURS[sRange])
                            aWin = [oRow for oRow in aRows if oRow[0] >= oCutoff]
                            sHtml = graphs_from_rows(aWin, sRange, oNow)
                            oPrev = self._cache["graphs"][sRange]
                            self._cache["graphs"][sRange] = (oPrev[0] + 1, sHtml)
                        self._condition.notify_all()
            except Exception as oExc:   # поток не должен умирать
                sys.stderr.write("[ht2000-web] push: {0}\n".format(oExc))
            self._pushStop.wait(SSE_SNAPSHOT_SECONDS)

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
                                           self.fnSnapshot, self.sDataDir, self)
            except OSError:
                continue    # порт занят — пробуем следующий
            self.nPort = nPort
            break
        if self.httpd is None:
            raise HT2000WebError(
                "не удалось занять порты {0}..{1}".format(
                    self.nStartPort, self.nStartPort + self.nMaxAttempts - 1))
        self._pushStop.clear()
        self._pushThread = threading.Thread(target=self._push_loop,
                                            name="ht2000-web-push")
        self._pushThread.daemon = True
        self._pushThread.start()
        self.oThread = threading.Thread(target=self.httpd.serve_forever, name="ht2000-web")
        self.oThread.daemon = True
        self.oThread.start()
        return True

    def stop(self):
        """Останавливает сервер. Идемпотентна: повторный вызов безопасен."""
        if self.httpd is None:
            return
        self._pushStop.set()
        with self._condition:
            self._condition.notify_all()
        if self._pushThread is not None:
            self._pushThread.join(timeout=2.0)
            self._pushThread = None
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        self.nPort = None
        self.oThread = None

    def is_running(self):
        return self.httpd is not None


class _HT2000Server(ThreadingHTTPServer):
    """ThreadingHTTPServer, несущий колбек снимка, каталог данных и кэш SSE."""
    daemon_threads = True

    def __init__(self, oAddress, oHandlerClass, fnSnapshot, sDataDir, oOwner):
        self.fnSnapshot = fnSnapshot
        self.sDataDir = sDataDir
        self.oOwner = oOwner      # HT2000WebServer: _cache и _condition
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
        elif oUrl.path == "/events":
            self._stream_events(self._range(oUrl))
        elif oUrl.path == "/graphs":
            sRange = self._range(oUrl)
            sHtml = graphs_fragment(self.server.sDataDir, sRange)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"html": sHtml}).encode("utf-8"))
        elif oUrl.path == "/data":
            self._send(200, "application/json; charset=utf-8",
                       format_json(snapshot_with_comfort(oSnapshot)))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _range(self, oUrl):
        aRanges = parse_qs(oUrl.query).get("range")
        if aRanges and aRanges[0] in RANGES:
            return aRanges[0]
        return "hour"

    def _stream_events(self, sRange):
        """SSE-поток: события snapshot (1 с) и graphs (5 с) из кэша сервера.

        При подключении сразу отдаёт текущее состояние (важно для
        авто-переподключения EventSource). Пишет chunked, чтобы не
        закрывать соединение между событиями. Обрыв клиента — исключение
        при записи — поток хендлера завершается."""
        # HTTP/1.0 закрывает соединение после do_GET — для потока нужен 1.1.
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        oOwner = self.server.oOwner
        oCache = oOwner._cache
        oCondition = oOwner._condition

        def oChunk(oBody):
            self.wfile.write(("{0:x}\r\n".format(len(oBody))).encode("ascii"))
            self.wfile.write(oBody)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        def oEvent(sName, sData):
            # В SSE каждая строка HTML должна иметь свой префикс data:.
            sPayload = "data: " + sData.replace("\n", "\ndata: ")
            oChunk(("event: {0}\n{1}\n\n".format(
                sName, sPayload)).encode("utf-8"))

        nLastSnapshot = -1
        nLastGraphs = -1
        try:
            while True:
                oGraphsVer, sGraphs = oCache["graphs"][sRange]
                oSnapVer, oSnap = oCache["snapshot"]
                if oSnapVer != nLastSnapshot:
                    nLastSnapshot = oSnapVer
                    oEvent("snapshot", format_json(oSnap).decode("utf-8"))
                if oGraphsVer != nLastGraphs:
                    nLastGraphs = oGraphsVer
                    oEvent("graphs", sGraphs)
                with oCondition:
                    oCondition.wait(SSE_SNAPSHOT_SECONDS)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass    # клиент закрыл соединение

    def log_message(self, sFormat, *aArgs):
        sys.stderr.write("[ht2000-web] {0} {1}\n".format(
            self.address_string(), sFormat % aArgs))

    def _send(self, nCode, sContentType, oBody):
        self.send_response(nCode)
        self.send_header("Content-Type", sContentType)
        self.send_header("Content-Length", str(len(oBody)))
        self.end_headers()
        self.wfile.write(oBody)