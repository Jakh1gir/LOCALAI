"""Ссылки, уникальные поля, граф и правила кода через API (K-301)."""

from __future__ import annotations

import pytest

from server.app import create_app

from .asgi import Client


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "links.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


@pytest.fixture
def plant(admin):
    site = admin.post("/sites", {"name": "ТЭЦ"}).json()
    cable = admin.post("/types", {"name": "Кабель"}).json()
    admin.post(
        f"/types/{cable['id']}/fields",
        {"name": "Номер", "kind": "text", "key": "number", "unique": True},
    )
    signal = admin.post("/types", {"name": "Сигнал"}).json()
    for body in (
        {"name": "Код сигнала", "kind": "text", "key": "code", "unique": True},
        {"name": "Аппарат", "kind": "kks", "key": "device"},
        {"name": "Кабель", "kind": "record", "key": "cable", "target_type_id": cable["id"]},
        {"name": "Шкаф", "kind": "kks", "key": "cabinet", "kks_role": "route_end"},
        {"name": "Клеммы", "kind": "text", "key": "terminals", "contact_for": "cabinet"},
    ):
        reply = admin.post(f"/types/{signal['id']}/fields", body)
        assert reply.status == 201, reply.json()
    cables = admin.post(
        "/datasets", {"site_id": site["id"], "type_id": cable["id"], "name": "Кабели"}
    ).json()
    signals = admin.post(
        "/datasets", {"site_id": site["id"], "type_id": signal["id"], "name": "Сигналы"}
    ).json()
    made = admin.post(f"/datasets/{cables['id']}/records", {"values": {"number": "5011"}}).json()
    rows = [
        admin.post(
            f"/datasets/{signals['id']}/records",
            {
                "values": {
                    "code": f"01MAJ81AA203{suffix}",
                    "device": "01MAJ81AA203",
                    "cable": "5011",
                    "cabinet": "00CVA01GH002",
                    "terminals": terminals,
                }
            },
        ).json()
        for suffix, terminals in (("XB01", "31-32"), ("XB02", "33-34"))
    ]
    return {"site": site, "cable": made, "signals": signals["id"], "rows": rows}


def test_record_shows_who_links_to_it(admin, plant):
    reply = admin.get(f"/records/{plant['cable']['id']}").json()
    assert sorted(item["record_id"] for item in reply["backlinks"]) == sorted(
        row["id"] for row in plant["rows"]
    )
    assert set(reply["backlink_refs"].values()) == {"01MAJ81AA203XB01", "01MAJ81AA203XB02"}
    signal = admin.get(f"/records/{plant['rows'][0]['id']}").json()
    assert signal["refs"]["records"] == {plant["cable"]["id"]: "5011"}


def test_duplicate_signal_code_is_a_conflict(admin, plant):
    reply = admin.post(
        f"/datasets/{plant['signals']}/records",
        {"values": {"code": "01maj81aa203xb01", "device": "01MAJ81AA203"}},
    )
    assert reply.status == 409 and reply.json()["error"]["code"] == "value_not_unique"


def test_signal_code_resolves_to_the_device_and_graph_follows(admin, plant):
    site = plant["site"]["id"]
    found = admin.get(f"/sites/{site}/kks/resolve", code="01MAJ81AA203XB02").json()
    assert found["code"] == "01MAJ81AA203" and found["signal"] == "XB02"
    around = admin.get("/graph/neighbourhood", node=f"kks:{found['id']}", depth=2).json()
    assert {node["label"] for node in around["nodes"]} >= {"5011", "00CVA01GH002"}
    card = admin.get(f"/kks/{found['id']}").json()
    assert card["via_links"][0]["records"] == 1
    bad = admin.get("/graph/neighbourhood", node="что-то")
    assert bad.status == 400 and bad.json()["error"]["field"] == "node"


def test_kks_rules_are_read_by_all_and_changed_by_structure(app, admin, plant):
    rules = admin.get("/kks/rules").json()
    assert rules["example"] == {
        "code": "01MAJ81AA203XB01",
        "object": "01MAJ81AA203",
        "signal": "XB01",
    }
    changed = admin.put("/kks/rules", {"signal_pattern": "X[A-Z][0-9]{2}|-[0-9]{2}"})
    assert changed.status == 200
    site = plant["site"]["id"]
    assert admin.get(f"/sites/{site}/kks/resolve", code="01MAJ81AA203-03").json()["signal"] == "03"
    bad = admin.put("/kks/rules", {"signal_pattern": "("})
    assert bad.status == 400 and bad.json()["error"]["code"] == "kks_rule_invalid"
