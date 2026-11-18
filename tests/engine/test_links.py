"""Один ККС — много строк: уникальные поля, ссылки, контакты, граф, правила кода (K-301).

Данные — строки из кабельной базы владельца: у аппарата 01MAJ81AA203 три
сигнала, кабель 5025 несёт сигналы двух разных аппаратов.
"""

from __future__ import annotations

import pytest

from engine import EngineError, batch, graph, kks, kks_rules, links, query, records, structure

from .conftest import ENGINEER

SIGNALS = [
    ("01MAG01CQ002XQ01", "01MAG01CQ002", "AI", "5025", "11-12", "00CVA01GH001", "LU001"),
    ("01MAG01CQ001XQ01", "01MAG01CQ001", "AI", "5025", "9-10", "00CVA01GH001", "LU001"),
    ("01MAJ81AA203XB62", "01MAJ81AA203", "DO", "5024", "3-4", "00CVA01GH002", "LU002"),
    ("01MAJ81AA203XB01", "01MAJ81AA203", "DI", "5011", "31-32", "00CVA01GH002", "LU002"),
    ("01MAJ81AA203XB02", "01MAJ81AA203", "DI", "5011", "33-34", "00CVA01GH002", "LU002"),
]


def _everything(_dataset_id):
    return frozenset()


@pytest.fixture
def cables(db):
    with db.write(ENGINEER) as tx:
        site = structure.create_site(tx, "ТЭЦ")
        cable = structure.create_type(tx, "Кабель")
        structure.add_field(tx, cable["id"], "Номер кабеля", "text", key="number", unique=True)
        structure.add_field(tx, cable["id"], "Откуда", "kks", key="start", kks_role="route_start")
        structure.add_field(tx, cable["id"], "Куда", "kks", key="finish", kks_role="route_end")
        signal = structure.create_type(tx, "Сигнал")
        add = lambda *args, **kw: structure.add_field(tx, signal["id"], *args, **kw)
        add("Код сигнала", "text", key="code", unique=True)
        add("Аппарат", "kks", key="device", kks_role="object", required=True)
        add("Тип сигнала", "text", key="kind")
        add("Кабель", "record", key="cable", target_type_id=cable["id"])
        add("Шкаф ПТК", "kks", key="cabinet", kks_role="route_end")
        add("Клеммы ПТК", "text", key="terminals", contact_for="cabinet")
        add("Шкаф I/O", "kks", key="io", kks_role="related")
        cable_set = structure.create_dataset(tx, site["id"], cable["id"], "Кабели")
        signal_set = structure.create_dataset(tx, site["id"], signal["id"], "Сигналы")
        for number, start in (
            ("5025", "01MAG01CQ002"),
            ("5024", "01MAJ81AA203"),
            ("5011", "01MAJ81AA203"),
        ):
            records.create(
                tx, cable_set["id"], {"number": number, "start": start, "finish": "00CVA01GH001"}
            )
        ids = records.create_many(
            tx,
            signal_set["id"],
            [
                {
                    "code": code,
                    "device": device,
                    "kind": kind,
                    "cable": cable_no,  # номер кабеля находит запись кабеля (DATA-13)
                    "cabinet": cabinet,
                    "terminals": terminals,
                    "io": io,
                }
                for code, device, kind, cable_no, terminals, cabinet, io in SIGNALS
            ],
        )
    return {"site": site, "cables": cable_set["id"], "signals": signal_set["id"], "ids": ids}


def test_one_kks_object_carries_several_signal_rows(db, cables):
    with db.read() as conn:
        device = kks.resolve(conn, cables["site"]["id"], "01MAJ81AA203")
        card = kks.card(conn, device["id"])
        objects = conn.execute("SELECT COUNT(*) FROM kks_objects").fetchone()[0]
    usage = {(item["dataset_id"], item["role"]): item["records"] for item in card["usage"]}
    assert usage[(cables["signals"], "object")] == 3  # три сигнала у одного ККС
    assert card["via_links"] == [{"dataset_id": cables["cables"], "records": 2}]  # 5024 и 5011
    assert objects == 7  # 3 аппарата и 4 шкафа; коды сигналов в каталог не попали


def test_signal_code_finds_the_device(db, cables):
    with db.read() as conn:
        found = kks_rules.resolve(conn, cables["site"]["id"], "01maj81aa203xb01")
        plain = kks_rules.resolve(conn, cables["site"]["id"], "01MAJ81AA203")
        missing = kks_rules.resolve(conn, cables["site"]["id"], "99XXX00XB01")
    assert found["code"] == "01MAJ81AA203" and found["via"] == "signal"
    assert found["signal"] == "XB01"
    assert plain["via"] == "code" and missing is None


def test_unique_value_is_checked_case_insensitively_and_freed_by_archive(db, cables):
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.create(
            tx, cables["signals"], {"code": "01maj81aa203xb01", "device": "01MAJ81AA203"}
        )
    assert caught.value.code == "value_not_unique" and caught.value.field == "code"
    assert caught.value.details["record_id"] == cables["ids"][3]
    with db.write(ENGINEER) as tx:
        records.set_archived(tx, cables["ids"][3], True, 1)
        again = records.create(
            tx, cables["signals"], {"code": "01MAJ81AA203XB01", "device": "01MAJ81AA203"}
        )
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.set_archived(tx, cables["ids"][3], False, 2)  # значение уже занято
    assert caught.value.code == "value_not_unique"
    assert again["doc"]["code"] == "01MAJ81AA203XB01"


def test_making_a_field_unique_refuses_existing_duplicates(db, cables):
    with db.read() as conn:
        kind = next(
            field
            for field in structure.load_schema(
                conn, structure.load_dataset(conn, cables["signals"])["type_id"]
            ).fields
            if field.key == "kind"
        )
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        structure.update_field(tx, kind.id, unique=True)
    assert caught.value.code == "unique_has_duplicates"


def test_record_link_by_key_refs_filter_and_backlinks(db, cables):
    with db.read() as conn:
        page = query.list_records(
            conn, cables["signals"], {"where": [{"field": "cable", "op": "eq", "value": "5011"}]}
        )
        cable_id = page["items"][0]["doc"]["cable"]
        seq = conn.execute("SELECT seq FROM records WHERE id = ?", (cable_id,)).fetchone()[0]
        incoming = links.backlinks(conn, seq)
    assert sorted(item["doc"]["code"] for item in page["items"]) == [
        "01MAJ81AA203XB01",
        "01MAJ81AA203XB02",
    ]
    assert page["refs"]["records"] == {cable_id: "5011"}
    assert len(incoming) == 2 and {item["field_key"] for item in incoming} == {"cable"}
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        records.create(tx, cables["signals"], {"code": "X1", "device": "A1", "cable": "9999"})
    assert caught.value.code == "record_link_missing" and caught.value.field == "cable"


def test_referenced_record_cannot_be_purged(db, cables):
    with db.read() as conn:
        cable_id = query.list_records(
            conn, cables["cables"], {"where": [{"field": "number", "op": "eq", "value": "5011"}]}
        )["items"][0]["id"]
    with db.write(ENGINEER) as tx:
        records.set_archived(tx, cable_id, True, 1)
    with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
        batch.purge_many(tx, cables["cables"], [{"id": cable_id, "expected_version": 2}])
    assert caught.value.code == "record_referenced"


def test_graph_shows_signals_cables_and_cabinets_with_contacts(db, cables):
    with db.read() as conn:
        device = kks.resolve(conn, cables["site"]["id"], "01MAJ81AA203")
        around = graph.neighbourhood(conn, f"kks:{device['id']}", _everything, 2)
        cabinet = kks.resolve(conn, cables["site"]["id"], "00CVA01GH002")
        route = graph.path(conn, f"kks:{device['id']}", f"kks:{cabinet['id']}", _everything)
    labels = {node["label"] for node in around["nodes"]}
    assert {"01MAJ81AA203XB01", "5011", "00CVA01GH002", "LU002"} <= labels
    contacts = {edge["contact"] for edge in around["edges"] if edge["field"] == "cabinet"}
    assert contacts == {"3-4", "31-32", "33-34"}
    assert route["found"] and len(route["edges"]) == 2  # аппарат → сигнал → шкаф


def test_graph_respects_hidden_datasets_and_fields(db, cables):
    with db.read() as conn:
        device = kks.resolve(conn, cables["site"]["id"], "01MAJ81AA203")
        no_cables = graph.neighbourhood(
            conn,
            f"kks:{device['id']}",
            lambda dataset_id: None if dataset_id == cables["cables"] else frozenset({"cabinet"}),
            2,
        )
    kinds = {(node["kind"], node.get("dataset_id")) for node in no_cables["nodes"]}
    assert ("record", cables["cables"]) not in kinds
    assert all(edge["field"] != "cabinet" for edge in no_cables["edges"])


def test_kks_rule_can_be_changed_and_is_validated(db, cables):
    with db.write(ENGINEER) as tx:
        kks_rules.set_rule(tx, "-[0-9]{2}")
    with db.read() as conn:
        assert kks_rules.split(conn, "01TK12CKA110-03") == ("01TK12CKA110", "03")
        assert kks_rules.split(conn, "01MAJ81AA203XB01")[1] is None
    for bad in ("(", "", ".*"):
        with pytest.raises(EngineError) as caught, db.write(ENGINEER) as tx:
            kks_rules.set_rule(tx, bad)
        assert caught.value.code == "kks_rule_invalid"
