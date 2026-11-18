"""Сервер API версии 1: маршруты, формат ошибок, версии, идемпотентность (API-01…API-11)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from server import api, openapi
from server.app import create_app
from server.routes import ENDPOINTS

from .asgi import Client

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(tmp_path):
    return Client(create_app(tmp_path / "localai.sqlite3"))


@pytest.fixture
def plant(client):
    """Площадка, справочник, тип с полями и реестр — через API."""
    site = client.post("/sites", {"name": "ТЭЦ-1"}).json()
    makers = client.post("/dictionaries", {"name": "Изготовитель"}).json()
    client.post(
        f"/dictionaries/{makers['id']}/values", {"value": "Siemens", "synonyms": ["Сименс"]}
    )
    kind = client.post("/types", {"name": "Оборудование"}).json()
    fields = {
        "kks": client.post(
            f"/types/{kind['id']}/fields",
            {"name": "ККС", "kind": "kks", "required": True, "key": "kks"},
        ).json(),
        "name": client.post(
            f"/types/{kind['id']}/fields",
            {"name": "Наименование", "kind": "text", "key": "name", "sortable": True},
        ).json(),
        "power": client.post(
            f"/types/{kind['id']}/fields",
            {"name": "Мощность", "kind": "number", "key": "power", "filterable": True},
        ).json(),
        "maker": client.post(
            f"/types/{kind['id']}/fields",
            {
                "name": "Изготовитель",
                "kind": "choice",
                "key": "maker",
                "dictionary_id": makers["id"],
                "input_mode": "suggest",
            },
        ).json(),
    }
    dataset = client.post(
        "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": "Эксплуатация"}
    ).json()
    return {"site": site, "type": kind, "fields": fields, "dataset": dataset, "makers": makers}


# Правила маршрутов ---------------------------------------------------------------


def test_each_method_and_path_has_one_handler():
    seen = [(endpoint.method, endpoint.path) for endpoint in ENDPOINTS]
    assert len(seen) == len(set(seen))  # API-01


def test_every_route_declares_a_known_permission():
    for endpoint in ENDPOINTS:
        assert endpoint.permission in api.PERMISSIONS, endpoint.path  # API-02
    public = [endpoint.path for endpoint in ENDPOINTS if endpoint.permission == "public"]
    assert public == ["/auth/login", "/system/health"]


def test_openapi_file_matches_code():
    current = (ROOT / "docs" / "contract" / "openapi.json").read_text(encoding="utf-8")
    assert current == openapi.render(), "выполните python -m server.openapi"  # API-11


def test_contract_document_lists_exactly_the_implemented_routes():
    text = (ROOT / "docs" / "contract" / "api-v1.md").read_text(encoding="utf-8")
    documented = set()
    for line in text.splitlines():
        match = re.match(r"^\| `(GET|POST|PATCH|PUT|DELETE) ([^`?]+)[^`]*`((?:, `/[^`]+`)*)", line)
        if not match:
            continue
        method, path, siblings = match.groups()
        documented.add((method, path))
        for sibling in re.findall(r"`(/[^`]+)`", siblings):
            documented.add((method, path.rsplit("/", 1)[0] + sibling))
    implemented = {(endpoint.method, endpoint.path) for endpoint in ENDPOINTS}
    assert documented == implemented


# Сквозной сценарий -----------------------------------------------------------------


def test_record_lifecycle_through_the_api(client, plant):
    dataset = plant["dataset"]["id"]
    created = client.post(
        f"/datasets/{dataset}/records",
        {"values": {"kks": "10LAC10AP001", "name": "Насос", "power": "12,5", "maker": "сименс"}},
    )
    assert created.status == 201
    record = created.json()
    assert record["version"] == 1 and record["doc"]["power"] == 12.5

    page = client.post(
        f"/datasets/{dataset}/records/query", {"search": "насос", "limit": 10}
    ).json()
    assert [item["id"] for item in page["items"]] == [record["id"]]
    assert page["refs"]["choices"][record["doc"]["maker"]] == "Siemens"
    assert page["refs"]["kks"][record["doc"]["kks"]] == "10LAC10AP001"
    count = client.post(
        f"/datasets/{dataset}/records/count",
        {"where": [{"field": "power", "op": "gt", "value": 10}]},
    ).json()
    assert count == {"count": 1}

    changed = client.patch(
        f"/records/{record['id']}", {"expected_version": 1, "values": {"name": "Насос ПЭН"}}
    )
    assert changed.status == 200 and changed.json()["version"] == 2
    stale = client.patch(
        f"/records/{record['id']}", {"expected_version": 1, "values": {"name": "Другое"}}
    )
    assert stale.status == 409
    assert stale.json()["error"]["code"] == "version_conflict"
    assert stale.json()["error"]["details"] == {"current_version": 2}

    history = client.get(f"/records/{record['id']}/history").json()["items"]
    assert [item["version"] for item in history] == [2, 1]
    assert history[0]["actor_label"] == "Тестовый администратор"  # учётная запись (ROLE-04)
    assert history[0]["actor_id"] is not None
    first = client.get(f"/records/{record['id']}/versions/1").json()
    assert first["doc"]["name"] == "Насос" and first["refs"]["kks"]
    rolled = client.post(
        f"/records/{record['id']}/rollback", {"expected_version": 2, "to_version": 1}
    )
    assert rolled.json()["version"] == 3 and rolled.json()["doc"]["name"] == "Насос"

    archived = client.post(f"/records/{record['id']}/archive", {"expected_version": 3}).json()
    assert archived["archived_at"] and archived["version"] == 4
    hidden = client.post(f"/datasets/{dataset}/records/query", {}).json()["items"]
    assert hidden == []
    restored = client.post(f"/records/{record['id']}/restore", {"expected_version": 4}).json()
    assert restored["archived_at"] is None
    one = client.get(f"/records/{record['id']}").json()
    assert one["record"]["version"] == 5 and one["refs"]["choices"]


def test_kks_card_rename_and_aliases(client, plant):
    dataset, site = plant["dataset"]["id"], plant["site"]["id"]
    client.post(f"/datasets/{dataset}/records", {"values": {"kks": "10LAC10AP001"}})
    found = client.get(f"/sites/{site}/kks/search", q="10lac").json()["items"]
    assert [item["code"] for item in found] == ["10LAC10AP001"]
    object_id = found[0]["id"]
    renamed = client.patch(
        f"/kks/{object_id}", {"code": "10LAC10AP002", "name": "Насос ПЭН-1"}
    ).json()
    assert renamed["code"] == "10LAC10AP002" and renamed["name"] == "Насос ПЭН-1"
    by_old = client.get(f"/sites/{site}/kks/resolve", code="10lac10ap001").json()
    assert by_old["id"] == object_id and by_old["via"] == "alias"
    alias = client.post(f"/kks/{object_id}/aliases", {"alias": "ПЭН-1"})
    assert alias.status == 201
    card = client.get(f"/kks/{object_id}").json()
    assert sorted(item["alias"] for item in card["aliases"]) == ["10LAC10AP001", "ПЭН-1"]
    assert card["usage"] == [{"dataset_id": dataset, "role": "object", "records": 1}]
    assert client.delete(f"/kks-aliases/{alias.json()['id']}").status == 204
    missing = client.get(f"/sites/{site}/kks/resolve", code="ПЭН-1")
    assert missing.status == 404 and missing.json()["error"]["code"] == "kks_not_found"
    linked = client.post(f"/datasets/{dataset}/records/query", {"kks": object_id}).json()
    assert len(linked["items"]) == 1


def test_structure_routes(client, plant):
    kind = plant["type"]["id"]
    field = plant["fields"]["name"]["id"]
    renamed = client.patch(f"/fields/{field}", {"name": "Название"}).json()
    assert renamed["name"] == "Название" and renamed["key"] == "name"
    assert client.post(f"/fields/{field}/archive").json()["archived_at"]
    assert client.post(f"/fields/{field}/restore").json()["archived_at"] is None
    full = client.get(f"/types/{kind}").json()
    assert [item["key"] for item in full["fields"]] == ["kks", "name", "power", "maker"]
    values = client.get(f"/dictionaries/{plant['makers']['id']}/values").json()["items"]
    assert values[0]["synonyms"] == ["Сименс"]
    listed = client.get("/datasets", site_id=plant["site"]["id"]).json()["items"]
    assert [item["name"] for item in listed] == ["Эксплуатация"]
    in_use = client.post(f"/types/{kind}/archive")
    assert in_use.status == 400 and in_use.json()["error"]["code"] == "type_in_use"
    events = client.get("/audit", limit=3).json()["items"]
    assert len(events) == 3 and events[0]["seq"] > events[-1]["seq"]
    health = client.get("/system/health").json()
    assert health["status"] == "ok" and health["schema_version"] >= 2 and health["database_id"]


# Ошибки -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "code", "field"),
    [
        (
            "GET",
            "/records/00000000-0000-4000-8000-000000000000",
            None,
            404,
            "record_not_found",
            None,
        ),
        ("GET", "/no-such-route", None, 404, "route_not_found", None),
        ("DELETE", "/sites", None, 405, "method_not_allowed", None),
        ("POST", "/sites", {"name": "x", "лишнее": 1}, 400, "request_invalid", "лишнее"),
        ("POST", "/sites", {}, 400, "request_invalid", "name"),
        ("POST", "/sites", {"name": "   "}, 400, "name_empty", "name"),
    ],
)
def test_errors_have_one_format(client, method, path, body, status, code, field):
    reply = client.request(method, path, body)
    assert reply.status == status
    error = reply.json()["error"]
    assert error["code"] == code and error["message"]
    assert error.get("field") == field
    assert not re.search(r"[A-Za-z]{4,} [a-z]{4,}", error["message"]) or code == "route_not_found"


def test_bad_json_and_bad_query(client, plant):
    broken = client.post("/sites", raw=b"{name:")
    assert broken.status == 400 and broken.json()["error"]["code"] == "request_json"
    dataset = plant["dataset"]["id"]
    bad = client.post(
        f"/datasets/{dataset}/records/query",
        {"where": [{"field": "power", "op": "похоже", "value": 1}]},
    )
    assert bad.status == 400 and bad.json()["error"] == {
        "code": "query_invalid",
        "message": "Неизвестная операция: похоже",
        "field": "power",
    }
    value = client.post(f"/datasets/{dataset}/records", {"values": {"kks": "A1", "power": "много"}})
    assert value.status == 400 and value.json()["error"]["field"] == "power"


def test_idempotency_key_repeats_the_first_answer(client, plant):
    dataset = plant["dataset"]["id"]
    body = {"values": {"kks": "A1"}}
    first = client.post(f"/datasets/{dataset}/records", body, key="k-1").json()
    again = client.post(f"/datasets/{dataset}/records", body, key="k-1").json()
    assert first == again
    total = client.post(f"/datasets/{dataset}/records/count", {}).json()["count"]
    assert total == 1
    other = client.post(f"/datasets/{dataset}/records", {"values": {"kks": "A2"}}, key="k-1")
    assert other.status == 409 and other.json()["error"]["code"] == "idempotency_mismatch"


def test_unexpected_failure_is_reported_without_details(tmp_path, monkeypatch):
    def boom(call):
        """Сломанный обработчик."""
        raise RuntimeError("секрет внутреннего устройства")

    monkeypatch.setattr("server.routes.system.ENDPOINTS", [])
    endpoint = api.Endpoint("GET", "/system/health", boom, "public")
    monkeypatch.setattr("server.app.ENDPOINTS", [endpoint])
    reply = Client(create_app(tmp_path / "x.sqlite3"), login=None).get("/system/health")
    assert reply.status == 500
    assert reply.json()["error"]["code"] == "internal"
    assert "секрет" not in reply.body.decode()


def test_failed_write_leaves_no_trace(client, plant):
    before = len(client.get("/audit", limit=200).json()["items"])
    failed = client.post(
        f"/datasets/{plant['dataset']['id']}/records", {"values": {"name": "без ККС"}}
    )
    assert failed.status == 400 and failed.json()["error"]["code"] == "value_required"
    assert len(client.get("/audit", limit=200).json()["items"]) == before
