# -*- coding: utf-8 -*-
"""Нормы микроклимата офиса с оптимальной и допустимой зонами.

Тёплый сезон — май–сентябрь, холодный — октябрь–апрель.

Температура (значения заданы пользователем):
- холодный сезон: оптимальная 22–24 °C, допустимая 20–25 °C;
- тёплый сезон: оптимальная 23–25 °C, допустимая 21–28 °C.

Относительная влажность: 40–60 % круглогодично (одна, «нормальная» зона).

Статусы температуры: 'ok' (в оптимальной зоне), 'warn_low'/'warn_high'
(вне оптимальной, но в допустимой), 'bad_low'/'bad_high' (вне допустимой).
Статусы влажности: 'ok', 'low', 'high' (допустимая зона = оптимальной,
поэтому 'low' → жёлтый, 'high' → красный бейдж/⚠).

Диапазоны переопределяются в config.json — см. configure_from_file().
"""

import datetime
import json
import os
import sys

WARM_SEASON_MONTHS = (5, 6, 7, 8, 9)

# Дефолты заданы пользователем (сверить со своей нормативной базой).
TEMP_OPT_COLD = (22.0, 24.0)
TEMP_ACC_COLD = (20.0, 25.0)
TEMP_OPT_WARM = (23.0, 25.0)
TEMP_ACC_WARM = (21.0, 28.0)
RH_RANGE = (40.0, 60.0)

_DEFAULT_RANGES = {
    "cold": {
        "temperature_optimal": TEMP_OPT_COLD,
        "temperature_acceptable": TEMP_ACC_COLD,
        "humidity_optimal": RH_RANGE,
        "humidity_acceptable": RH_RANGE,
    },
    "warm": {
        "temperature_optimal": TEMP_OPT_WARM,
        "temperature_acceptable": TEMP_ACC_WARM,
        "humidity_optimal": RH_RANGE,
        "humidity_acceptable": RH_RANGE,
    },
}

_ranges = None      # загруженный конфиг или None (дефолты)


def configure(oRanges=None):
    """Задаёт диапазоны норм напрямую (dict как в _DEFAULT_RANGES).

    None — сброс к дефолтам. Используется тестами и configure_from_file()."""
    global _ranges
    _ranges = oRanges


def _pair(oValue, dDefault):
    """Два числа [lo, hi] с lo <= hi, иначе dDefault."""
    try:
        dLo = float(oValue[0])
        dHi = float(oValue[1])
    except (TypeError, ValueError, KeyError, IndexError):
        return dDefault
    if dLo > dHi:
        return dDefault
    return (dLo, dHi)


def _apply(oJson):
    """Применяет словарь конфига, пропуская битые ключи."""
    if not isinstance(oJson, dict):
        return None
    oRanges = {}
    for sSeason in ("cold", "warm"):
        oS = oJson.get(sSeason)
        if not isinstance(oS, dict):
            oS = {}
        oDef = _DEFAULT_RANGES[sSeason]
        oRanges[sSeason] = {
            "temperature_optimal":
                _pair(oS.get("temperature_optimal"), oDef["temperature_optimal"]),
            "temperature_acceptable":
                _pair(oS.get("temperature_acceptable"), oDef["temperature_acceptable"]),
            "humidity_optimal":
                _pair(oS.get("humidity_optimal"), oDef["humidity_optimal"]),
            "humidity_acceptable":
                _pair(oS.get("humidity_acceptable"), oDef["humidity_acceptable"]),
        }
    configure(oRanges)
    return oRanges


def configure_from_file(sPath=None):
    """Загружает config.json с диапазонами норм.

    Пути поиска (если sPath не задан): ~/.config/ht2000/config.json, затем
    config.json рядом с comfort.py. Первый найденный файл выигрывает.
    Битый JSON — предупреждение в stderr и дефолты. Отсутствие файла —
    просто дефолты.

    Схема файла:
        {"cold": {"temperature_optimal": [22, 24],
                  "temperature_acceptable": [20, 25]},
         "warm": {...}}
    Ключи опциональны; допустимые диапазоны можно не указывать (тогда
    они равны оптимальным — так у влажности)."""
    aCandidates = [sPath] if sPath else [
        os.path.expanduser("~/.config/ht2000/config.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
    ]
    for sPath in aCandidates:
        try:
            with open(sPath, encoding="utf-8") as oFile:
                oJson = json.load(oFile)
        except OSError:
            continue
        except ValueError as oExc:
            sys.stderr.write("HT2000: config.json повреждён ({0}): {1}\n".format(
                sPath, oExc))
            return None
        return _apply(oJson)
    return None


def season(oDt):
    """Сезон для даты: 'cold' (октябрь–апрель) или 'warm' (май–сентябрь)."""
    return "warm" if oDt.month in WARM_SEASON_MONTHS else "cold"


def _season_ranges(oDt):
    if _ranges is not None:
        return _ranges[season(oDt)]
    return _DEFAULT_RANGES[season(oDt)]


def temperature_ranges(oDt):
    """Диапазоны нормы температуры (°C): (оптимальная, допустимая)."""
    oR = _season_ranges(oDt)
    return (oR["temperature_optimal"], oR["temperature_acceptable"])


def humidity_ranges(oDt):
    """Диапазоны нормы влажности (%): (оптимальная, допустимая).

    Для влажности задана одна зона, допустимая равна оптимальной."""
    oR = _season_ranges(oDt)
    return (oR["humidity_optimal"], oR["humidity_acceptable"])


def _status(dValue, oOpt, oAcc):
    if oOpt[0] <= dValue <= oOpt[1]:
        return "ok"
    if oAcc[0] <= dValue <= oAcc[1]:
        s = "warn" if oAcc != oOpt else "bad"
        return s + ("_low" if dValue < oOpt[0] else "_high")
    return "bad" + ("_low" if dValue < oAcc[0] else "_high")


def comfort_status(oDt, dTemp, dRh):
    """Статусы нормы температуры и влажности для момента времени.

    Возвращает dict с ключами 'temperature' и 'humidity'.
    Температура: ok / warn_low / warn_high / bad_low / bad_high.
    Влажность: ok / low / high (warn → low, т.к. зона одна)."""
    oTOpt, oTAcc = temperature_ranges(oDt)
    oHOpt, oHAcc = humidity_ranges(oDt)
    sTemp = _status(dTemp, oTOpt, oTAcc)
    sHum = _status(dRh, oHOpt, oHAcc)
    if oHOpt == oHAcc:
        # Допустимая зона влажности = оптимальной: вне нормы — «low»/«high»
        # (низ — жёлтый бейдж, верх — красный).
        if sHum.startswith("bad"):
            sHum = "low" if sHum.endswith("low") else "high"
    return {"temperature": sTemp, "humidity": sHum}


def comfort_tooltip(dTemp, dRh, oDt=None):
    """Две строки для хинта иконки: 'T: ...\\nRH: ...'.

    Примеры: 'T: 24.8 °C ⚠ (выше оптимальной 22–24 °C)',
    'RH: 45.0 % ✓ (норма 40–60 %)'."""
    if oDt is None:
        oDt = datetime.datetime.now()
    oStatus = comfort_status(oDt, dTemp, dRh)
    return "T: {0}\nRH: {1}".format(
        _hint_line(dTemp, "°C", temperature_ranges(oDt), oStatus["temperature"]),
        _hint_line(dRh, "%", humidity_ranges(oDt), oStatus["humidity"]))


def _hint_line(dValue, sUnit, oRanges, sStatus):
    """Одна строка хинта: значение, отметка статуса и зона."""
    oOpt, oAcc = oRanges
    if oAcc != oOpt:
        # Две зоны (температура): «оптимальная» и «допустимая».
        if sStatus == "ok":
            return "{0:.1f} {1} ✓ (оптимум {2:.0f}–{3:.0f})".format(
                dValue, sUnit, oOpt[0], oOpt[1])
        if sStatus.startswith("warn"):
            sWhere = "ниже" if sStatus.endswith("low") else "выше"
            return "{0:.1f} {1} ⚠ ({2} оптимальной {3:.0f}–{4:.0f})".format(
                dValue, sUnit, sWhere, oOpt[0], oOpt[1])
        sWhere = "ниже" if sStatus.endswith("low") else "выше"
        dLimit = oAcc[0] if sStatus.endswith("low") else oAcc[1]
        return "{0:.1f} {1} ⚠ ({2} допустимой {3:.0f})".format(
            dValue, sUnit, sWhere, dLimit)
    # Одна зона (влажность): просто «норма».
    sMark = "✓" if sStatus == "ok" else "⚠"
    if sStatus == "ok":
        return "{0:.1f} {1} ✓ (норма {2:.0f}–{3:.0f})".format(
            dValue, sUnit, oOpt[0], oOpt[1])
    sWhere = " ниже нормы" if sStatus.endswith("low") else " выше нормы"
    return "{0:.1f} {1} {2}{3} ({4:.0f}–{5:.0f})".format(
        dValue, sUnit, sMark, sWhere, oOpt[0], oOpt[1])