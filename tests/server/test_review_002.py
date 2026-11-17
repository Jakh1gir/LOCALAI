"""Находки перепроверки R-002: номера — из docs/requests/002-claude-recheck.md."""

from __future__ import annotations

import base64
import hashlib
import json
import os

import pytest

from engine import Actor, EngineError, open_database, query, records, structure
from server.app import create_app
from server.localdb_import import run
from server.localdb_import.__main__ import main

from .asgi import Client
from .localdb_fixture import LocalDb

ADMIN = Actor(None, "Тест")


def _source(folder):
    folder.mkdir(exist_ok=True)
    base = LocalDb(folder / "local_db.sqlite3")
    base.field("qty", "Количество", "integer")
    base.record({"qty": 1})
    base.close()
    return folder / "local_db.sqlite3"


def test_01_hard_link_to_the_source_is_not_a_report_path(tmp_path):
    source = _source(tmp_path / "src")
    report = tmp_path / "report.json"
    os.link(source, report)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(SystemExit):
        main([str(source), "--report", str(report)])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    ("literal", "status", "stored"),
    [
        (b"9007199254740993.0", 201, 9007199254740993),
        (b"0.100000000000000005", 400, None),
        (b"12.50", 201, 12.5),
        (b"1E+3", 201, 1000),
    ],
)
def test_02_json_number_keeps_its_precision(tmp_path, literal, status, stored):
    client = Client(create_app(tmp_path / "api.sqlite3"))
    site = client.post("/sites", {"name": "ТЭЦ"}).json()
    kind = client.post("/types", {"name": "Тип"}).json()
    client.post(f"/types/{kind['id']}/fields", {"name": "Число", "kind": "number", "key": "num"})
    dataset = client.post(
        "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": "Р"}
    ).json()
    reply = client.post(
        f"/datasets/{dataset['id']}/records", raw=b'{"values":{"num":' + literal + b"}}"
    )
    assert reply.status == status
    if stored is None:
        assert reply.json()["error"] == {
            "code": "value_precision",
            "message": "Число не сохраняется точно: больше 15 значащих цифр",
            "field": "num",
        }
    else:
        assert reply.json()["doc"]["num"] == stored


@pytest.mark.parametrize("name", ["обычная.sqlite3", "подготовленная #01.sqlite3"])
def test_03_dry_run_reads_the_named_target_and_leaves_its_folder_alone(tmp_path, name):
    source = _source(tmp_path / "src")
    target = tmp_path / "targets" / name
    target.parent.mkdir()
    db = open_database(str(target))
    assert run(db, source, apply=True)["mode"] == "applied"
    db.close()
    before = sorted(item.name for item in target.parent.iterdir())
    report = tmp_path / "report.json"
    assert main([str(source), "--target", str(target), "--report", str(report)]) == 0
    datasets = json.loads(report.read_text(encoding="utf-8"))["datasets"]
    assert sum(item["records_unchanged"] for item in datasets) == 1  # прочитана именно эта база
    assert sorted(item.name for item in target.parent.iterdir()) == before


@pytest.fixture
def ranked(tmp_path):
    db = open_database(str(tmp_path / "rank.sqlite3"))
    with db.write(ADMIN) as tx:
        site = structure.create_site(tx, "ТЭЦ")
        kind = structure.create_type(tx, "Тип")
        structure.add_field(tx, kind["id"], "Ранг", "integer", key="rank", sortable=True)
        same = structure.create_dataset(tx, site["id"], kind["id"], "Одинаковые")
        for _ in range(2):
            records.create_many(tx, same["id"], [{"rank": 1} for _ in range(3000)])
    yield db, same["id"]
    db.close()


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_04_cursor_inside_a_group_of_equal_values_starts_at_its_place(ranked, direction):
    db, dataset = ranked
    conn = db.connect()
    request = {"sort": {"field": "rank", "dir": direction}, "limit": 200}
    cursor = None
    for _ in range(25):
        cursor = query.list_records(conn, dataset, {**request, "cursor": cursor})["next_cursor"]
    steps = {"count": 0}

    def tick() -> int:
        steps["count"] += 1
        return 0

    def cost(body):
        steps["count"] = 0
        conn.set_progress_handler(tick, 100)
        page = query.list_records(conn, dataset, body)
        conn.set_progress_handler(None, 0)
        return steps["count"], page

    first, _ = cost({**request, "limit": 5})
    deep, page = cost({**request, "limit": 5, "cursor": cursor})
    assert len(page["items"]) == 5
    assert deep <= first * 3 + 5  # было 770 шагов на 9 600-й строке


def _forge(cursor, index, value):
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    parts = json.loads(raw)
    parts[index] = value
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    ("index", "value"), [(1, {}), (1, [1]), (1, True), (2, 2**63), (2, "1"), (2, 0)]
)
def test_05_forged_cursor_is_a_clear_400(ranked, index, value):
    db, dataset = ranked
    request = {"sort": {"field": "rank", "dir": "desc"}, "limit": 1}
    with db.read() as conn:
        cursor = query.list_records(conn, dataset, request)["next_cursor"]
        with pytest.raises(EngineError) as caught:
            query.list_records(conn, dataset, {**request, "cursor": _forge(cursor, index, value)})
    assert caught.value.code == "query_invalid" and caught.value.field == "cursor"


def test_06_body_limit_applies_to_routes_without_a_body(tmp_path):
    client = Client(create_app(tmp_path / "api.sqlite3"))
    kind = client.post("/types", {"name": "Тип"}).json()
    field = client.post(f"/types/{kind['id']}/fields", {"name": "Поле", "kind": "text"}).json()
    huge = b'{"name":"' + b"a" * (10 * 1024 * 1024) + b'"}'
    reply = client.post(f"/fields/{field['id']}/archive", raw=huge)
    assert reply.status == 413 and reply.json()["error"]["code"] == "request_too_large"
    assert client.get(f"/types/{kind['id']}").json()["fields"][0]["archived_at"] is None


@pytest.mark.parametrize("archived", [False, "any"])
def test_04_cursor_walks_a_group_of_equal_values_without_gaps(ranked, archived):
    db, dataset = ranked
    request = {"sort": {"field": "rank", "dir": "desc"}, "limit": 200, "archived": archived}
    seen, cursor = [], None
    with db.read() as conn:
        while True:
            page = query.list_records(conn, dataset, {**request, "cursor": cursor})
            seen += [item["id"] for item in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
    assert len(seen) == len(set(seen)) == 6000
