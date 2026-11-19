"""Общий поиск: ККС и записи всех видимых реестров (ТЗ 5.1, AI-16, ROLE-03). Данные вымышленные."""

from __future__ import annotations

import pytest

from server.app import create_app

from .asgi import Client

NEW_PASSWORD = "свой-пароль-пользователя"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "search.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


@pytest.fixture
def plant(admin):
    site = admin.post("/sites", {"name": "ТЭЦ"}).json()
    kind = admin.post("/types", {"name": "Сигнал"}).json()
    fields = {}
    for spec in (
        {"name": "Код", "kind": "text", "key": "code", "unique": True},
        {"name": "Описание", "kind": "text", "key": "text"},
        {"name": "Секрет", "kind": "text", "key": "secret"},
        {"name": "ККС", "kind": "kks", "key": "kks", "kks_role": "object"},
    ):
        fields[spec["key"]] = admin.post(f"/types/{kind['id']}/fields", spec).json()
    datasets = {
        name: admin.post(
            "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": name}
        ).json()
        for name in ("Сигналы", "Закрытые")
    }
    rows = {
        "signal": ("Сигналы", {"code": "10LAC10AP001XQ01", "text": "давление насоса", "kks": "10LAC10AP001"}),
        "mention": ("Сигналы", {"code": "S-2", "text": "см. 10LAC10AP001XQ01 в шкафу", "secret": "пароль-шкафа"}),
        "closed": ("Закрытые", {"code": "S-3", "text": "давление насоса резерв"}),
        "old": ("Сигналы", {"code": "S-4", "text": "давление насоса старое"}),
    }  # fmt: skip
    records = {}
    for key, (dataset, values) in rows.items():
        records[key] = admin.post(
            f"/datasets/{datasets[dataset]['id']}/records", {"values": values}
        ).json()
    old = records["old"]
    admin.post(f"/records/{old['id']}/archive", {"expected_version": old["version"]})
    device = admin.get(f"/sites/{site['id']}/kks/resolve", code="10LAC10AP001").json()
    admin.post(f"/kks/{device['id']}/aliases", {"alias": "ПЭН-1"})
    admin.post(f"/sites/{site['id']}/kks", {"code": "10LAC10AP002"})
    return {"site": site, "type": kind, "fields": fields, "datasets": datasets, "records": records}


def _user(app, admin, login, permissions):
    role = admin.post("/roles", {"name": f"Роль {login}"}).json()
    assert admin.put(f"/roles/{role['id']}/permissions", {"permissions": permissions}).status == 200
    created = admin.post("/users", {"login": login, "name": login.title(), "role_id": role["id"]})
    client = Client(app, login=login, password=created.json()["temporary_password"])
    client.post(
        "/auth/password",
        {"current_password": created.json()["temporary_password"], "new_password": NEW_PASSWORD},
    )
    return client


def test_signal_code_finds_device_and_puts_its_row_first(admin, plant):
    found = admin.get("/search", q="10LAC10AP001XQ01").json()
    first = found["kks"][0]
    assert first["code"] == "10LAC10AP001" and first["exact"] and first["via"] == "signal"
    assert first["signal"] == "XQ01"  # AI-16
    labels = [item["label"] for item in found["records"]]
    assert labels == ["10LAC10AP001XQ01", "S-2"]  # совпадение с ключом — первым
    assert found["records"][0]["match"] == {"field": "Код", "text": "10LAC10AP001XQ01"}
    assert found["records"][1]["match"]["field"] == "Описание"
    assert found["records"][0]["dataset"] == "Сигналы" and found["documents"] == []


def test_kks_by_part_and_alias(admin, plant):
    part = admin.get("/search", q="lac10ap00").json()["kks"]
    assert [item["code"] for item in part] == ["10LAC10AP001", "10LAC10AP002"]
    assert not any(item["exact"] for item in part)
    alias = admin.get("/search", q="ПЭН-1").json()["kks"][0]
    assert alias["code"] == "10LAC10AP001" and alias["exact"] and alias["via"] == "alias"


def test_words_find_records_in_every_visible_dataset_but_not_archived(admin, plant):
    found = admin.get("/search", q="ДАВЛЕНИЕ насоса").json()
    assert {item["label"] for item in found["records"]} == {"10LAC10AP001XQ01", "S-3"}
    short = admin.get("/search", q="10").json()
    assert short["records"] == [] and len(short["kks"]) == 2  # короткая строка — только ККС
    limited = admin.get("/search", q="давление", limit=1).json()
    assert len(limited["records"]) == 1 and limited["more"]["records"] is True


def test_rights_hide_datasets_and_hidden_fields(app, admin, plant):
    signals = plant["datasets"]["Сигналы"]["id"]
    reader = _user(
        app,
        admin,
        "reader",
        [
            {"scope": "dataset", "target": signals, "action": "view"},
            {"scope": "field", "target": plant["fields"]["secret"]["id"], "action": "hidden"},
        ],
    )
    found = reader.get("/search", q="давление насоса").json()
    assert [item["label"] for item in found["records"]] == [
        "10LAC10AP001XQ01"
    ]  # «Закрытые» не видны
    assert reader.get("/search", q="пароль-шкафа").json()["records"] == []  # скрытое поле
    assert [item["label"] for item in admin.get("/search", q="пароль-шкафа").json()["records"]] == [
        "S-2"
    ]
    still = reader.get("/search", q="в шкафу").json()["records"]
    assert [item["label"] for item in still] == ["S-2"]  # видимые поля той же записи ищутся


@pytest.mark.parametrize(
    ("query", "code"), [("   ", "search_empty"), ("я" * 201, "search_too_long")]
)
def test_bad_queries(admin, query, code):
    reply = admin.get("/search", q=query)
    assert reply.status == 400 and reply.json()["error"]["code"] == code
    assert admin.get("/search", q="насос", limit=0).json()["error"]["code"] == "limit_invalid"
