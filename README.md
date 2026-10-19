# HT2000 Monitor

Выводит в системный трей данные с CO2-монитора SLAB HT2000 (VID 0x10c4,
PID 0x82cd, USB HID) тремя отдельными иконками: **CO2, температура, влажность**,
и раз в минуту пишет их в CSV-файл.

Основано на проекте [trayinfo](https://github.com/mik/trayinfo) (панель часов,
цвета иконок, отрисовка графиков).

## Протокол устройства

HT2000 общается через HID *feature reports* (ioctl `HIDIOCSFEATURE` /
`HIDIOCGFEATURE`), обычный write/read в hidraw не работает:

1. `Set Feature`, report 0x05, данные `ff ff ff` — запрос свежего снимка;
2. `Get Feature`, report 0x05 — ответ ≥ 30 байт:
   - байты `[7:9]` (big-endian) — температура, `(v - 400) / 10` °C;
   - байты `[9:11]` (big-endian) — влажность, `v / 10` %;
   - байты `[24:26]` (big-endian) — CO2, ppm.

Протокол реверс-инжинирирован сообществом
(https://github.com/tomvanbraeckel/slab_ht2000), проверен на живом устройстве.

## Установка прав на устройство

`/dev/hidraw*` по умолчанию доступен только root. Один раз:

```bash
sudo cp 99-ht2000.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Запуск

```bash
python3 ht2000_monitor.py
```

Данные пишутся в `data/ht2000_YYYY-MM.csv` (разделитель `;`):

```
datetime;co2_ppm;temp_c;rh_percent
2026-10-19 12:20:01;715;25.7;30.0
```

## Тесты

```bash
python3 -m unittest discover -s tests -v
```

## Файлы

- `ht2000_reader.py` — чтение и разбор данных с устройства (без GUI);
- `ht2000_monitor.py` — приложение: три иконки в трее + запись в файл;
- `tests/test_reader.py` — тесты парсера и поиска устройства;
- `99-ht2000.rules` — udev-правило для доступа к hidraw без root.