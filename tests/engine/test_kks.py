"""Каталог ККС: уникальность, переименование, псевдонимы, связи (DATA-06, DATA-07, AT-03)."""

from __future__ import annotations

import pytest

from engine import EngineError, kks, query, records, structure

from .conftest import ENGINEER


def _record(db, plant, code, **extra):
    keys = plant["keys"]
    with db.write(ENGINEER) as tx:
        return records.create(tx, plant["dataset"]["id"], {keys["kks"]: code, **extra})


def test_code_is_unique_per_site_ignoring_case(db, plant):
    first = _record(db, plant, "10LAC10AP001")
    second = _record(db, plant, " 10lac10ap001")
    key = plant["keys"]["kks"]
    assert first["doc"][key] == second["doc"][key]  # один объект на площадке
    with db.write(ENGINEER) as tx:
        other_site = structure.create_site(tx, "ТЭЦ-2")
        twin = kks.create(tx, other_site["id"], "10LAC10AP001")
    assert twin["id"] != first["doc"][key]  # на другой площадке код свободен
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        kks.create(tx, plant["site"]["id"], "10LAC10AP001")
    assert caught.value.code == "kks_code_taken"


def test_rename_keeps_uuid_links_and_old_code_as_alias(db, plant):
    record = _record(db, plant, "10LAC10AP001")
    kks_id = record["doc"][plant["keys"]["kks"]]
    with db.write(ENGINEER) as tx:
        renamed = kks.rename(tx, kks_id, "10LAC10AP002")
    assert renamed["id"] == kks_id and renamed["code"] == "10LAC10AP002"
    with db.read() as conn:
        by_old = kks.resolve(conn, plant["site"]["id"], "10lac10ap001")
        page = query.list_records(conn, plant["dataset"]["id"], {"kks": kks_id})
        found = query.list_records(conn, plant["dataset"]["id"], {"search": "AP002"})
        card = kks.card(conn, kks_id)
    assert by_old["id"] == kks_id and by_old["via"] == "alias"
    assert [item["id"] for item in page["items"]] == [record["id"]]
    assert page["refs"]["kks"][kks_id] == "10LAC10AP002"  # запись показывает новый код
    assert len(found["items"]) == 1  # поиск знает новый код
    assert [alias["alias"] for alias in card["aliases"]] == ["10LAC10AP001"]
    assert card["usage"] == [{"dataset_id": plant["dataset"]["id"], "role": "object", "records": 1}]
    with pytest.raises(EngineError) as caught:
        _ = _record(db, plant, "X1")
        with db.write(ENGINEER) as tx:
            other = kks.resolve(tx.conn, plant["site"]["id"], "X1")
            kks.rename(tx, other["id"], "10LAC10AP001")  # занят псевдонимом
    assert caught.value.code == "kks_code_taken"


def test_rename_back_reuses_own_alias(db, plant):
    record = _record(db, plant, "A1")
    kks_id = record["doc"][plant["keys"]["kks"]]
    with db.write(ENGINEER) as tx:
        kks.rename(tx, kks_id, "A2")
    with db.write(ENGINEER) as tx:
        back = kks.rename(tx, kks_id, "A1")
    with db.read() as conn:
        aliases = [alias["alias"] for alias in kks.card(conn, kks_id)["aliases"]]
    assert back["code"] == "A1" and aliases == ["A2"]


def test_archived_object_keeps_links_but_blocks_new_ones(db, plant):
    record = _record(db, plant, "A1")
    kks_id = record["doc"][plant["keys"]["kks"]]
    with db.write(ENGINEER) as tx:
        kks.update(tx, kks_id, archived=True)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM record_kks_links").fetchone()[0] == 1
    with pytest.raises(EngineError) as caught:
        _record(db, plant, "a1")
    assert caught.value.code == "kks_archived"
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        kks.create(tx, plant["site"]["id"], "A1")  # код остаётся за объектом
    assert caught.value.code == "kks_code_taken"


def test_route_points_keep_order(db, plant):
    with db.write(ENGINEER) as tx:
        cable = structure.create_type(tx, "Кабель")
        start = structure.add_field(tx, cable["id"], "Откуда", "kks", kks_role="route_start")
        via = structure.add_field(
            tx, cable["id"], "Через", "kks", kks_role="route_via", multiple=True
        )
        end = structure.add_field(tx, cable["id"], "Куда", "kks", kks_role="route_end")
        cables = structure.create_dataset(tx, plant["site"]["id"], cable["id"], "Кабели")
        record = records.create(
            tx,
            cables["id"],
            {start["key"]: "ШУ-1", via["key"]: ["КК-1", "КК-2", "КК-1"], end["key"]: "ШУ-2"},
        )
    with db.read() as conn:
        seq = conn.execute("SELECT seq FROM records WHERE id = ?", (record["id"],)).fetchone()[0]
        links = kks.record_links(conn, seq)
        objects = conn.execute("SELECT COUNT(*) FROM kks_objects").fetchone()[0]
    route = [
        (link["role"], link["ordinal"], link["code"])
        for link in links
        if link["role"] == "route_via"
    ]
    assert route == [("route_via", 0, "КК-1"), ("route_via", 1, "КК-2"), ("route_via", 2, "КК-1")]
    assert len(links) == 5 and objects == 4  # повтор точки — тот же объект на другой позиции
    with db.read() as conn:
        through = query.count_records(
            conn, cables["id"], {"where": [{"field": via["key"], "op": "eq", "value": "кк-2"}]}
        )
        not_from = query.count_records(
            conn, cables["id"], {"where": [{"field": start["key"], "op": "ne", "value": "ШУ-1"}]}
        )
        part = query.count_records(
            conn, cables["id"], {"where": [{"field": via["key"], "op": "starts", "value": "кк"}]}
        )
    assert (through, not_from, part) == (1, 0, 1)  # кратность связей не множит записи


def test_search_suggestions_rank_prefix_first(db, plant):
    for code in ("10LAC10AP001", "20LAC10AP001", "10HFB10AN001"):
        _record(db, plant, code)
    with db.read() as conn:
        found = [item["code"] for item in kks.search(conn, plant["site"]["id"], "10")]
    assert found == ["10HFB10AN001", "10LAC10AP001", "20LAC10AP001"]
