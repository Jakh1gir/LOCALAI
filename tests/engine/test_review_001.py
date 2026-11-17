"""Находки R-001 в ядре: каждая проверка названа номером из docs/requests/001-claude-review.md."""

from __future__ import annotations

import sqlite3

import pytest

from engine import Database, EngineError, kks, migrator, open_database, query, records, structure

from .conftest import ENGINEER
from .test_structure import _with_extra_migration


def _record(db, plant, **values):
    keys = plant["keys"]
    with db.write(ENGINEER) as tx:
        return records.create(tx, plant["dataset"]["id"], {keys[k]: v for k, v in values.items()})


def test_03_large_integers_keep_precision(db, plant):
    keys = plant["keys"]
    record = _record(db, plant, kks="A1", power="9007199254740993")
    assert record["doc"][keys["power"]] == 9007199254740993
    with pytest.raises(EngineError) as caught:
        _record(db, plant, kks="A2", power="3.141592653589793238")
    assert caught.value.code == "value_precision" and caught.value.field == keys["power"]


def test_04_rollback_refuses_value_that_does_not_fit_the_field(db, plant):
    keys = plant["keys"]
    record = _record(db, plant, kks="A1", power=5)
    with db.write(ENGINEER) as tx:
        # история, перенесённая как текст: такое значение не приводится к числу
        tx.conn.execute(
            "UPDATE record_versions SET changes = ? WHERE version = 1",
            ("{}",),
        )
        records.update(tx, record["id"], {keys["power"]: 6}, 1)
        tx.conn.execute(
            "UPDATE record_versions SET changes = json_set(changes, ?, json_array('не число', 6))"
            " WHERE version = 2",
            (f"$.{keys['power']}",),
        )
    with db.read() as conn:
        assert (
            records.doc_at(conn, record["id"], 1)[keys["power"]] == "не число"
        )  # просмотр доступен
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.rollback(tx, record["id"], 1, 2)
    assert caught.value.code == "rollback_invalid" and caught.value.field == keys["power"]
    with db.read() as conn:
        assert records.get(conn, record["id"])["version"] == 2


def test_05_missing_version_is_reported_not_invented(db, plant):
    keys = plant["keys"]
    record = _record(db, plant, kks="A1", power=1)
    for value in (2, 3):
        with db.read() as conn:
            version = records.get(conn, record["id"])["version"]
        with db.write(ENGINEER) as tx:
            records.update(tx, record["id"], {keys["power"]: value}, version)
    with db.write(ENGINEER) as tx:
        tx.conn.execute("DELETE FROM record_versions WHERE version < 3")
    with db.read() as conn:
        assert [item["version"] for item in records.history(conn, record["id"])] == [3]
        with pytest.raises(EngineError) as caught:
            records.doc_at(conn, record["id"], 2)
    assert caught.value.code == "version_missing"
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.rollback(tx, record["id"], 2, 3)
    assert caught.value.code == "version_missing"


def test_12_out_of_range_integer_is_refused_on_write_and_in_filter(db, plant):
    keys = plant["keys"]
    with pytest.raises(EngineError) as caught:
        _record(db, plant, kks="A1", power=2**63)
    assert caught.value.code == "value_out_of_range"
    with db.read() as conn, pytest.raises(EngineError) as caught:
        query.list_records(
            conn,
            plant["dataset"]["id"],
            {"where": [{"field": keys["power"], "op": "eq", "value": 2**63}]},
        )
    assert caught.value.code == "query_invalid"


@pytest.mark.parametrize(
    "request_body",
    [
        {"where": [{"field": [], "op": "eq", "value": 1}]},
        {"where": [{"field": "x", "op": [], "value": 1}]},
        {"sort": {"field": [], "dir": "asc"}},
        {"limit": True},
    ],
)
def test_13_malformed_query_is_a_clear_error(db, plant, request_body):
    with db.read() as conn, pytest.raises(EngineError) as caught:
        query.list_records(conn, plant["dataset"]["id"], request_body)
    assert caught.value.code == "query_invalid"


def test_13_null_field_property_is_refused(db, plant):
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        structure.update_field(tx, plant["fields"]["name"]["id"], position=None)
    assert caught.value.code == "field_change" and caught.value.field == "position"


@pytest.mark.parametrize("value", ["2024-02-29Tмусор", "2024-02-2", "29.02.2025", "2024/02/29"])
def test_15_date_with_garbage_is_refused(db, plant, value):
    with pytest.raises(EngineError) as caught:
        _record(db, plant, kks="A1", checked=value)
    assert caught.value.code == "value_date"
    assert _record(db, plant, kks="A2", checked="2024-02-29T10:00:00+03:00")["doc"]


def test_16_empty_and_long_kks_follow_one_rule(db, plant):
    with db.write(ENGINEER) as tx:
        structure.update_field(tx, plant["fields"]["kks"]["id"], required=False)
    keys = plant["keys"]
    assert keys["kks"] not in _record(db, plant, kks="")["doc"]
    assert keys["kks"] not in _record(db, plant, kks="   ")["doc"]
    with pytest.raises(EngineError) as caught:
        _record(db, plant, kks="A" * 129)
    assert caught.value.code == "kks_too_long" and caught.value.field == keys["kks"]


def test_16_search_normalizes_unicode_like_codes(db, plant):
    _record(db, plant, kks="10ЁA")
    with db.read() as conn:
        site = plant["site"]["id"]
        assert kks.search(conn, site, "10Ё")
        assert kks.search(conn, site, "10Ё")


def test_17_kks_description_change_is_audited(db, plant):
    record = _record(db, plant, kks="A1")
    kks_id = record["doc"][plant["keys"]["kks"]]
    with db.write(ENGINEER) as tx:
        kks.update(tx, kks_id, name="Насос ПЭН-1")
    with db.read() as conn:
        actions = [
            row[0]
            for row in conn.execute("SELECT action FROM audit_log WHERE entity_id = ?", (kks_id,))
        ]
    assert "kks.update" in actions


def test_18_dataset_of_archived_type_stays_archived(db, plant):
    with db.write(ENGINEER) as tx:
        structure.archive_dataset(tx, plant["dataset"]["id"])
        structure.archive_type(tx, plant["type"]["id"])
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        structure.archive_dataset(tx, plant["dataset"]["id"], False)
    assert caught.value.code == "dataset_parent_archived"


def test_19_deep_descending_page_starts_at_the_cursor(db, plant):
    keys = plant["keys"]
    rows = [{keys["kks"]: f"R{n}", keys["power"]: n} for n in range(3000)]
    with db.write(ENGINEER) as tx:
        records.create_many(tx, plant["dataset"]["id"], rows)
    request = {"sort": {"field": keys["power"], "dir": "desc"}, "limit": 200}
    conn = db.connect()
    cursor = None
    for _ in range(14):
        cursor = query.list_records(conn, plant["dataset"]["id"], {**request, "cursor": cursor})[
            "next_cursor"
        ]
    steps = {"count": 0}

    def tick() -> int:
        steps["count"] += 1
        return 0

    def measure(body):
        steps["count"] = 0
        conn.set_progress_handler(tick, 100)
        page = query.list_records(conn, plant["dataset"]["id"], body)
        conn.set_progress_handler(None, 0)
        return steps["count"], page

    first, _ = measure({**request, "limit": 5})
    deep, page = measure({**request, "limit": 5, "cursor": cursor})
    assert [item["doc"][keys["power"]] for item in page["items"]] == [199, 198, 197, 196, 195]
    assert deep <= first * 3 + 5  # глубокая страница стоит как первая, а не как 2800 строк


def test_21_rollback_treats_archived_value_and_kks_alike(db, plant):
    keys = plant["keys"]
    record = _record(db, plant, kks="X1", maker="Siemens")
    with db.write(ENGINEER) as tx:
        abb = structure.add_dictionary_value(tx, plant["makers"]["id"], "ABB")
        records.update(tx, record["id"], {keys["maker"]: "ABB"}, 1)
        structure.update_dictionary_value(tx, plant["siemens"]["id"], archived=True)
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.rollback(tx, record["id"], 1, 2)
    assert caught.value.code == "value_choice_archived"
    with db.write(ENGINEER) as tx:
        structure.update_dictionary_value(tx, plant["siemens"]["id"], archived=False)
        rolled = records.rollback(tx, record["id"], 1, 2)
    assert rolled["doc"][keys["maker"]] == plant["siemens"]["id"] != abb["id"]


def test_01_same_kks_in_two_fields_is_allowed(db, plant):
    with db.write(ENGINEER) as tx:
        second = structure.add_field(tx, plant["type"]["id"], "Второй ККС", "kks", key="aux")
        record = records.create(
            tx, plant["dataset"]["id"], {plant["keys"]["kks"]: "10CCC01", "aux": "10ccc01"}
        )
    with db.read() as conn:
        links = conn.execute("SELECT field_key FROM record_kks_links ORDER BY field_key").fetchall()
    assert record["doc"]["aux"] == record["doc"][plant["keys"]["kks"]]
    assert [row[0] for row in links] == sorted([plant["keys"]["kks"], second["key"]])


def test_23_verified_copy_before_each_migration(tmp_path, monkeypatch):
    path = tmp_path / "base.sqlite3"
    open_database(str(path)).close()
    latest = len(migrator.available())
    _with_extra_migration(tmp_path, monkeypatch, "probe", "CREATE TABLE probe (x INTEGER);")
    extra = migrator.MIGRATIONS_DIR
    (extra / f"{latest + 2:04d}_broken.sql").write_text("NOT SQL;", encoding="utf-8")
    db = Database(str(path))
    with pytest.raises(EngineError):
        migrator.migrate(db)
    copies = sorted(item.name.split(".")[1] for item in tmp_path.glob("base.schema*.sqlite3"))
    assert copies == [f"schema{latest:04d}", f"schema{latest + 1:04d}"]
    assert migrator.current_version(db.connect()) == latest + 1
    for copy in tmp_path.glob("base.schema*.sqlite3"):
        assert sqlite3.connect(copy).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()
