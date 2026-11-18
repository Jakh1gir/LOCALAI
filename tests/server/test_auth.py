"""Вход, права на маршрутах, пользователи и роли через API (AT-20…AT-22, API-02)."""

from __future__ import annotations

import io
import re
import sys

import pytest

from server import admin as admin_cli
from server.app import create_app
from server.routes import ENDPOINTS

from .asgi import ADMIN_PASSWORD, Client

NEW_PASSWORD = "свой-пароль-пользователя"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "auth.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


@pytest.fixture
def plant(admin):
    site = admin.post("/sites", {"name": "ТЭЦ"}).json()
    kind = admin.post("/types", {"name": "Насос"}).json()
    name = admin.post(
        f"/types/{kind['id']}/fields", {"name": "Название", "kind": "text", "key": "name"}
    ).json()
    price = admin.post(
        f"/types/{kind['id']}/fields",
        {"name": "Цена", "kind": "number", "key": "price", "filterable": True},
    ).json()
    power = admin.post(
        f"/types/{kind['id']}/fields", {"name": "Мощность", "kind": "number", "key": "power"}
    ).json()
    datasets = [
        admin.post(
            "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": name_}
        ).json()
        for name_ in ("Насосы", "Секретные насосы")
    ]
    record = admin.post(
        f"/datasets/{datasets[0]['id']}/records",
        {"values": {"name": "Насос ПЭН", "price": 125000, "power": 500}},
    ).json()
    return {
        "type": kind,
        "fields": {"name": name, "price": price, "power": power},
        "datasets": datasets,
        "record": record,
    }


def _role(admin, name, permissions):
    role = admin.post("/roles", {"name": name}).json()
    reply = admin.put(f"/roles/{role['id']}/permissions", {"permissions": permissions})
    assert reply.status == 200, reply.json()
    return reply.json()


def _user(app, admin, login, role_id):
    """Пользователь, который уже сменил временный пароль."""
    created = admin.post("/users", {"login": login, "name": login.title(), "role_id": role_id})
    assert created.status == 201, created.json()
    client = Client(app, login=login, password=created.json()["temporary_password"])
    changed = client.post(
        "/auth/password",
        {
            "current_password": created.json()["temporary_password"],
            "new_password": NEW_PASSWORD,
        },
    )
    assert changed.status == 200, changed.json()
    return created.json(), client


def _view(dataset_id="*"):
    return {"scope": "dataset", "target": dataset_id, "action": "view"}


# Отказ по умолчанию ----------------------------------------------------------


def test_every_route_but_the_public_ones_requires_a_session(app):
    """API-02: без входа каждый закрытый маршрут отвечает 401, а не 404 или 200."""
    anonymous = Client(app, login=None)
    some_id = "00000000-0000-4000-8000-000000000000"
    for endpoint in ENDPOINTS:
        if endpoint.permission == "public":
            continue
        path = re.sub(r"\{\w+\}", some_id, endpoint.path)
        reply = anonymous.request(endpoint.method, path, {} if endpoint.body else None)
        assert reply.status == 401, (endpoint.method, endpoint.path, reply.json())
        assert reply.json()["error"]["code"] == "auth_required"


def test_health_tells_the_installer_that_no_admin_exists_yet(app):
    anonymous = Client(app, login=None)
    assert anonymous.get("/system/health").json()["setup_required"] is True
    Client(app)
    assert anonymous.get("/system/health").json()["setup_required"] is False


# Вход и сессия ----------------------------------------------------------------


def test_login_sets_a_strict_http_only_cookie_and_logout_closes_it(app):
    client = Client(app)
    me = client.get("/auth/me").json()
    assert me["user"]["login"] == "admin" and me["role"]["name"] == "Администратор"
    assert "admin" in me["permissions"]["sections"] and me["session_expires_at"]
    login = Client(app, login=None).post(
        "/auth/login", {"login": "admin", "password": ADMIN_PASSWORD}
    )
    raw = dict(login.headers)["set-cookie"]
    assert "HttpOnly" in raw and "SameSite=strict" in raw and "Max-Age=43200" in raw
    token = client.cookie
    assert client.post("/auth/logout").status == 204
    assert client.cookie is None
    client.cookie = token  # тот же токен после выхода не действует
    assert client.get("/auth/me").json()["error"]["code"] == "session_invalid"


def test_bearer_token_works_like_the_cookie(app):
    client = Client(app)
    bearer = Client(app, login=None)
    reply = bearer.request("GET", "/auth/me", headers={"authorization": f"Bearer {client.cookie}"})
    assert reply.status == 200 and reply.json()["user"]["login"] == "admin"


def test_wrong_password_locks_the_account_and_is_audited(app, admin):
    stranger = Client(app, login=None)
    for _ in range(4):
        reply = stranger.post("/auth/login", {"login": "admin", "password": "не-тот-пароль"})
        assert reply.status == 401 and reply.json()["error"]["code"] == "login_failed"
    reply = stranger.post("/auth/login", {"login": "admin", "password": "не-тот-пароль"})
    assert reply.status == 429 and reply.json()["error"]["code"] == "login_locked"
    reply = stranger.post("/auth/login", {"login": "admin", "password": ADMIN_PASSWORD})
    assert reply.status == 429  # блокировка держится и для верного пароля
    events = admin.get("/audit").json()["items"]
    failed = [event for event in events if event["action"] == "auth.login_failed"]
    assert len(failed) == 6 and failed[0]["details"]["reason"] == "locked"


def test_one_address_cannot_guess_logins_endlessly(app):
    stranger = Client(app, login=None)
    for number in range(20):
        stranger.post("/auth/login", {"login": f"user{number}", "password": "пароль-наугад"})
    reply = stranger.post("/auth/login", {"login": "user99", "password": "пароль-наугад"})
    assert reply.status == 429 and reply.json()["error"]["code"] == "login_throttled"


# AT-20…AT-22 ------------------------------------------------------------------


def test_at20_rights_change_reaches_an_open_session_on_the_next_request(app, admin, plant):
    dataset = plant["datasets"][0]["id"]
    role = _role(admin, "Обходчик", [_view(dataset)])
    user, client = _user(app, admin, "obhodchik", role["id"])
    body = {"values": {"name": "Новый насос"}}
    denied = client.post(f"/datasets/{dataset}/records", body)
    assert denied.status == 403
    assert denied.json()["error"]["details"] == {"permission": "dataset.create"}
    granted = [*role["permissions"], {"scope": "dataset", "target": dataset, "action": "create"}]
    admin.put(f"/roles/{role['id']}/permissions", {"permissions": granted})
    created = client.post(f"/datasets/{dataset}/records", body)
    assert created.status == 201  # та же сессия, без нового входа (ROLE-09)

    renamed = admin.patch(f"/roles/{role['id']}", {"name": "Обходчик котельного цеха"}).json()
    assert renamed["id"] == role["id"] and len(renamed["permissions"]) == 2  # ROLE-02
    assert client.get("/auth/me").json()["role"]["name"] == "Обходчик котельного цеха"
    history = client.get(f"/records/{created.json()['id']}/history").json()["items"]
    assert history[0]["actor_id"] == user["id"]  # в журнале учётная запись (ROLE-04)
    events = admin.get("/audit", entity_id=role["id"]).json()["items"]
    assert [event["action"] for event in events] == [
        "role.update",
        "role.permissions",
        "role.permissions",
        "role.create",
    ]


def test_at21_reset_closes_sessions_and_forces_a_new_password(app, admin):
    roles = {role["name"]: role["id"] for role in admin.get("/roles").json()["items"]}
    user, client = _user(app, admin, "petrov", roles["Инженер"])
    reset = admin.post(f"/users/{user['id']}/reset-password")
    temporary = reset.json()["temporary_password"]
    assert reset.status == 200 and len(temporary) == 16
    assert client.get("/sites").json()["error"]["code"] == "session_invalid"
    assert (
        Client(app, login=None)
        .post("/auth/login", {"login": "petrov", "password": NEW_PASSWORD})
        .status
        == 401
    )
    fresh = Client(app, login="petrov", password=temporary)
    assert fresh.get("/auth/me").json()["must_change_password"] is True
    blocked = fresh.get("/sites")
    assert blocked.status == 403 and blocked.json()["error"]["code"] == "password_change_required"
    changed = fresh.post(
        "/auth/password", {"current_password": temporary, "new_password": "ещё-один-пароль"}
    )
    assert changed.status == 200 and changed.json()["must_change_password"] is False
    assert fresh.get("/sites").status == 200


def test_at22_last_admin_cannot_be_blocked(app, admin):
    me = admin.get("/auth/me").json()
    reply = admin.post(f"/users/{me['user']['id']}/block")
    assert reply.status == 409
    assert reply.json()["error"]["code"] == "last_admin"
    assert "последний активный администратор" in reply.json()["error"]["message"]
    roles = {role["name"]: role for role in admin.get("/roles").json()["items"]}
    stripped = admin.put(f"/roles/{roles['Администратор']['id']}/permissions", {"permissions": []})
    assert stripped.status == 409 and stripped.json()["error"]["code"] == "last_admin"
    assert admin.get("/users").status == 200  # право не снято


# Пользователи --------------------------------------------------------------------


def test_temporary_password_is_shown_once_and_not_stored(app, admin):
    roles = {role["name"]: role["id"] for role in admin.get("/roles").json()["items"]}
    body = {"login": "sidorov", "name": "Сидоров", "role_id": roles["Наблюдатель"]}
    first = admin.post("/users", body, key="create-sidorov")
    again = admin.post("/users", body, key="create-sidorov")
    temporary = first.json()["temporary_password"]
    assert again.status == 201 and "temporary_password" not in again.json()
    assert again.json()["id"] == first.json()["id"]
    listed = admin.get("/users").json()["items"]
    assert not {"password_hash", "temporary_password"} & {key for user in listed for key in user}
    app.state.db.connect().execute("PRAGMA wal_checkpoint(FULL)")
    with open(app.state.db.path, "rb") as handle:
        assert temporary.encode() not in handle.read()  # ни в ответе по ключу, ни в журнале


def test_idempotency_key_belongs_to_its_user(app, admin, plant):
    dataset = plant["datasets"][0]["id"]
    roles = {role["name"]: role["id"] for role in admin.get("/roles").json()["items"]}
    _, engineer = _user(app, admin, "ivanov", roles["Инженер"])
    body = {"values": {"name": "Один и тот же"}}
    mine = admin.post(f"/datasets/{dataset}/records", body, key="общий-ключ")
    theirs = engineer.post(f"/datasets/{dataset}/records", body, key="общий-ключ")
    assert mine.status == theirs.status == 201
    assert mine.json()["id"] != theirs.json()["id"]


# Права на реестры и поля ------------------------------------------------------


def test_dataset_rights_hide_other_datasets(app, admin, plant):
    visible, secret = plant["datasets"]
    role = _role(admin, "Только насосы", [_view(visible["id"])])
    _, client = _user(app, admin, "nasos", role["id"])
    listed = [item["id"] for item in client.get("/datasets").json()["items"]]
    assert listed == [visible["id"]]
    assert client.get(f"/datasets/{secret['id']}").status == 403
    assert client.post(f"/datasets/{secret['id']}/records/query", {}).status == 403
    assert client.post("/types", {"name": "Свой тип"}).status == 403  # нет раздела структуры


def test_hidden_field_is_invisible_everywhere(app, admin, plant):
    dataset = plant["datasets"][0]["id"]
    price, record = plant["fields"]["price"], plant["record"]
    role = _role(
        admin,
        "Без цен",
        [_view(), {"scope": "field", "target": price["id"], "action": "hidden"}],
    )
    _, client = _user(app, admin, "bezcen", role["id"])
    page = client.post(f"/datasets/{dataset}/records/query", {}).json()
    assert page["items"][0]["doc"] == {"name": "Насос ПЭН", "power": 500}
    assert "price" not in client.get(f"/records/{record['id']}").json()["record"]["doc"]
    assert "price" not in client.get(f"/records/{record['id']}/versions/1").json()["doc"]
    fields = [item["key"] for item in client.get(f"/types/{plant['type']['id']}").json()["fields"]]
    assert fields == ["name", "power"]
    by_price = {"where": [{"field": "price", "op": "eq", "value": 125000}]}
    assert client.post(f"/datasets/{dataset}/records/query", by_price).status == 400
    found = client.post(f"/datasets/{dataset}/records/query", {"search": "125000"}).json()
    assert found["items"] == []  # скрытое значение не находит запись
    found = client.post(f"/datasets/{dataset}/records/query", {"search": "пэн"}).json()
    assert len(found["items"]) == 1  # видимое — находит
    admin_page = admin.post(f"/datasets/{dataset}/records/query", {"search": "125000"}).json()
    assert len(admin_page["items"]) == 1


def test_readonly_field_cannot_be_changed_even_by_rollback(app, admin, plant):
    power, record = plant["fields"]["power"], plant["record"]
    rights = [
        _view(),
        {"scope": "dataset", "target": "*", "action": "edit"},
        {"scope": "field", "target": power["id"], "action": "readonly"},
    ]
    role = _role(admin, "Без мощности", rights)
    _, client = _user(app, admin, "operator", role["id"])
    denied = client.patch(
        f"/records/{record['id']}", {"expected_version": 1, "values": {"power": 600}}
    )
    assert denied.status == 403
    assert denied.json()["error"]["code"] == "field_forbidden"
    assert denied.json()["error"]["field"] == "power"
    allowed = client.patch(
        f"/records/{record['id']}", {"expected_version": 1, "values": {"name": "ПЭН-2"}}
    )
    assert allowed.status == 200
    admin.patch(f"/records/{record['id']}", {"expected_version": 2, "values": {"power": 700}})
    rollback = client.post(
        f"/records/{record['id']}/rollback", {"expected_version": 3, "to_version": 1}
    )
    assert rollback.status == 403 and rollback.json()["error"]["field"] == "power"
    assert client.get(f"/records/{record['id']}").json()["record"]["doc"]["power"] == 700


# Командная строка -----------------------------------------------------------------


def test_cli_creates_the_first_admin_and_resets_a_forgotten_password(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cli.sqlite3"
    monkeypatch.setattr(sys, "stdin", io.StringIO("короткий\n"))
    assert (
        admin_cli.main(
            [
                "--db",
                str(path),
                "create-admin",
                "--login",
                "boss",
                "--name",
                "Босс",
                "--password-stdin",
            ]
        )
        == 1
    )
    assert "короче 12" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.StringIO("пароль-установщика\n"))
    assert (
        admin_cli.main(
            [
                "--db",
                str(path),
                "create-admin",
                "--login",
                "boss",
                "--name",
                "Босс",
                "--password-stdin",
            ]
        )
        == 0
    )
    app = create_app(path)
    assert Client(app, login="boss", password="пароль-установщика").get("/users").status == 200
    assert admin_cli.main(["--db", str(path), "reset-password", "--login", "boss"]) == 0
    temporary = re.search(r": (\S+)\n", capsys.readouterr().out).group(1)
    fresh = Client(app, login="boss", password=temporary)
    assert fresh.get("/users").json()["error"]["code"] == "password_change_required"


def test_admin_without_data_rights_still_sees_every_dataset_and_field(app, admin, plant):
    """Экран прав: администратор выдаёт права на реестры и поля, которые сам не читает."""
    price = plant["fields"]["price"]
    secret = plant["datasets"][1]["id"]
    rights = [
        {"scope": "section", "target": "admin", "action": "use"},
        _view(plant["datasets"][0]["id"]),
        {"scope": "field", "target": price["id"], "action": "hidden"},
    ]
    role = _role(admin, "Только администрирование", rights)
    _, client = _user(app, admin, "kadry", role["id"])
    listed = {item["id"] for item in client.get("/datasets").json()["items"]}
    assert secret in listed
    fields = [item["key"] for item in client.get(f"/types/{plant['type']['id']}").json()["fields"]]
    assert "price" in fields
    assert client.post(f"/datasets/{secret}/records/query", {}).status == 403  # записи закрыты
