"""Импорт базы Local_DB: пробный прогон, перенос, повтор, отказ (MIG-01…MIG-15, AT-19)."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from engine import Actor, EngineError, kks, open_database, query, records, structure
from server.localdb_import import Decisions, run
from server.localdb_import.__main__ import main

from .localdb_fixture import LocalDb, standard

OPERATION = "В эксплуатации"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def source(tmp_path):
    """Каталог данных Local_DB с типовой базой и описанием того, что в ней лежит."""
    folder = tmp_path / "localdb"
    folder.mkdir()
    base = standard(folder / "local_db.sqlite3")
    pump = base.record(
        {"database_mode": OPERATION, "kks": ["10LAC10AP001"], "sensor": "Насос ПЭН-1",
         "manufacturer": "Siemens", "installed_qty": 2, "power": "500.50",
         "next_check_date": "2026-05-01", "notes": "проверить уплотнение"},
        history=[("CREATE_RECORD", {"database_mode": OPERATION, "kks": ["10LAC10AP009"],
                                    "sensor": "Насос ПЭН-1", "manufacturer": "Завод-1"})],
    )  # fmt: skip
    spare = base.record(
        {"database_mode": "Склад", "kks": ["10LAC10AP001", "10LAC10AP002"],
         "sensor": "Насос ПЭН-1 резервный", "manufacturer": "АББ старое", "installed_qty": 1},
    )  # fmt: skip
    similar = base.record({"database_mode": OPERATION, "kks": ["10LAC10 AP002"],
                           "sensor": "Вентилятор"})  # fmt: skip
    old = base.record({"database_mode": OPERATION, "kks": ["20BBA01"], "sensor": "Ячейка"},
                      archived=True)  # fmt: skip
    base.audit("CREATE_RECORD", pump)
    base.audit("UPDATE_RECORD", pump)
    base.close()
    return {"folder": folder, "file": folder / "local_db.sqlite3", "pump": pump,
            "spare": spare, "similar": similar, "old": old}  # fmt: skip


@pytest.fixture
def target(tmp_path):
    db = open_database(str(tmp_path / "localai.sqlite3"))
    yield db
    db.close()


def _count(db, table):
    with db.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_dry_run_reports_everything_and_writes_nothing(source, target):
    before = _sha(source["file"])
    report = run(target, source["folder"], Decisions(site_name="ТЭЦ-1"))
    assert report["mode"] == "dry_run"
    assert report["source"]["schema_version"] == 34
    names = {item["name"]: item for item in report["datasets"]}
    assert names[OPERATION]["records_new"] == 3 and names["Склад"]["records_new"] == 1
    assert all(check["ok"] for check in report["reconciliation"])
    assert report["kks"]["history_only"] == 1  # 10LAC10AP009 был только в истории
    assert report["not_transferred"] == {}
    for table in ("records", "sites", "types", "kks_objects", "import_runs", "audit_log"):
        assert _count(target, table) == 0, table
    assert _sha(source["file"]) == before  # источник не изменён (MIG-02)
    assert [item.name for item in source["folder"].iterdir()] == ["local_db.sqlite3"]


def test_apply_transfers_structure_records_history_and_catalog(source, target, tmp_path):
    report = run(target, source["folder"], Decisions(site_name="ТЭЦ-1"), apply=True,
                 snapshot_dir=tmp_path / "snapshots")  # fmt: skip
    assert report["mode"] == "applied", report["issues"]
    assert list((tmp_path / "snapshots").glob("*.before-localdb-import.*.sqlite3"))
    with target.read() as conn:
        kind = structure.list_types(conn)[0]
        fields = {field["key"]: field for field in structure.get_type(conn, kind["id"])["fields"]}
        datasets = {item["name"]: item for item in structure.list_datasets(conn)}
        pump = records.get(conn, source["pump"])
        spare = records.get(conn, source["spare"])
        history = records.history(conn, source["pump"])
        first = records.doc_at(conn, source["pump"], 1)
        old = records.get(conn, source["old"])
        site_id = structure.list_sites(conn)[0]["id"]
        shared = kks.resolve(conn, site_id, "10lac10ap001")
        card = kks.card(conn, shared["id"])
        gone = kks.resolve(conn, site_id, "10LAC10AP009")
        found = query.list_records(conn, datasets[OPERATION]["id"], {"search": "siemens пэн"})
        by_synonym = query.count_records(
            conn, datasets[OPERATION]["id"],
            {"where": [{"field": "manufacturer", "op": "eq", "value": "Сименс"}]},
        )  # fmt: skip
        events = conn.execute(
            "SELECT actor_label FROM audit_log WHERE origin = 'localdb'"
        ).fetchall()
    # Структура: ключи полей — коды Local_DB, режим стал реестрами (MIG-04).
    assert kind["name"] == "Оборудование"
    assert "database_mode" not in fields
    assert fields["kks"]["kind"] == "kks" and fields["kks"]["multiple"]
    assert fields["manufacturer"]["kind"] == "choice"
    assert fields["power"]["kind"] == "number" and fields["notes"]["kind"] == "text"
    assert fields["old_code"]["archived_at"] is not None
    assert set(datasets) == {OPERATION, "Склад"}
    # Записи: UUID, версии, авторы и значения сохранены (MIG-05, MIG-09).
    assert pump["dataset_id"] == datasets[OPERATION]["id"] and pump["version"] == 2
    assert pump["doc"]["power"] == 500.5 and pump["doc"]["installed_qty"] == 2
    assert pump["doc"]["next_check_date"] == "2026-05-01"
    assert [item["version"] for item in history] == [2, 1]
    assert history[0]["actor_label"] == "Иванов (из Local_DB)" and history[0]["origin"] == "localdb"
    assert "manufacturer" in history[0]["changes"]
    assert first["manufacturer"] != pump["doc"]["manufacturer"]
    assert old["archived_at"] is not None
    # Объединённое значение справочника заменено конечным (ABB).
    assert spare["doc"]["manufacturer"] == _value_id(target, "ABB")
    # Каталог ККС: один объект на код в двух режимах, история — в архиве (MIG-06).
    assert sorted((row["role"], row["records"]) for row in card["usage"]) == [("object", 1)] * 2
    assert gone is not None and gone["archived_at"] is not None
    assert ["10LAC10 AP002", "10LAC10AP002"] in report["kks"]["similar"]
    assert len(found["items"]) == 1 and by_synonym == 1  # синоним Local_DB перенесён
    assert len(events) == 2 and events[0]["actor_label"].endswith("(из Local_DB)")
    assert _count(target, "import_runs") == 1


def _value_id(db, value):
    with db.read() as conn:
        return conn.execute("SELECT id FROM dictionary_values WHERE value = ?", (value,)).fetchone()[0]


def test_repeat_import_adds_only_new_data_and_keeps_local_edits(source, target):
    decisions = Decisions(site_name="ТЭЦ-1")
    assert run(target, source["folder"], decisions, apply=True)["mode"] == "applied"
    with target.read() as conn:
        spare_before = records.get(conn, source["spare"])
    with target.write(Actor(None, "Инженер")) as tx:
        records.update(tx, source["spare"], {"installed_qty": 5}, spare_before["version"])
    base = LocalDb.__new__(LocalDb)
    base.conn = sqlite3.connect(source["file"])
    base.fields = {row[0]: {"id": row[1], "type": row[2], "dictionary_id": row[3]}
                   for row in base.conn.execute(
                       "SELECT code, id, data_type, dictionary_id FROM field_definitions")}  # fmt: skip
    base.dictionaries = {code: meta["dictionary_id"] for code, meta in base.fields.items()
                         if meta["dictionary_id"]}  # fmt: skip
    base.records = dict(base.conn.execute("SELECT uuid, id FROM records"))
    base.table_id = 1
    base.update(source["pump"], {"database_mode": OPERATION, "kks": ["10LAC10AP001"],
                                 "sensor": "Насос ПЭН-1", "manufacturer": "ABB"})  # fmt: skip
    base.update(source["spare"], {"database_mode": "Склад", "kks": ["10LAC10AP001"],
                                  "sensor": "Насос ПЭН-1 резервный"})  # fmt: skip
    fresh = base.record({"database_mode": "Склад", "kks": ["30XYZ01"], "sensor": "Задвижка"})
    base.audit("UPDATE_RECORD", source["pump"])
    base.close()

    report = run(target, source["folder"], decisions, apply=True)
    assert report["mode"] == "applied", report["issues"]
    names = {item["name"]: item for item in report["datasets"]}
    assert names[OPERATION]["records_extended"] == 1 and names[OPERATION]["records_unchanged"] == 2
    assert names["Склад"]["records_new"] == 1 and names["Склад"]["records_skipped"] == 1
    assert {item["code"] for item in report["issues"]} >= {"record_edited"}
    assert report["audit"]["events"] == 1  # только новое событие журнала
    with target.read() as conn:
        pump = records.get(conn, source["pump"])
        spare = records.get(conn, source["spare"])
        assert records.get(conn, fresh)["version"] == 1
    assert pump["version"] == 3 and pump["doc"]["manufacturer"] == _value_id(target, "ABB")
    assert spare["doc"]["installed_qty"] == 5  # правка в LOCALAI не затёрта
    assert _count(target, "records") == 5 and _count(target, "import_runs") == 2


def test_bad_value_refuses_whole_import(source, target):
    base = sqlite3.connect(source["file"])
    record_id = base.execute("SELECT id FROM records WHERE uuid = ?", (source["similar"],)).fetchone()[0]
    field_id = base.execute("SELECT id FROM field_definitions WHERE code = 'next_check_date'").fetchone()[0]
    base.execute(
        "INSERT INTO record_values (record_id, field_id, ordinal, value_date, created_at)"
        " VALUES (?, ?, 0, '2026-13-45', 'x')",
        (record_id, field_id),
    )
    base.commit()
    base.close()
    report = run(target, source["folder"], Decisions(site_name="ТЭЦ-1"), apply=True)
    assert report["mode"] == "refused"
    problem = next(item for item in report["issues"] if item["code"] == "value_invalid")
    assert problem["examples"][0]["record_id"] == source["similar"]
    for table in ("records", "sites", "kks_objects", "import_runs"):
        assert _count(target, table) == 0, table  # частичного переноса нет (MIG-12)


@pytest.mark.parametrize(("schema", "code"), [(4, "localdb_schema_old"), (40, "localdb_schema_new")])
def test_unsupported_schema_is_refused_with_explanation(tmp_path, target, schema, code):
    base = LocalDb(tmp_path / "local_db.sqlite3", schema=schema)
    base.close()
    with pytest.raises(EngineError) as caught:
        run(target, tmp_path / "local_db.sqlite3")
    assert caught.value.code == code


def test_foreign_file_is_not_taken_for_localdb(tmp_path, target):
    other = tmp_path / "other.sqlite3"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(EngineError) as caught:
        run(target, other)
    assert caught.value.code == "localdb_not_recognized"


def test_several_sites_need_a_decision(source, target):
    with target.write(Actor(None, "Админ")) as tx:
        structure.create_site(tx, "ТЭЦ-1")
        structure.create_site(tx, "ТЭЦ-2")
    report = run(target, source["folder"])
    assert report["mode"] == "refused"
    assert report["issues"][0]["code"] == "site_required"


def test_command_line_dry_run(source, capsys, tmp_path):
    code = main([str(source["folder"]), "--report", str(tmp_path / "report.json")])
    printed = capsys.readouterr().out
    assert code == 0
    assert "Пробный прогон" in printed and "Склад" in printed and "совпадает" in printed
    assert (tmp_path / "report.json").exists()
