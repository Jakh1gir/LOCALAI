"""Записи: проверка значений, версии, история, откат, архив (DATA-02, 04, 05, 08)."""

from __future__ import annotations

import pytest

from engine import EngineError, audit, idempotency, records, structure

from .conftest import ENGINEER


def _create(db, plant, **values):
    keys = plant["keys"]
    with db.write(ENGINEER) as tx:
        return records.create(
            tx, plant["dataset"]["id"], {keys[name]: value for name, value in values.items()}
        )


def test_create_normalizes_values_and_stores_ids(db, plant):
    keys = plant["keys"]
    record = _create(
        db,
        plant,
        kks="10lac10ap001 ",
        name="  Насос питательный ",
        power="12,5",
        maker="сименс",
        checked="15.03.2024",
        spare="да",
        tags=["резерв", "", "осмотр"],
    )
    doc = record["doc"]
    assert record["version"] == 1
    assert doc[keys["name"]] == "Насос питательный"
    assert doc[keys["power"]] == 12.5
    assert doc[keys["maker"]] == plant["siemens"]["id"]  # синоним дал ID значения
    assert doc[keys["checked"]] == "2024-03-15"
    assert doc[keys["spare"]] is True
    assert doc[keys["tags"]] == ["резерв", "осмотр"]
    with db.read() as conn:
        links = conn.execute("SELECT kks_id, role FROM record_kks_links").fetchall()
        code = conn.execute("SELECT code FROM kks_objects").fetchone()[0]
    assert [(row["kks_id"], row["role"]) for row in links] == [(doc[keys["kks"]], "object")]
    assert code == "10lac10ap001"  # исходное написание сохранено


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"name": "Без ККС"}, "value_required"),
        ({"kks": "A1", "power": "много"}, "value_number"),
        ({"kks": "A1", "checked": "2024-13-40"}, "value_date"),
        ({"kks": "A1", "name": ["a", "b"]}, "value_single"),
    ],
)
def test_create_rejects_invalid_values(db, plant, values, code):
    with pytest.raises(EngineError) as caught:
        _create(db, plant, **values)
    assert caught.value.code == code
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kks_objects").fetchone()[0] == 0


def test_unknown_field_and_strict_dictionary(db, plant):
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.create(tx, plant["dataset"]["id"], {"nope": 1})
    assert caught.value.code == "field_unknown"
    with db.write(ENGINEER) as tx:
        structure.update_field(tx, plant["fields"]["maker"]["id"], input_mode="strict")
    with pytest.raises(EngineError) as caught:
        _create(db, plant, kks="A1", maker="Неизвестный завод")
    assert caught.value.code == "value_choice"


def test_update_checks_version_and_skips_empty_change(db, plant):
    keys = plant["keys"]
    record = _create(db, plant, kks="A1", name="Насос")
    with db.write(ENGINEER) as tx:
        same = records.update(tx, record["id"], {keys["name"]: "Насос"}, 1)
    assert same["version"] == 1  # правка без изменений не создаёт версию
    with db.write(ENGINEER) as tx:
        changed = records.update(tx, record["id"], {keys["name"]: "Насос ПЭН"}, 1)
    assert changed["version"] == 2
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.update(tx, record["id"], {keys["name"]: "Другое"}, 1)
    assert caught.value.code == "version_conflict"
    assert caught.value.details == {"current_version": 2}


def test_history_restores_any_version_and_rolls_back(db, plant):
    keys = plant["keys"]
    record = _create(db, plant, kks="A1", name="Первое", power=10)
    with db.write(ENGINEER) as tx:
        records.update(tx, record["id"], {keys["name"]: "Второе", keys["power"]: None}, 1)
    with db.write(ENGINEER) as tx:
        records.update(tx, record["id"], {keys["name"]: "Третье"}, 2)
    with db.read() as conn:
        first = records.doc_at(conn, record["id"], 1)
        second = records.doc_at(conn, record["id"], 2)
        log = records.history(conn, record["id"])
    assert first[keys["name"]] == "Первое" and first[keys["power"]] == 10
    assert second[keys["name"]] == "Второе" and keys["power"] not in second
    assert [item["version"] for item in log] == [3, 2, 1]
    assert log[1]["changes"][keys["power"]] == [10, None]  # хранится разница
    assert log[2]["changes"][keys["name"]] == [None, "Первое"]  # первая версия восстановлена
    assert log[0]["actor_label"] == "Инженер Петров"
    with db.write(ENGINEER) as tx:
        rolled = records.rollback(tx, record["id"], 1, 3)
    assert rolled["version"] == 4
    assert rolled["doc"] == first
    with db.read() as conn:
        assert records.history(conn, record["id"])[0]["restored_from"] == 1


def test_archive_keeps_links_and_blocks_edit(db, plant):
    keys = plant["keys"]
    record = _create(db, plant, kks="A1")
    with db.write(ENGINEER) as tx:
        archived = records.set_archived(tx, record["id"], True, 1)
    assert archived["archived_at"] and archived["version"] == 2
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM record_kks_links").fetchone()[0] == 1
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.update(tx, record["id"], {keys["name"]: "x"}, 2)
    assert caught.value.code == "record_archived"
    with db.write(ENGINEER) as tx:
        restored = records.set_archived(tx, record["id"], False, 2)
    assert restored["archived_at"] is None and restored["version"] == 3


def test_create_many_is_atomic(db, plant):
    keys = plant["keys"]
    rows = [{keys["kks"]: f"B{number}", keys["power"]: number} for number in range(50)]
    rows[30] = {keys["kks"]: "B30", keys["power"]: "не число"}
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.create_many(tx, plant["dataset"]["id"], rows)
    assert caught.value.details["row"] == 30
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kks_objects").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'record.%'").fetchone()[
                0
            ]
            == 0
        )


def test_audit_is_written_with_the_change(db, plant):
    record = _create(db, plant, kks="A1")
    with db.read() as conn:
        events = audit.list_events(conn, entity_id=record["id"])
    assert [event["action"] for event in events] == ["record.create"]
    assert events[0]["actor_id"] == ENGINEER.id


def test_idempotency_key_returns_first_result(db, plant):
    keys = plant["keys"]
    request = {keys["kks"]: "A1"}

    def make(tx):
        return idempotency.run(
            tx,
            "records.create",
            "key-1",
            request,
            lambda: records.create(tx, plant["dataset"]["id"], request)["id"],
        )

    with db.write(ENGINEER) as tx:
        first = make(tx)
    with db.write(ENGINEER) as tx:
        second = make(tx)
    assert first == second
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        idempotency.run(tx, "records.create", "key-1", {"other": 1}, lambda: None)
    assert caught.value.code == "idempotency_mismatch"
