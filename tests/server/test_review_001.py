"""Находки R-001 в сервере и импортёре: номера — из docs/requests/001-claude-review.md."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3

import pytest

from engine import SYSTEM, EngineError, kks, open_database, query, records, structure
from server import openapi
from server.app import create_app
from server.localdb_import import Decisions, run
from server.localdb_import.__main__ import main

from .asgi import Client
from .localdb_fixture import LocalDb


@pytest.fixture
def case(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    db = open_database(str(tmp_path / "target.sqlite3"))
    yield LocalDb(folder / "local_db.sqlite3"), db
    db.close()


def _reopen(path):
    """Продолжить запись в уже закрытую тестовую базу Local_DB."""
    base = LocalDb.__new__(LocalDb)
    base.path = path
    base.conn = sqlite3.connect(path)
    base.fields = {
        row[0]: {"id": row[1], "type": row[2], "dictionary_id": row[3]}
        for row in base.conn.execute(
            "SELECT code, id, data_type, dictionary_id FROM field_definitions"
        )
    }
    base.dictionaries = {
        c: m["dictionary_id"] for c, m in base.fields.items() if m["dictionary_id"]
    }
    base.records = dict(base.conn.execute("SELECT uuid, id FROM records"))
    base.table_id = 1
    return base


# Импорт ------------------------------------------------------------------------


def test_01_failed_extend_leaves_the_record_untouched(case):
    src, db = case
    src.field("kks", "ККС", "text")
    src.field("aux", "Второй ККС", "text")
    rid = src.record({"kks": "10AAA01", "aux": "10BBB01"})
    path = src.path
    src.close()
    decisions = Decisions(kks_fields=("kks", "aux"))
    assert run(db, path, decisions, apply=True)["mode"] == "applied"
    # тот же код в двух полях теперь допустим
    base = _reopen(path)
    base.update(rid, {"kks": "10CCC01", "aux": "10CCC01"})
    base.close()
    report = run(db, path, decisions, apply=True)
    assert report["mode"] == "applied" and all(c["ok"] for c in report["reconciliation"])
    with db.read() as conn:
        row = records.get(conn, rid)
        assert row["version"] == 2 and row["doc"]["kks"] == row["doc"]["aux"]
        assert not query.list_records(conn, row["dataset_id"], {"search": "10BBB01"})["items"]
    # история, которую нельзя продолжить, не оставляет следов
    with db.write(SYSTEM) as tx:  # документ расходится со своей историей
        site = structure.list_sites(tx.conn)[0]["id"]
        stray = kks.resolve(tx.conn, site, "10AAA01")["id"]
        tx.conn.execute(
            "UPDATE records SET doc = json_set(doc, '$.kks', ?) WHERE id = ?", (stray, rid)
        )
    base = _reopen(path)
    base.update(rid, {"kks": "10DDD01", "aux": "10EEE01"})
    base.close()
    report = run(db, path, decisions, apply=True)
    assert report["mode"] == "applied"
    assert {item["code"] for item in report["issues"]} >= {"load_history_gap"}
    with db.read() as conn:
        assert records.get(conn, rid)["version"] == 2
        site = structure.list_sites(conn)[0]["id"]
        assert kks.resolve(conn, site, "10DDD01") is None  # каталог не пополнился


def test_02_report_cannot_overwrite_the_source(case):
    src, _db = case
    src.field("kks", "ККС", "text")
    src.record({"kks": "A1"})
    path = src.path
    src.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(SystemExit):
        main([str(path.parent), "--report", str(path)])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_03_large_number_is_imported_exactly(case):
    src, db = case
    src.field("num", "Число", "decimal")
    rid = src.record({"num": "9007199254740993"})
    path = src.path
    src.close()
    assert run(db, path, apply=True)["mode"] == "applied"
    with db.read() as conn:
        assert records.get(conn, rid)["doc"]["num"] == 9007199254740993


def test_05_reconciliation_counts_kept_history_rows(case):
    src, db = case
    src.field("qty", "Количество", "integer")
    rid = src.record(
        {"qty": 3}, history=[("CREATE_RECORD", {"qty": 1}), ("UPDATE_RECORD", {"qty": 2})]
    )
    src.conn.execute("DELETE FROM record_versions WHERE version < 3")
    path = src.path
    src.close()
    report = run(db, path, apply=True)
    check = report["reconciliation"][0]
    assert check["ok"] and check["history_rows_source"] == check["history_rows_target"] == 1
    with db.read() as conn, pytest.raises(EngineError) as caught:
        records.doc_at(conn, rid, 1)
    assert caught.value.code == "version_missing"


def test_06_renamed_kks_does_not_block_repeat_import(case):
    src, db = case
    src.field("kks", "ККС", "text")
    rid = src.record({"kks": "01AAA"})
    path = src.path
    src.close()
    run(db, path, apply=True)
    with db.write(SYSTEM) as tx:
        kks.rename(tx, records.get(tx.conn, rid)["doc"]["kks"], "02AAA")
    base = _reopen(path)
    base.record({"kks": "03AAA"})
    base.close()
    report = run(db, path, apply=True)
    assert report["mode"] == "applied", report["issues"]


def test_07_skipped_record_adds_nothing_to_the_catalog(case):
    src, db = case
    src.field("kks", "ККС", "text")
    src.field("label", "Название", "text")
    rid = src.record({"kks": "01AAA", "label": "Исходное"})
    path = src.path
    src.close()
    run(db, path, apply=True)
    with db.write(SYSTEM) as tx:
        records.update(tx, rid, {"label": "Локальная правка"}, 1)
    base = _reopen(path)
    base.update(rid, {"kks": "99ZZZ", "label": "Правка источника"})
    base.record({"label": "Другая новая запись"})
    base.close()
    report = run(db, path, apply=True)
    with db.read() as conn:
        site = structure.list_sites(conn)[0]["id"]
        assert kks.resolve(conn, site, "99ZZZ") is None
        assert records.get(conn, rid)["doc"]["label"] == "Локальная правка"
    check = report["reconciliation"][0]
    assert check["ok"] and check["records_skipped"] == 1


def test_08_wal_without_shm_is_read_from_a_copy(case, tmp_path):
    src, db = case
    src.field("kks", "ККС", "text")
    src.conn.commit()
    src.conn.execute("PRAGMA wal_autocheckpoint = 0")
    src.record({"kks": "A1"})
    src.conn.commit()
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    for suffix in ("", "-wal"):
        shutil.copy2(f"{src.path}{suffix}", crashed / f"local_db.sqlite3{suffix}")
    src.close()
    report = run(db, crashed)
    assert sum(item["records_new"] for item in report["datasets"]) == 1
    assert sorted(item.name for item in crashed.iterdir()) == [
        "local_db.sqlite3",
        "local_db.sqlite3-wal",
    ]


def test_09_old_schema_is_refused_before_reading_structure(tmp_path):
    base = LocalDb(tmp_path / "local_db.sqlite3", schema=4)
    base.conn.execute("ALTER TABLE dictionary_values DROP COLUMN merged_into_id")
    base.close()
    db = open_database(str(tmp_path / "target.sqlite3"))
    with pytest.raises(EngineError) as caught:
        run(db, tmp_path)
    db.close()
    assert caught.value.code == "localdb_schema_old"


def test_10_dry_run_never_writes_to_target(case, tmp_path, capsys):
    src, db = case
    src.field("kks", "ККС", "text")
    src.record({"kks": "A1"})
    path = src.path
    src.close()
    missing = tmp_path / "новый.sqlite3"
    assert main([str(path.parent), "--target", str(missing)]) == 0
    assert not missing.exists()
    existing = tmp_path / "target.sqlite3"
    db.close()
    before = hashlib.sha256(existing.read_bytes()).hexdigest()
    assert main([str(path.parent), "--target", str(existing)]) == 0
    assert hashlib.sha256(existing.read_bytes()).hexdigest() == before
    assert "не изменён" in capsys.readouterr().out


# Сервер ------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    client = Client(create_app(tmp_path / "api.sqlite3"))
    site = client.post("/sites", {"name": "ТЭЦ-1"}).json()
    kind = client.post("/types", {"name": "Тип"}).json()
    client.post(f"/types/{kind['id']}/fields", {"name": "Ранг", "kind": "integer", "key": "rank"})
    client.post(f"/types/{kind['id']}/fields", {"name": "Срок", "kind": "date", "key": "due"})
    dataset = client.post(
        "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": "Реестр"}
    ).json()
    return client, dataset["id"], kind["id"]


def test_11_mutating_request_needs_idempotency_key(api):
    client, dataset, _ = api
    reply = client.post(f"/datasets/{dataset}/records", {"values": {}}, key="")
    assert reply.status == 400
    assert reply.json()["error"]["code"] == "idempotency_key_required"
    spec = openapi.build()
    header = next(
        p
        for p in spec["paths"]["/api/v1/datasets/{id}/records"]["post"]["parameters"]
        if p["name"] == "Idempotency-Key"
    )
    assert header["required"] is True
    read = client.post(f"/datasets/{dataset}/records/query", {}, key="")
    assert read.status == 200  # чтение ключа не требует


def test_12_huge_integer_is_a_400_not_a_500(api):
    client, dataset, _ = api
    created = client.post(f"/datasets/{dataset}/records", {"values": {"rank": 2**63}})
    assert created.status == 400 and created.json()["error"]["code"] == "value_out_of_range"
    filtered = client.post(
        f"/datasets/{dataset}/records/query",
        {"where": [{"field": "rank", "op": "eq", "value": 2**63}]},
    )
    assert filtered.status == 400


@pytest.mark.parametrize(
    "body",
    [
        {"where": [{"field": [], "op": "eq", "value": 1}]},
        {"sort": {"field": [], "dir": "asc"}},
        {"limit": True},
    ],
)
def test_13_malformed_query_is_a_400(api, body):
    client, dataset, _ = api
    reply = client.post(f"/datasets/{dataset}/records/query", body)
    assert reply.status == 400 and reply.json()["error"]["code"] in {
        "query_invalid",
        "request_invalid",
    }


def test_13_null_position_is_a_400(api):
    client, _, kind = api
    field = client.get(f"/types/{kind}").json()["fields"][0]
    reply = client.patch(f"/fields/{field['id']}", {"position": None})
    assert reply.status == 400 and reply.json()["error"]["field"] == "position"


def test_14_too_large_body_is_json(api):
    client, _, _ = api
    reply = client.post("/sites", raw=b'{"name":"' + b"a" * (10 * 1024 * 1024) + b'"}')
    assert reply.status == 413
    assert json.loads(reply.body)["error"]["code"] == "request_too_large"


def test_15_date_with_garbage_is_a_400(api):
    client, dataset, _ = api
    reply = client.post(f"/datasets/{dataset}/records", {"values": {"due": "2024-02-29Tмусор"}})
    assert reply.status == 400 and reply.json()["error"] == {
        "code": "value_date",
        "message": "Ожидается дата в виде ГГГГ-ММ-ДД или ДД.ММ.ГГГГ",
        "field": "due",
    }
