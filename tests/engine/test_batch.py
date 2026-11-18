"""Пакетная правка, окончательное удаление, подсчёт по значениям (F-007, F-009, K-103)."""

from __future__ import annotations

import pytest

from engine import EngineError, audit, batch, facets, query, records, structure

from .conftest import ENGINEER


@pytest.fixture
def filled(db, plant):
    keys = plant["keys"]
    rows = [
        {
            keys["kks"]: f"10LAC{number:02d}",
            keys["name"]: f"Насос {number}",
            keys["maker"]: ["Siemens", "ABB", None][number % 3],
            keys["tags"]: ["резерв", "осмотр"] if number % 2 else ["резерв"],
            keys["spare"]: number % 4 == 0,
        }
        for number in range(12)
    ]
    with db.write(ENGINEER) as tx:
        ids = records.create_many(tx, plant["dataset"]["id"], rows)
    return {**plant, "ids": ids}


def _items(ids, version=1):
    return [{"id": record_id, "expected_version": version} for record_id in ids]


def test_batch_update_gives_each_record_its_own_version_and_history(db, filled):
    keys, dataset = filled["keys"], filled["dataset"]["id"]
    chosen = filled["ids"][:4]
    with db.write(ENGINEER) as tx:
        result = batch.update_many(
            tx, dataset, _items(chosen), {keys["name"]: "Насос резервный", keys["power"]: "7,5"}
        )
    assert result["changed"] == 4
    assert [item["version"] for item in result["items"]] == [2, 2, 2, 2]
    with db.read() as conn:
        first = records.get(conn, chosen[0])
        log = records.history(conn, chosen[0])
        events = audit.list_events(conn, entity_id=dataset)
        untouched = records.get(conn, filled["ids"][5])
    assert first["doc"][keys["name"]] == "Насос резервный"
    assert first["doc"][keys["power"]] == 7.5
    assert log[0]["changes"][keys["power"]] == [None, 7.5]
    assert events[0]["action"] == "record.update_many"
    assert events[0]["details"] == {
        "count": 4,
        "changed": 4,
        "fields": sorted([keys["name"], keys["power"]]),
    }
    assert untouched["version"] == 1


def test_batch_update_skips_records_that_already_have_the_value(db, filled):
    keys, dataset = filled["keys"], filled["dataset"]["id"]
    chosen = filled["ids"][:2]
    with db.write(ENGINEER) as tx:
        batch.update_many(tx, dataset, _items(chosen[:1]), {keys["name"]: "Одинаково"})
    with db.write(ENGINEER) as tx:
        result = batch.update_many(
            tx,
            dataset,
            [{"id": chosen[0], "expected_version": 2}, {"id": chosen[1], "expected_version": 1}],
            {keys["name"]: "Одинаково"},
        )
    assert result["changed"] == 1
    assert [item["version"] for item in result["items"]] == [2, 2]


@pytest.mark.parametrize(
    ("change", "code", "row"),
    [
        ("stale", "version_conflict", 2),
        ("value", "value_number", 0),
        ("other", "record_other_dataset", 1),
        ("archived", "record_archived", 1),
        ("twice", "items_duplicate", None),
    ],
)
def test_batch_update_is_all_or_nothing(db, filled, change, code, row):
    keys, dataset = filled["keys"], filled["dataset"]["id"]
    items = _items(filled["ids"][:3])
    patch = {keys["name"]: "Не сохранится"}
    if change == "stale":
        items[2]["expected_version"] = 5
    elif change == "value":
        patch[keys["power"]] = "много"
    elif change == "other":
        with db.write(ENGINEER) as tx:
            other = structure.create_dataset(
                tx, filled["site"]["id"], filled["type"]["id"], "Склад"
            )
            items[1]["id"] = records.create(tx, other["id"], {keys["kks"]: "X1"})["id"]
    elif change == "archived":
        with db.write(ENGINEER) as tx:
            records.set_archived(tx, items[1]["id"], True, 1)
        items[1]["expected_version"] = 2
    else:
        items[2] = dict(items[0])
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        batch.update_many(tx, dataset, items, patch)
    assert caught.value.code == code
    if row is not None:
        assert caught.value.details["row"] == row
    with db.read() as conn:
        names = {records.get(conn, item)["doc"][keys["name"]] for item in filled["ids"][:3]}
    assert "Не сохранится" not in names


def test_purge_removes_archived_records_with_history_links_and_search(db, filled):
    dataset = filled["dataset"]["id"]
    gone, kept = filled["ids"][:2], filled["ids"][2]
    with db.write(ENGINEER) as tx:
        for record_id in gone:
            records.set_archived(tx, record_id, True, 1)
    with db.write(ENGINEER) as tx:
        assert batch.purge_many(tx, dataset, _items(gone, 2)) == {"purged": 2}
    with db.read() as conn:
        for record_id in gone:
            with pytest.raises(EngineError):
                records.get(conn, record_id)
        left = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("records", "record_versions", "record_kks_links")
        }
        kks_objects = conn.execute("SELECT COUNT(*) FROM kks_objects").fetchone()[0]
        found = query.list_records(conn, dataset, {"search": "10LAC00", "archived": "any"})
        events = [event["action"] for event in audit.list_events(conn, entity_id=gone[0])]
        records.get(conn, kept)
    assert left == {"records": 10, "record_versions": 10, "record_kks_links": 10}
    assert kks_objects == 12  # объекты ККС остаются в каталоге
    assert found["items"] == []
    assert events[0] == "record.purge"


def test_purge_refuses_active_records_and_stale_versions(db, filled):
    dataset = filled["dataset"]["id"]
    active = filled["ids"][0]
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        batch.purge_many(tx, dataset, _items([active]))
    assert caught.value.code == "record_not_archived" and caught.value.details["row"] == 0
    with db.write(ENGINEER) as tx:
        records.set_archived(tx, active, True, 1)
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        batch.purge_many(tx, dataset, _items([active], 1))
    assert caught.value.code == "version_conflict"
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        batch.purge_many(tx, dataset, [])
    assert caught.value.code == "items_invalid"


def test_value_counts_follow_filters_and_name_choices(db, filled):
    keys, dataset = filled["keys"], filled["dataset"]["id"]
    with db.read() as conn:
        makers = facets.value_counts(conn, dataset, keys["maker"])
        tags = facets.value_counts(conn, dataset, keys["tags"])
        spare = facets.value_counts(conn, dataset, keys["spare"])
        narrowed = facets.value_counts(
            conn,
            dataset,
            keys["maker"],
            {"where": [{"field": keys["spare"], "op": "eq", "value": True}]},
            limit=1,
        )
    labels = makers["refs"]["choices"]
    shown = [(labels.get(item["value"]), item["count"]) for item in makers["items"]]
    assert sorted(shown[:2]) == [("ABB", 4), ("Siemens", 4)]
    assert shown[2] == (None, 4)  # запись без значения — своей группой, в конце равных
    assert makers["distinct"] == 3
    assert {item["value"]: item["count"] for item in tags["items"]} == {"резерв": 12, "осмотр": 6}
    assert {item["value"]: item["count"] for item in spare["items"]} == {False: 9, True: 3}
    assert narrowed["distinct"] == 3 and len(narrowed["items"]) == 1


@pytest.mark.parametrize(
    ("field", "options", "name"),
    [("нет", {}, "field"), ("name", {"limit": 0}, "limit"), ("name", {"limit": 201}, "limit")],
)
def test_value_counts_reject_bad_requests(db, filled, field, options, name):
    key = filled["keys"].get(field, field)
    with pytest.raises(EngineError) as caught, db.read() as conn:
        facets.value_counts(conn, filled["dataset"]["id"], key, **options)
    assert caught.value.code == "query_invalid" and caught.value.field == name


def test_value_counts_do_not_see_hidden_fields(db, filled):
    keys = filled["keys"]
    with pytest.raises(EngineError) as caught, db.read() as conn:
        facets.value_counts(
            conn, filled["dataset"]["id"], keys["maker"], hidden=frozenset({keys["maker"]})
        )
    assert caught.value.field == "field"
