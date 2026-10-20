#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика пропусков данных непрерывного мониторинга HT2000.

Анализирует CSV-файлы измерений ``data/ht2000_*.csv`` и находит интервалы,
в которых записи отсутствуют дольше пороговой паузы. Модуль не зависит от
GUI и сети, покрыт тестами (tests/test_data_gaps.py). Назначение — выяснить,
в какие периоды и насколько долго данные мониторинга пропадали.

Логика:

- ``load_rows(sDataDir)`` — собрать все валидные строки ``(datetime, co2,
  temp, rh)`` из всех ``data/ht2000_*.csv`` и отсортировать по времени.
- ``find_gaps(aRows, nMaxGapMin=2, oStart=None, oEnd=None)`` — найти
  непрерывные окна ``[oFrom, oTo]`` (минуты), где данных нет дольше порога.
  Опционально ограничить окно ``oStart..oEnd`` (например, «последние
  N часов»).
"""

import csv
import glob
import os
import sys
from datetime import datetime, timedelta

DEFAULT_MAX_GAP_MIN = 2
HEADER_N_COLS = 4


def load_rows(sDataDir):
    """Читает строки из всех ``data/ht2000_*.csv`` в каталоге.

    Возвращает отсортированный список кортежей
    ``(datetime, co2, temp, rh)``. Битые строки и недоступные файлы
    пропускаются. CO2 возвращается как int, T/RH как float (префиксы
    d/float), чтобы соответствовать реальным данным мониторинга.
    """
    aRows = []
    for sPath in sorted(glob.glob(os.path.join(sDataDir, "ht2000_*.csv"))):
        try:
            oFile = open(sPath, newline="")
        except OSError:
            continue
        try:
            for aLine in csv.reader(oFile, delimiter=";"):
                if len(aLine) < HEADER_N_COLS:
                    continue
                try:
                    oDt = datetime.strptime(aLine[0], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if aLine[1] in ("", "co2_ppm"):
                    continue
                try:
                    nCo2 = int(aLine[1])
                    dTemp = float(aLine[2])
                    dRh = float(aLine[3])
                except ValueError:
                    continue
                aRows.append((oDt, nCo2, dTemp, dRh))
        finally:
            oFile.close()
    aRows.sort(key=lambda oRow: oRow[0])
    return aRows


def find_gaps(aRows, nMaxGapMin=DEFAULT_MAX_GAP_MIN,
              oStart=None, oEnd=None):
    """Находит интервалы отсутствия данных дольше ``nMaxGapMin`` минут.

    Каждая строка данных принадлежит «своей» минуте (округление вниз до
    начала минуты). Пропуском считается расстояние между минутой
    предыдущей и минутой следующей строки, если оно строго больше порога
    ``nMaxGapMin``.

    Параметры:
        aRows — строки ``(datetime, co2, temp, rh)`` от ``load_rows``;
        nMaxGapMin — минимальная пауза (мин), считающаяся пропуском;
        oStart, oEnd — опциональные границы окна анализа (datetime),
            за пределами которого данные не учитываются.

    Возвращает список словарей::

        {
            "from": datetime,   # первая пропущенная минута (включительно)
            "to":   datetime,   # последняя пропущенная минута (включительно)
            "minutes": int,     # длительность пропуска в минутах
        }

    Пропуски отсортированы по времени; окно ``oStart..oEnd`` анализируется
    как непрерывная минута за минутой, поэтому краевые пропуски в начале и
    конце интервала учитываются полностью.
    """
    aPresent = set()
    for oRow in aRows:
        oDt = oRow[0].replace(second=0, microsecond=0)
        aPresent.add(oDt)

    if oStart is None:
        oStart = min(aPresent) if aPresent else datetime.now()
    if oEnd is None:
        oEnd = max(aPresent) if aPresent else oStart

    # Анализируем только минуты внутри окна.
    oStartMin = oStart.replace(second=0, microsecond=0)
    if oEnd < oStartMin:
        return []
    nTotalMin = int((oEnd - oStartMin).total_seconds() // 60) + 1

    aGaps = []
    oGapFrom = None
    for nIdx in range(nTotalMin):
        oMin = oStartMin + timedelta(minutes=nIdx)
        if oMin in aPresent:
            if oGapFrom is not None:
                aGaps.append(_make_gap(oGapFrom, oMin - timedelta(minutes=1)))
                oGapFrom = None
        else:
            if oGapFrom is None:
                oGapFrom = oMin
    if oGapFrom is not None:
        aGaps.append(_make_gap(oGapFrom, oEnd))
    # Не показываем пропуски короче или равные порогу (округление вверх).
    # Пауза измеряется как расстояние между минутами соседних строк.
    aGaps = [oG for oG in aGaps if oG["minutes"] > nMaxGapMin]
    return aGaps


def _make_gap(oFrom, oTo):
    """Собирает словарь пропуска; ``oTo >= oFrom``."""
    nMin = int((oTo - oFrom).total_seconds() // 60) + 1
    return {"from": oFrom, "to": oTo, "minutes": nMin}


def summarize_gaps(aGaps, oStart=None, oEnd=None):
    """Собирает сводную статистику пропусков для отчёта.

    Параметры:
        aGaps — список словарей от ``find_gaps``;
        oStart, oEnd — опциональные границы окна (для расчёта доли).

    Возвращает словарь::

        {
            "count": int,      # число непрерывных пропусков
            "minutes": int,    # суммарно пропущенных минут
            "window_minutes": int,      # минут в окне анализа
            "window_present": int,      # минут с данными
            "percent": float,           # доля пропусков (0..100)
            "gaps": [... кратно find_gaps ...],
        }
    """
    aGaps = list(aGaps)
    nMissed = sum(oG["minutes"] for oG in aGaps)

    if oStart is None or oEnd is None:
        if not aGaps:
            return {"count": 0, "minutes": 0, "window_minutes": 0,
                    "window_present": 0, "percent": 0.0, "gaps": []}
        oStart = min(oG["from"] for oG in aGaps)
        oEnd = max(oG["to"] for oG in aGaps)
    nWindow = max(0, int((oEnd.replace(second=0, microsecond=0)
                          - oStart.replace(second=0, microsecond=0)
                          ).total_seconds() // 60) + 1)
    nPresent = max(0, nWindow - nMissed)
    dPercent = (nMissed / nWindow * 100.0) if nWindow else 0.0
    return {
        "count": len(aGaps),
        "minutes": nMissed,
        "window_minutes": nWindow,
        "window_present": nPresent,
        "percent": round(dPercent, 1),
        "gaps": aGaps,
    }


def _fmt(oDt):
    """Формат минуты для вывода (ГГГГ-ММ-ДД ЧЧ:ММ)."""
    return oDt.strftime("%Y-%m-%d %H:%M")


def report(aStats, oHow="активна", sSuffix=""):
    """Человекочитаемый отчёт о пропусках для stderr/stdout.

    Параметры:
        aStats — словарь от ``summarize_gaps``;
        oHow — описание того, что анализировалось;
        sSuffix — текст, дописываемый к каждой строке пропуска (причина).
    """
    if aStats["count"] == 0:
        sys.stderr.write("Пропусков данных не выявлено.\n")
        return
    sys.stderr.write(
        "Пропусков: {0}, суммарно {1} из {2} минут "
        "({3}%). {4} {5} есть данные.\n".format(
            aStats["count"], aStats["minutes"], aStats["window_minutes"],
            aStats["percent"], aStats["window_present"], oHow))
    for oG in aStats["gaps"]:
        sys.stderr.write(
            "  {0} -- {1}  ({2} мин) {3}\n".format(
                _fmt(oG["from"]), _fmt(oG["to"]), oG["minutes"], sSuffix))


def main(sDataDir=None, nHoursBack=None):
    """CLI-вход: анализ пропусков за все данные или за последние N часов.

    Примеры:
        python3 data_gaps.py                     # весь архив
        python3 data_gaps.py --hours 12          # последние 12 часов
    """
    if sDataDir is None:
        sDataDir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data")
    aRows = load_rows(sDataDir)
    if nHoursBack is not None:
        oEnd = datetime.now()
        oStart = oEnd - timedelta(hours=nHoursBack)
    else:
        oStart = oEnd = None
    aGaps = find_gaps(aRows, oStart=oStart, oEnd=oEnd)
    aStats = summarize_gaps(aGaps, oStart, oEnd)
    sHow = "за последние {0} ч".format(nHoursBack) if nHoursBack is not None \
        else "в архиве"
    report(aStats, oHow=sHow)


if __name__ == "__main__":
    import argparse

    oParser = argparse.ArgumentParser(
        description="Анализ пропусков данных мониторинга HT2000.")
    oParser.add_argument("--data-dir", default=None,
                         help="каталог c CSV (по умолчанию data/ рядом)")
    oParser.add_argument("--hours", type=int, default=None,
                         help="анализировать только последние N часов")
    oArgs = oParser.parse_args()
    main(sDataDir=oArgs.data_dir, nHoursBack=oArgs.hours)