"""Магнит ККС: раскладка плоской таблицы по объектам ККС (IMP-13…16, MIG-16).

Модуль только планирует и ничего не пишет в базу. На входе — строки таблицы
(``{заголовок: значение}``) и роли столбцов; на выходе — план:

- **аппараты** — объекты ККС, к которым притянуты строки;
- **сигналы** — строки «строка на сигнал»: код сигнала, аппарат, кабель, места
  (шкафы, коробки) с контактами, канал ввода-вывода;
- **резервные каналы** — строки без кода сигнала с одним и тем же словом
  (`Spare`, «Резерв»): канал есть, сигнала нет;
- **кабели** — по номеру кабеля; место, общее для всех сигналов кабеля;
- **пропуски** — строки, где кроме ключевых столбцов ничего нет;
- **расхождения** — не сливаются молча (IMP-16): строка остаётся в плане и
  попадает в отчёт.

Разбор кода — через функцию ``split(code) -> (объект, признак | None)``,
то есть по правилам кода станции (DATA-16).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

Split = Callable[[str], tuple[str, str | None]]
Row = dict[str, Any]

# Похоже на ККС: необязательный номер блока, буквы системы, номера и буквы
# оборудования. Только для отчёта — объект создаётся в любом случае.
KKS_LIKE = re.compile(r"^[0-9]{0,3}[A-Z]{1,4}[0-9]{1,3}[A-Z]{1,3}[0-9]{1,4}[A-Z]?$")
SPARE_WORDS = {"spare", "резерв", "reserve", "rezerv"}
_NUMBER = re.compile(r"[0-9]+")


@dataclass
class Layout:
    """Роли столбцов таблицы сигналов."""

    kks: str | None = None  # ККС аппарата
    signal: str | None = None  # код сигнала
    cable: str | None = None  # номер кабеля
    places: dict[str, str] = field(default_factory=dict)  # столбец места → роль связи
    contacts: dict[str, list[str]] = field(default_factory=dict)  # место → его столбцы контактов
    channel: list[str] = field(default_factory=list)  # столбцы, из которых состоит канал
    service: list[str] = field(default_factory=list)  # столбцы без смысла для данных (№)

    def key_columns(self) -> set[str]:
        return {name for name in (self.kks, *self.service) if name}

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Issue:
    row: int
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"row": self.row, "code": self.code, "message": self.message}


@dataclass
class SignalRow:
    row: int
    kind: str  # signal | spare
    code: str | None
    device: str | None
    via: str | None  # откуда взят аппарат: kks | code
    cable: str | None
    channel: str | None
    values: Row


@dataclass
class SignalPlan:
    layout: Layout
    signals: list[SignalRow] = field(default_factory=list)
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    cables: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: list[int] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        kinds = Counter(item.kind for item in self.signals)
        via = Counter(item.via for item in self.signals if item.kind == "signal")
        return {
            "rows": len(self.signals) + len(self.skipped),
            "signals": kinds["signal"],
            "spares": kinds["spare"],
            "device_from_kks_column": via["kks"],
            "device_from_signal_code": via["code"],
            "devices": len(self.devices),
            "devices_not_kks_like": sorted(
                code for code, item in self.devices.items() if not item["kks_like"]
            ),
            "cables": len(self.cables),
            "cables_with_several_devices": sum(
                1 for item in self.cables.values() if len(item["devices"]) > 1
            ),
            "skipped_empty_rows": len(self.skipped),
            "issues": Counter(item.code for item in self.issues),
        }


@dataclass
class DevicePlan:
    kks: str
    rows: dict[str, tuple[int, Row]] = field(default_factory=dict)  # ККС → строка
    skipped: list[int] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "devices": len(self.rows),
            "skipped_empty_rows": len(self.skipped),
            "issues": Counter(item.code for item in self.issues),
        }


# Раскладка ---------------------------------------------------------------------


def plan_devices(
    rows: Iterable[tuple[int, Row]], kks: str, service: Iterable[str] = ()
) -> DevicePlan:
    """Реестр «строка — аппарат»: у каждого ККС одна строка.

    Повтор с теми же данными — одна запись. Повтор с другими данными остаётся
    первой строкой и попадает в расхождения: что верно, решает человек.
    """
    plan = DevicePlan(kks)
    ignored = {kks, *service}
    for number, row in rows:
        code = _text(row.get(kks))
        if code is None:
            if _has_data(row, ignored):
                plan.issues.append(Issue(number, "no_kks", "Строка с данными без ККС"))
            else:
                plan.skipped.append(number)
            continue
        key = code.upper()
        if key in plan.rows:
            first, earlier = plan.rows[key]
            if _data(earlier, ignored) != _data(row, ignored):
                plan.issues.append(
                    Issue(
                        number,
                        "duplicate_differs",
                        f"ККС {code} уже есть в строке {first} с другими данными",
                    )
                )
            continue
        plan.rows[key] = (number, row)
    return plan


def plan_signals(rows: Iterable[tuple[int, Row]], layout: Layout, split: Split) -> SignalPlan:
    """Таблица «строка на сигнал»: каждая строка притягивается к ККС аппарата."""
    if not layout.signal:
        raise ValueError("Для таблицы сигналов нужен столбец кода сигнала")
    plan = SignalPlan(layout)
    rows = list(rows)
    spare_words = _spare_words(rows, layout, split)
    ignored = layout.key_columns()
    channels: dict[str, int] = {}
    contacts: dict[tuple[str, str], int] = {}
    for number, row in rows:
        raw = _text(row.get(layout.signal))
        kks = _text(row.get(layout.kks)) if layout.kks else None
        if raw is None and not _has_data(row, ignored):
            plan.skipped.append(number)  # строка-заготовка: только ключ
            continue
        item = _signal(number, row, raw, kks, layout, split, spare_words, plan.issues)
        plan.signals.append(item)
        if item.device:
            device = plan.devices.setdefault(
                item.device.upper(),
                {
                    "code": item.device,
                    "signals": 0,
                    "kks_like": bool(KKS_LIKE.match(item.device.upper())),
                },
            )
            device["signals"] += 1
        _check_unique(item.channel, number, channels, "channel_duplicate", "канал", plan.issues)
        for place, columns in layout.contacts.items():
            shown = _contact(row, columns)
            spot = _text(row.get(place))
            if shown and spot:
                _check_unique(
                    (spot.upper(), shown),
                    number,
                    contacts,
                    "contact_duplicate",
                    f"подключение {place}",
                    plan.issues,
                )
        if item.cable:
            _add_cable(plan.cables, item, row, layout)
    _cable_places(plan.cables)
    return plan


def contact_text(row: Row, columns: list[str]) -> str | None:
    """Контакт связи для графа: «X25:11-12» из клеммника и двух клемм."""
    return _contact(row, columns)


def channel_text(row: Row, columns: list[str]) -> str | None:
    parts = [_text(row.get(name)) for name in columns]
    if not columns or any(part is None for part in parts):
        return None
    return " / ".join(part for part in parts if part)


# Роли столбцов (IMP-14) -----------------------------------------------------------


def suggest_layout(headers: list[str], rows: list[Row], split: Split) -> Layout:
    """Роли столбцов по заголовкам и содержимому; результат подтверждает человек."""
    folded = {name: _fold(name) for name in headers}
    layout = Layout(service=[name for name, low in folded.items() if low in ("№", "no", "n", "#")])
    for name, low in folded.items():
        if low in ("ккс сигнала", "kks сигнала", "signal kks", "kks signal", "код сигнала"):
            layout.signal = name
        elif low in ("ккс", "kks", "ккс аппарата", "fld_dev") and layout.kks is None:
            layout.kks = name
        elif low in ("№ кабеля", "номер кабеля", "cable_no", "cable no", "кабель №"):
            layout.cable = name
    if layout.signal is None:
        layout.signal = _best_signal_column(headers, rows, split, layout)
    for name, low in folded.items():
        if name in (layout.kks, layout.signal):
            continue
        if low.startswith(("шкаф", "ккс ск", "cabinet", "коробк", "box")):
            layout.places[name] = _place_role(low)
    _assign_contacts(layout, folded)
    layout.channel = _best_channel(layout, rows)
    return layout


def _place_role(low: str) -> str:
    """Коробка по пути — «через», шкаф ввода-вывода — «связано», шкаф ПТК — «куда»."""
    if low.startswith(("ккс ск", "коробк", "box")):
        return "route_via"
    if "i/o" in low or "ввода" in low or low.endswith(" io"):
        return "related"
    return "route_end"


def _assign_contacts(layout: Layout, folded: dict[str, str]) -> None:
    """Клеммы и позиции относятся к месту с тем же отличительным словом: «ПТК», «I/O», «СК-1»."""
    marks = {place: _mark(folded[place]) for place in layout.places}
    for name, low in folded.items():
        if not low.startswith(("клемм", "позиция", "terminal", "position")):
            continue
        mark = _mark(low)
        for place, place_mark in marks.items():
            if mark and mark == place_mark:
                layout.contacts.setdefault(place, []).append(name)
                break


def _best_channel(layout: Layout, rows: list[Row]) -> list[str]:
    """Канал — место с контактами, которое однозначно определяет каждую строку сигнала."""
    signal_rows = [row for row in rows if layout.signal and _text(row.get(layout.signal))]
    for place, columns in layout.contacts.items():
        names = [place, *columns]
        keys = [channel_text(row, names) for row in signal_rows]
        if signal_rows and all(keys) and len(set(keys)) == len(keys):
            return names
    return []


def _best_signal_column(
    headers: list[str], rows: list[Row], split: Split, layout: Layout
) -> str | None:
    best, share = None, 0.5
    for name in headers:
        if name in (layout.kks, layout.cable):
            continue
        values = [_text(row.get(name)) for row in rows]
        filled = [value for value in values if value]
        if not filled:
            continue
        hits = sum(1 for value in filled if _try_split(split, value)[1] is not None)
        if hits / len(filled) > share:
            best, share = name, hits / len(filled)
    return best


# Внутреннее ------------------------------------------------------------------------


def _signal(
    number: int,
    row: Row,
    raw: str | None,
    kks: str | None,
    layout: Layout,
    split: Split,
    spare_words: set[str],
    issues: list[Issue],
) -> SignalRow:
    cable = _text(row.get(layout.cable)) if layout.cable else None
    channel = channel_text(row, layout.channel)
    values = {name: value for name, value in row.items() if _text(value) is not None}
    if raw is None or raw.casefold() in spare_words:
        if kks:
            issues.append(Issue(number, "spare_with_kks", f"Резервный канал с ККС {kks}"))
        return SignalRow(number, "spare", None, kks, "kks" if kks else None, cable, channel, values)
    obj, mark = _try_split(split, raw)
    if mark is None:
        if kks:
            return SignalRow(number, "signal", raw, kks, "kks", cable, channel, values)
        issues.append(
            Issue(
                number, "signal_without_device", f"Код {raw}: признака сигнала нет, ККС не указан"
            )
        )
        return SignalRow(number, "signal", raw, None, None, cable, channel, values)
    if kks and kks.upper() != obj.upper():
        issues.append(
            Issue(
                number,
                "signal_kks_mismatch",
                f"Код сигнала {raw} не начинается с ККС {kks}; строка привязана к {kks}",
            )
        )
        return SignalRow(number, "signal", raw, kks, "kks", cable, channel, values)
    return SignalRow(
        number, "signal", raw, kks or obj, "kks" if kks else "code", cable, channel, values
    )


def _spare_words(rows: list[tuple[int, Row]], layout: Layout, split: Split) -> set[str]:
    """Слова резерва: известные и любое значение без признака сигнала, повторённое без ККС."""
    words = set(SPARE_WORDS)
    repeats = Counter(
        raw.casefold()
        for _, row in rows
        if (raw := _text(row.get(layout.signal or ""))) is not None
        and not (layout.kks and _text(row.get(layout.kks)))
        and _try_split(split, raw)[1] is None
    )
    words.update(word for word, count in repeats.items() if count > 1)
    return words


def _add_cable(
    cables: dict[str, dict[str, Any]], item: SignalRow, row: Row, layout: Layout
) -> None:
    assert item.cable is not None
    cable = cables.setdefault(
        item.cable.upper(),
        {"number": item.cable, "signals": 0, "devices": set(), "places": defaultdict(set)},
    )
    cable["signals"] += 1
    if item.device:
        cable["devices"].add(item.device.upper())
    for place in layout.places:
        cable["places"][place].add(_text(row.get(place)))


def _cable_places(cables: dict[str, dict[str, Any]]) -> None:
    """Место кабеля — то, что одинаково у всех его сигналов; иначе места нет."""
    for cable in cables.values():
        cable["places"] = {
            place: next(iter(values))
            for place, values in cable["places"].items()
            if len(values) == 1 and next(iter(values)) is not None
        }


def _check_unique(
    key: Any, number: int, seen: dict[Any, int], code: str, what: str, issues: list[Issue]
) -> None:
    if key is None:
        return
    if key in seen:
        issues.append(Issue(number, code, f"Тот же {what}, что в строке {seen[key]}"))
    else:
        seen[key] = number


def _contact(row: Row, columns: list[str]) -> str | None:
    parts = [_text(row.get(name)) for name in columns]
    parts = [part for part in parts if part]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    head, *rest = parts
    return f"{head}:{'-'.join(rest)}" if not head.isdigit() else "-".join(parts)


def _try_split(split: Split, value: str) -> tuple[str, str | None]:
    try:
        return split(value)
    except Exception:  # noqa: BLE001 — код, который правило не разбирает, просто без признака
        return value, None


def _has_data(row: Row, ignored: set[str]) -> bool:
    return any(_text(value) is not None for name, value in row.items() if name not in ignored)


def _data(row: Row, ignored: set[str]) -> dict[str, str]:
    return {
        name: text
        for name, value in row.items()
        if name not in ignored and (text := _text(value)) is not None
    }


def _text(value: Any) -> str | None:
    if value is None:
        return None
    shown = str(value).strip()
    return shown or None


def _fold(name: str) -> str:
    return " ".join(str(name).casefold().replace("ё", "е").split())


def _mark(low: str) -> str:
    """Отличительное слово места или контакта: «птк», «i/o», «ск-1»."""
    words = [
        word
        for word in re.split(r"[\s–—]+", low)
        if word
        and not word.startswith(("клемм", "позиция", "шкаф", "ккс", "terminal", "position"))
        and not _NUMBER.fullmatch(word)
    ]
    return words[-1] if words else ""
