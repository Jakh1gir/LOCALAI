"""Перенос выгрузки Local_DB из Excel с магнитом ККС (K-302, AT-41). Данные вымышленные."""

from __future__ import annotations

import json

import pytest
from openpyxl import Workbook

from engine import graph, kks, open_database, structure
from server.localdb_export.__main__ import main
from server.xlsx import read_sheet

HEADERS = [
    "№", "Режим БД", "ККС", "Описание", "ККС сигнала", "Тип сигнала", "Шкаф ПТК", "№ кабеля",
    "Клеммник ПТК", "Клемма ПТК 1", "Клемма ПТК 2", "Шкаф I/O", "Позиция I/O", "Клемма I/O 1",
    "Клемма I/O 2",
]  # fmt: skip
OPERATION = [
    (1, "Эксплуатация", "10ABC01CP001", "Датчик давления"),
    (2, "Эксплуатация", "10ABC02AA101", "Задвижка"),
]
SIGNALS = [
    # № ККС  код сигнала  тип  шкаф ПТК  кабель  клеммник  к1  к2  шкаф I/O позиция  к1  к2
    (3, "10ABC01CP001", "10ABC01CP001XQ01", "AI", "00ZZA90GH001", "5025", "X25", "11", "12", "LU001", "2B3A", "21", "22"),
    (4, None, "10ABC02AA101XB62", "DO", "00ZZA90GH002", "5024", "X103", "3", "4", "LU002", "3B3A", "7", "8"),
    (5, None, "10ABC02AA101XB01", "DI", "00ZZA90GH002", "5011", "X11", "31", "32", "LU002", "3B1A", "31", "32"),
    (6, None, "10ABC02AA101XB02", "DI", "00ZZA90GH002", "5011", "X11", "33", "34", "LU002", "3B1A", "33", "34"),
    (7, None, "10ABC05CQ001XQ01", "AI", "00ZZA90GH001", "5025", "X25", "9", "10", "LU001", "2B3A", "17", "18"),
    (8, None, "Spare", None, "00ZZA90GH001", "5025", "X25", "13", "14", "LU001", "2B3A", "23", "24"),
    (9, "10ABC02AA101", None, None, None, None, None, None, None, None, None, None, None),
]  # fmt: skip


def _book(path, *, merged=False):
    book = Workbook()
    sheet = book.active
    sheet.title = "Local_DB"
    sheet.append(HEADERS)
    for number, mode, code, text in OPERATION:
        sheet.append([number, mode, code, text])
    for number, code, signal, *rest in SIGNALS:
        sheet.append([number, "Кабельная база", code, None, signal, *rest])
    if merged:
        # Как в рабочих таблицах: ККС аппарата объединён на все его сигналы.
        sheet["C5"] = "10ABC02AA101"
        sheet.merge_cells("C5:C7")
    book.save(path)
    return path


def test_merged_cells_are_filled_down(tmp_path):
    sheet = read_sheet(_book(tmp_path / "book.xlsx", merged=True))
    rows = {number: row for number, row in sheet.rows}
    assert [rows[n]["ККС"] for n in (5, 6, 7)] == ["10ABC02AA101"] * 3


def test_dry_run_counts_everything_and_leaves_target_untouched(tmp_path, capsys):
    target = tmp_path / "target.sqlite3"
    open_database(str(target)).close()
    before = target.read_bytes()
    report_path = tmp_path / "report.json"
    assert (
        main(
            [
                str(_book(tmp_path / "book.xlsx")),
                "--target",
                str(target),
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    assert target.read_bytes() == before
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cable = report["modes"]["Кабельная база"]
    assert report["mode"] == "dry_run"
    assert report["modes"]["Эксплуатация"]["records"]["created"] == 2
    assert cable["plan"]["signals"] == 5 and cable["plan"]["spares"] == 1
    assert cable["plan"]["skipped_empty_rows"] == 1
    assert cable["plan"]["device_from_signal_code"] == 4
    assert cable["records"]["cables"]["created"] == 3
    assert "Пробный прогон" in capsys.readouterr().out


def test_apply_lays_signals_on_devices_and_repeat_changes_nothing(tmp_path):
    book = _book(tmp_path / "book.xlsx", merged=True)
    target = tmp_path / "target.sqlite3"
    assert main([str(book), "--target", str(target), "--apply"]) == 0
    db = open_database(str(target))
    conn = db.connect()
    site = structure.list_sites(conn)[0]["id"]
    codes = {row[0] for row in conn.execute("SELECT code FROM kks_objects")}
    assert not any(
        code.endswith(("XQ01", "XB01", "XB62")) for code in codes
    )  # сигналы не в каталоге
    device = kks.resolve(conn, site, "10ABC02AA101")
    card = kks.card(conn, device["id"])
    objects = sum(item["records"] for item in card["usage"] if item["role"] == "object")
    assert objects == 4  # строка «Эксплуатации» и три сигнала (по наборам — 1 + 3)
    assert card["via_links"][0]["records"] == 2  # кабели 5024 и 5011
    around = graph.neighbourhood(conn, f"kks:{device['id']}", lambda _: frozenset(), 2)
    contacts = {edge["contact"] for edge in around["edges"] if edge["contact"]}
    assert {"X11:31-32", "X11:33-34", "3B1A:31-32"} <= contacts
    new_device = kks.resolve(conn, site, "10ABC05CQ001")  # аппарата нет в «Эксплуатации»
    assert new_device is not None
    db.close()
    report_path = tmp_path / "again.json"
    assert main([str(book), "--target", str(target), "--apply", "--report", str(report_path)]) == 0
    again = json.loads(report_path.read_text(encoding="utf-8"))
    signals = again["modes"]["Кабельная база"]["records"]["signals"]
    assert signals == {"created": 0, "updated": 0, "unchanged": 6}
    assert again["modes"]["Эксплуатация"]["records"]["created"] == 0
    assert len(list(tmp_path.glob("target.before-export-import.*.sqlite3"))) == 2


def test_failed_rows_cancel_the_whole_transfer(tmp_path, monkeypatch):
    from server import magnet_store

    target = tmp_path / "target.sqlite3"
    original = magnet_store.records.create
    calls = {"n": 0}

    def broken(tx, dataset_id, values, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            from engine import EngineError

            raise EngineError("value_invalid", "Сбой на третьей записи")
        return original(tx, dataset_id, values, **kw)

    monkeypatch.setattr(magnet_store.records, "create", broken)
    assert main([str(_book(tmp_path / "book.xlsx")), "--target", str(target), "--apply"]) == 2
    db = open_database(str(target))
    assert db.connect().execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("bad", ["not-excel", "missing"])
def test_unreadable_book_is_a_clear_error(tmp_path, capsys, bad):
    path = tmp_path / "book.xlsx"
    if bad == "not-excel":
        path.write_text("это не книга", encoding="utf-8")
    assert main([str(path)]) == 1
    assert "Ошибка" in capsys.readouterr().err
