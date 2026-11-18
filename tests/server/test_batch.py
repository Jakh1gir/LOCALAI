"""Пакетная правка, удаление из архива и значения поля через API (K-103)."""

from __future__ import annotations

import pytest

from server.app import create_app

from .asgi import Client

NEW_PASSWORD = "свой-пароль-пользователя"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "batch.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


@pytest.fixture
def plant(admin):
    site = admin.post("/sites", {"name": "ТЭЦ"}).json()
    kind = admin.post("/types", {"name": "Насос"}).json()
    fields = {
        key: admin.post(
            f"/types/{kind['id']}/fields", {"name": name, "kind": "text", "key": key}
        ).json()
        for key, name in (("name", "Название"), ("shop", "Цех"), ("price", "Цена"))
    }
    dataset = admin.post(
        "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": "Насосы"}
    ).json()
    rows = [
        admin.post(
            f"/datasets/{dataset['id']}/records",
            {"values": {"name": f"Насос {number}", "shop": ["Котельный", "Турбинный"][number % 2]}},
        ).json()
        for number in range(6)
    ]
    return {"dataset": dataset["id"], "fields": fields, "rows": rows}


def _items(rows, version=None):
    return [{"id": row["id"], "expected_version": version or row["version"]} for row in rows]


def _user(app, admin, role_name, extra=()):
    roles = {role["name"]: role for role in admin.get("/roles").json()["items"]}
    role = admin.get(f"/roles/{roles[role_name]['id']}").json()
    if extra:
        copy = admin.post("/roles", {"name": f"{role_name} с ограничением"}).json()
        admin.put(
            f"/roles/{copy['id']}/permissions",
            {"permissions": [*role["permissions"], *extra]},
        )
        role = copy
    login = f"user{len(admin.get('/users').json()['items'])}"
    created = admin.post("/users", {"login": login, "name": login, "role_id": role["id"]}).json()
    client = Client(app, login=login, password=created["temporary_password"])
    client.post(
        "/auth/password",
        {"current_password": created["temporary_password"], "new_password": NEW_PASSWORD},
    )
    return client


def test_value_counts_answer_for_the_filter_popup(admin, plant):
    reply = admin.post(f"/datasets/{plant['dataset']}/records/values", {"field": "shop"}, key="")
    assert reply.status == 200
    assert {item["value"]: item["count"] for item in reply.json()["items"]} == {
        "Котельный": 3,
        "Турбинный": 3,
    }
    narrowed = admin.post(
        f"/datasets/{plant['dataset']}/records/values",
        {"field": "shop", "search": "насос 1", "limit": 5},
        key="",
    ).json()
    assert narrowed["items"] == [{"value": "Турбинный", "count": 1}]


def test_batch_update_through_the_api(admin, plant):
    dataset, rows = plant["dataset"], plant["rows"][:3]
    reply = admin.post(
        f"/datasets/{dataset}/records/batch-update",
        {"items": _items(rows), "values": {"shop": "Склад"}},
    )
    assert reply.status == 200 and reply.json()["changed"] == 3
    stale = admin.post(
        f"/datasets/{dataset}/records/batch-update",
        {"items": _items(rows), "values": {"shop": "Ещё раз"}},
    )
    assert stale.status == 409
    assert stale.json()["error"]["details"] == {
        "current_version": 2,
        "row": 0,
        "record_id": rows[0]["id"],
    }
    no_key = admin.post(
        f"/datasets/{dataset}/records/batch-update",
        {"items": _items(rows, 2), "values": {"shop": "Ещё раз"}},
        key="",
    )
    assert no_key.status == 400 and no_key.json()["error"]["code"] == "idempotency_key_required"


def test_purge_needs_its_own_right(app, admin, plant):
    dataset, row = plant["dataset"], plant["rows"][0]
    admin.post(f"/records/{row['id']}/archive", {"expected_version": 1})
    engineer = _user(app, admin, "Инженер")
    denied = engineer.post(f"/datasets/{dataset}/records/purge", {"items": _items([row], 2)})
    assert denied.status == 403
    assert denied.json()["error"]["details"] == {"permission": "dataset.purge"}
    done = admin.post(f"/datasets/{dataset}/records/purge", {"items": _items([row], 2)})
    assert done.status == 200 and done.json() == {"purged": 1}
    assert admin.get(f"/records/{row['id']}").status == 404


def test_field_limits_apply_to_batch_and_values(app, admin, plant):
    dataset = plant["dataset"]
    price, shop = plant["fields"]["price"], plant["fields"]["shop"]
    client = _user(
        app,
        admin,
        "Инженер",
        [
            {"scope": "field", "target": price["id"], "action": "hidden"},
            {"scope": "field", "target": shop["id"], "action": "readonly"},
        ],
    )
    locked = client.post(
        f"/datasets/{dataset}/records/batch-update",
        {"items": _items(plant["rows"][:2]), "values": {"shop": "Склад"}},
    )
    assert locked.status == 403 and locked.json()["error"]["field"] == "shop"
    hidden = client.post(f"/datasets/{dataset}/records/values", {"field": "price"}, key="")
    assert hidden.status == 400 and hidden.json()["error"]["field"] == "field"
    assert (
        client.post(f"/datasets/{dataset}/records/values", {"field": "shop"}, key="").status == 200
    )
