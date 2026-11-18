"""Перенос базы Local_DB с магнитом ККС (MIG-16, IMP-13…16, AT-41). Данные вымышленные."""

from __future__ import annotations

import json
import sqlite3

import pytest

from engine import graph, kks, open_database, records, structure
from server.localdb_import import Decisions, run
from server.localdb_import.__main__ import main

from .localdb_fixture import LocalDb

CABLES = "Кабельная база"
FIELDS = [
    ("kks", "ККС", "smartfield"),
    ("sensor", "Наименование", "text"),
    ("signal_kks", "ККС сигнала", "text"),
    ("signal_type", "Тип сигнала", "text"),
    ("cabinet_ptk", "Шкаф ПТК", "text"),
    ("cable_no", "№ кабеля", "text"),
    ("block_ptk", "Клеммник ПТК", "text"),
    ("term_ptk_1", "Клемма ПТК 1", "text"),
    ("term_ptk_2", "Клемма ПТК 2", "text"),
    ("cabinet_io", "Шкаф I/O", "text"),
    ("pos_io", "Позиция I/O", "text"),
    ("term_io_1", "Клемма I/O 1", "text"),
    ("term_io_2", "Клемма I/O 2", "text"),
]


# код сигнала, ККС, шкаф ПТК, кабель, клеммник, к1, к2, шкаф I/O, позиция, к1, к2, тип
SIGNALS = {
    "ai": ("10ABC01CP001XQ01", "10ABC01CP001", "00ZZA90GH001", "K-25", "X25", "11", "12", "LU001", "2B3A", "21", "22", "AI"),
    "open": ("10ABC02AA101XB01", None, "00ZZA90GH002", "K-11", "X11", "31", "32", "LU002", "3B1A", "31", "32", "DI"),
    "closed": ("10ABC02AA101XB02", None, "00ZZA90GH002", "K-11", "X11", "33", "34", "LU002", "3B1A", "33", "34", "DI"),
    "spare": ("Spare", None, "00ZZA90GH001", "K-25", "X25", "13", "14", "LU001", "2B3A", "23", "24", None),
}  # fmt: skip
# Первая версия «open»: код сигнала другого аппарата, потом исправлен.
OPEN_FIRST = ("10ABC09AA101XB01", None, "00ZZA90GH002", "K-11", "X11", "31", "32", "LU002", "3B1A", "31", "32", "DI")  # fmt: skip
# Новая версия «closed» в Local_DB: другой клеммник.
CLOSED_NEXT = ("10ABC02AA101XB02", None, "00ZZA90GH002", "K-11", "X12", "33", "34", "LU002", "3B1A", "33", "34", "DI")  # fmt: skip
_CODES = (
    "signal_kks", "kks", "cabinet_ptk", "cable_no", "block_ptk", "term_ptk_1", "term_ptk_2",
    "cabinet_io", "pos_io", "term_io_1", "term_io_2", "signal_type",
)  # fmt: skip


def _signal(row):
    values = {"database_mode": CABLES, **dict(zip(_CODES, row, strict=True))}
    values["kks"] = [values["kks"]] if values["kks"] else None
    return {code: value for code, value in values.items() if value is not None}


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / "localdb"
    folder.mkdir()
    base = LocalDb(folder / "local_db.sqlite3")
    base.field("database_mode", "Режим БД", "smartfield", input_mode="strict")
    for code, name, kind in FIELDS:
        base.field(code, name, kind, multiple=code == "kks")
    base.value("database_mode", "Эксплуатация")
    base.value("database_mode", CABLES)
    ids = {
        "sensor": base.record(
            {"database_mode": "Эксплуатация", "kks": ["10ABC01CP001"], "sensor": "Датчик"}
        ),
        "valve": base.record(
            {"database_mode": "Эксплуатация", "kks": ["10ABC02AA101"], "sensor": "Задвижка"}
        ),
        "blank": base.record({"database_mode": CABLES, "kks": ["10ABC02AA101"]}),
    }
    for name, row in SIGNALS.items():
        history = [("CREATE_RECORD", _signal(OPEN_FIRST))] if name == "open" else []
        ids[name] = base.record(_signal(row), history=history)
    base.close()
    return {"folder": folder, "file": folder / "local_db.sqlite3", **ids}


@pytest.fixture
def target(tmp_path):
    db = open_database(str(tmp_path / "localai.sqlite3"))
    yield db
    db.close()


def _entry(report):
    (entry,) = report["magnet"]
    return entry


def _fields(conn, record_id):
    """Ключи полей магнита по названиям для типа записи."""
    row = conn.execute(
        "SELECT d.type_id FROM records r JOIN datasets d ON d.seq = r.dataset_seq WHERE r.id = ?",
        (record_id,),
    ).fetchone()
    return {item.name: item.key for item in structure.load_schema(conn, row[0]).fields}


def test_dry_run_proposes_layout_and_applies_nothing(source, target):
    report = run(target, source["folder"])
    assert report["mode"] == "dry_run"
    entry = _entry(report)
    assert entry["dataset"] == CABLES and entry["applied"] is False
    assert entry["layout"]["signal"] == "ККС сигнала" and entry["layout"]["cable"] == "№ кабеля"
    plan = entry["plan"]
    assert (plan["signals"], plan["spares"], plan["cables"]) == (3, 1, 2)
    assert plan["device_from_signal_code"] == 2 and plan["skipped_empty_rows"] == 1
    assert "magnet_proposed" in {item["code"] for item in report["issues"]}
    assert all(item["ok"] for item in report["reconciliation"])


def test_apply_pulls_rows_to_devices_and_keeps_history(source, target):
    report = run(target, source["folder"], Decisions(magnet=True), apply=True)
    assert report["mode"] == "applied", report["issues"]
    entry = _entry(report)
    assert entry["applied"] and entry["cables"] == {"created": 2, "updated": 0, "unchanged": 0}
    assert entry["records_laid_out"] == 4  # три сигнала и резерв; заготовка без раскладки
    assert all(item["ok"] for item in report["reconciliation"])  # исходные поля сверены
    with target.read() as conn:
        site = structure.list_sites(conn)[0]["id"]
        keys = _fields(conn, source["open"])
        signal = records.get(conn, source["open"])
        valve = kks.resolve(conn, site, "10ABC02AA101")
        assert signal["version"] == 2  # номера версий Local_DB сохранены
        assert signal["doc"][keys["Аппарат"]] == valve["id"]
        cable = records.get(conn, signal["doc"][keys["Кабель"]])
        assert "K-11" in cable["doc"].values()
        codes = {row[0] for row in conn.execute("SELECT code FROM kks_objects")}
        assert not any(code.endswith(("XQ01", "XB01", "XB02")) for code in codes)
        # Аппарат прежней версии вычислен по её коду сигнала и остался только в истории.
        old = kks.resolve(conn, site, "10ABC09AA101")
        assert old is not None and old["archived_at"] is not None
        changes = conn.execute(
            "SELECT changes FROM record_versions v JOIN records r ON r.seq = v.record_seq"
            " WHERE r.id = ? AND v.version = 2",
            (source["open"],),
        ).fetchone()[0]
        assert keys["Аппарат"] in json.loads(changes)
        spare = records.get(conn, source["spare"])
        assert keys["Аппарат"] not in spare["doc"] and keys["Кабель"] in spare["doc"]
        card = kks.card(conn, valve["id"])
        objects = sum(item["records"] for item in card["usage"] if item["role"] == "object")
        assert objects == 4  # «Эксплуатация», два сигнала, заготовка
        assert card["via_links"][0]["records"] == 1  # кабель K-11
        around = graph.neighbourhood(conn, f"kks:{valve['id']}", lambda _: frozenset(), 2)
        contacts = {edge["contact"] for edge in around["edges"] if edge["contact"]}
        assert {"X11:31-32", "X11:33-34"} <= contacts


def test_repeat_keeps_layout_without_flag_and_changes_nothing(source, target):
    assert run(target, source["folder"], Decisions(magnet=True), apply=True)["mode"] == "applied"
    again = run(target, source["folder"], apply=True)  # без --magnet: раскладка из прошлого раза
    assert again["mode"] == "applied", again["issues"]
    entry = _entry(again)
    assert entry["applied"] and entry["cables"] == {"created": 0, "updated": 0, "unchanged": 2}
    names = {item["name"]: item for item in again["datasets"]}
    assert names[CABLES]["records_unchanged"] == 5 and names[CABLES]["records_new"] == 0
    assert "magnet_not_applied" not in {item["code"] for item in again["issues"]}


def test_magnet_added_later_lands_only_on_new_versions(source, target):
    assert run(target, source["folder"], apply=True)["mode"] == "applied"  # без магнита
    base = sqlite3.connect(source["file"])
    record_id = base.execute("SELECT id FROM records WHERE uuid = ?", (source["closed"],))
    record_id = record_id.fetchone()[0]
    base.execute(
        "UPDATE record_values SET value_text = 'X12' WHERE record_id = ? AND field_id ="
        " (SELECT id FROM field_definitions WHERE code = 'block_ptk')",
        (record_id,),
    )
    snapshot = _signal(CLOSED_NEXT)
    base.execute(
        "UPDATE records SET version = 2, updated_at = '2026-03-02T09:00:00+00:00' WHERE id = ?",
        (record_id,),
    )
    base.execute(
        "INSERT INTO record_versions (record_id, version, operation, snapshot_json, actor_name,"
        " mode, occurred_at) VALUES (?, 2, 'UPDATE_RECORD', ?, 'Петров', 'engineer',"
        " '2026-03-02T09:00:00.000000+00:00')",
        (record_id, json.dumps({"archived": False, "values": snapshot}, ensure_ascii=False)),
    )
    base.commit()
    base.close()

    report = run(target, source["folder"], Decisions(magnet=True), apply=True)
    assert report["mode"] == "applied", report["issues"]
    names = {item["name"]: item for item in report["datasets"]}
    assert names[CABLES]["records_extended"] == 1
    stale = next(item for item in report["issues"] if item["code"] == "magnet_not_applied")
    assert stale["count"] == 3  # сигнал, сигнал и резерв не менялись — магнита у них нет
    with target.read() as conn:
        keys = _fields(conn, source["closed"])
        closed = records.get(conn, source["closed"])
        assert closed["version"] == 2 and keys["Аппарат"] in closed["doc"]
        assert keys["Аппарат"] not in records.get(conn, source["ai"])["doc"]


def test_edited_layout_file_is_used(source, target, tmp_path, capsys):
    proposed = _entry(run(target, source["folder"]))["layout"]
    proposed["places"] = {"Шкаф ПТК": "route_end"}  # без шкафов ввода-вывода
    proposed["contacts"] = {"Шкаф ПТК": proposed["contacts"]["Шкаф ПТК"]}
    layout = tmp_path / "layout.json"
    layout.write_text(json.dumps({CABLES: proposed}, ensure_ascii=False), encoding="utf-8")
    target.close()
    code = main(
        [
            str(source["folder"]),
            "--target",
            str(tmp_path / "localai.sqlite3"),
            "--apply",
            "--magnet",
            "--layout",
            str(layout),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert f"Магнит ККС — реестр «{CABLES}»: разложено записей 4" in out
    db = open_database(str(tmp_path / "localai.sqlite3"))
    with db.read() as conn:
        names = set(_fields(conn, source["open"]))
        assert "Шкаф ПТК · объект" in names and "Шкаф I/O · объект" not in names
    db.close()


def test_bad_layout_file_is_refused(source, tmp_path, capsys):
    layout = tmp_path / "layout.json"
    layout.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SystemExit):
        main([str(source["folder"]), "--layout", str(layout)])
    assert "файл раскладки" in capsys.readouterr().err
