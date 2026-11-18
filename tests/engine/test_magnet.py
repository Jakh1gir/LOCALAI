"""Магнит ККС: раскладка таблицы сигналов (IMP-13…16). Данные вымышленные."""

from __future__ import annotations

import re

from engine import magnet

HEADERS = [
    "№",
    "ККС",
    "ККС сигнала",
    "Тип сигнала",
    "Шкаф ПТК",
    "№ кабеля",
    "Клеммник ПТК",
    "Клемма ПТК 1",
    "Клемма ПТК 2",
    "Шкаф I/O",
    "Позиция I/O",
    "Клемма I/O 1",
    "Клемма I/O 2",
]
_SIGNAL = re.compile(r"^(?P<o>.+?)[ _-]?(?P<s>X[A-Z][0-9]{2})$")


def split(code):
    norm = code.strip().upper()
    found = _SIGNAL.match(norm)
    return (found["o"], found["s"]) if found else (norm, None)


def _row(number, kks, code, kind, cabinet, cable, block, t1, t2, io, pos, i1, i2):
    values = [number, kks, code, kind, cabinet, cable, block, t1, t2, io, pos, i1, i2]
    return dict(zip(HEADERS, values, strict=True))


ROWS = [
    _row(
        1,
        "10ABC01CP001",
        "10ABC01CP001XQ01",
        "AI",
        "00ZZA90GH001",
        "K1",
        "X1",
        "1",
        "2",
        "LU1",
        "1A",
        "1",
        "2",
    ),
    _row(
        2, "10ABC01CP001", None, None, None, None, None, None, None, None, None, None, None
    ),  # заготовка
    _row(
        3,
        None,
        "10ABC02AA101XB01",
        "DI",
        "00ZZA90GH001",
        "K1",
        "X1",
        "3",
        "4",
        "LU1",
        "1A",
        "3",
        "4",
    ),
    _row(
        4,
        None,
        "10ABC02AA101XB02",
        "DI",
        "00ZZA90GH001",
        "K1",
        "X1",
        "5",
        "6",
        "LU1",
        "1A",
        "5",
        "6",
    ),
    _row(5, None, "Spare", None, "00ZZA90GH001", "K1", "X1", "7", "8", "LU1", "1A", "7", "8"),
    _row(6, None, "Spare", None, "00ZZA90GH002", "K2", "X9", "1", "2", "LU2", "2B", "1", "2"),
    _row(
        7,
        "10ABC09AA001",
        "10ABC03AA101XB62",
        "DO",
        "00ZZA90GH002",
        "K2",
        "X9",
        "3",
        "4",
        "LU2",
        "2B",
        "3",
        "4",
    ),
    _row(8, None, "SYS_101XB01", "DI", "00ZZA90GH002", "K2", "X9", "3", "4", "LU2", "2B", "5", "6"),
]


def _numbered(rows):
    return [(row["№"] + 1, row) for row in rows]


def test_layout_is_recognised_from_headers_and_content():
    layout = magnet.suggest_layout(HEADERS, ROWS, split)
    assert (layout.kks, layout.signal, layout.cable) == ("ККС", "ККС сигнала", "№ кабеля")
    assert layout.places == {"Шкаф ПТК": "route_end", "Шкаф I/O": "related"}
    assert layout.contacts["Шкаф ПТК"] == ["Клеммник ПТК", "Клемма ПТК 1", "Клемма ПТК 2"]
    assert layout.contacts["Шкаф I/O"] == ["Позиция I/O", "Клемма I/O 1", "Клемма I/O 2"]
    # Клеммы ПТК повторяются в строках 7 и 8, а канал I/O уникален — он и ключ.
    assert layout.channel == ["Шкаф I/O", "Позиция I/O", "Клемма I/O 1", "Клемма I/O 2"]
    assert layout.service == ["№"]


def test_rows_are_pulled_to_devices_and_spares_kept():
    layout = magnet.suggest_layout(HEADERS, ROWS, split)
    plan = magnet.plan_signals(_numbered(ROWS), layout, split)
    summary = plan.summary()
    assert summary["signals"] == 5 and summary["spares"] == 2
    assert summary["skipped_empty_rows"] == 1  # строка 2: только ККС
    assert summary["device_from_kks_column"] == 2 and summary["device_from_signal_code"] == 3
    by_row = {item.row: item for item in plan.signals}
    assert by_row[4].device == "10ABC02AA101" and by_row[4].via == "code"
    assert by_row[6].kind == "spare" and by_row[6].code is None
    assert by_row[2].channel == "LU1 / 1A / 1 / 2"
    assert set(plan.devices) == {"10ABC01CP001", "10ABC02AA101", "10ABC09AA001", "SYS_101"}
    assert summary["devices_not_kks_like"] == ["SYS_101"]


def test_disagreements_are_reported_not_merged():
    layout = magnet.suggest_layout(HEADERS, ROWS, split)
    plan = magnet.plan_signals(_numbered(ROWS), layout, split)
    codes = {(issue.row, issue.code) for issue in plan.issues}
    # Код сигнала 10ABC03…XB62, а в столбце ККС 10ABC09… — строка остаётся при столбце.
    assert (8, "signal_kks_mismatch") in codes
    assert next(item for item in plan.signals if item.row == 8).device == "10ABC09AA001"
    assert (9, "contact_duplicate") in codes  # те же клеммы ПТК, что в строке 8


def test_cables_carry_signals_of_several_devices():
    layout = magnet.suggest_layout(HEADERS, ROWS, split)
    plan = magnet.plan_signals(_numbered(ROWS), layout, split)
    first = plan.cables["K1"]
    assert first["signals"] == 4 and first["devices"] == {"10ABC01CP001", "10ABC02AA101"}
    assert first["places"] == {"Шкаф ПТК": "00ZZA90GH001", "Шкаф I/O": "LU1"}
    assert magnet.contact_text(ROWS[0], layout.contacts["Шкаф ПТК"]) == "X1:1-2"


def test_device_table_keeps_one_row_per_kks_and_reports_conflicts():
    rows = [
        (2, {"ККС": "10ABC01CP001", "Описание": "Датчик давления"}),
        (3, {"ККС": "10abc01cp001", "Описание": "Датчик давления"}),  # тот же, без расхождений
        (4, {"ККС": "10ABC01CP001", "Описание": "Другое"}),
        (5, {"ККС": None, "Описание": "Без кода"}),
        (6, {"ККС": None, "Описание": None}),
    ]
    plan = magnet.plan_devices(rows, "ККС")
    assert list(plan.rows) == ["10ABC01CP001"]
    assert [(issue.row, issue.code) for issue in plan.issues] == [
        (4, "duplicate_differs"),
        (5, "no_kks"),
    ]
    assert plan.skipped == [6]
