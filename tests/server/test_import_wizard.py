"""Мастер импорта Excel: загрузка, план на сервере, применение, профили (ТЗ 6.1, IMP-01…17).

Книги вымышленные, собираются в тесте.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from server.app import create_app

from .asgi import Client

NEW_PASSWORD = "свой-пароль-пользователя"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "import.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


@pytest.fixture
def plant(admin):
    site = admin.post("/sites", {"name": "ТЭЦ"}).json()
    kind = admin.post("/types", {"name": "Насос"}).json()
    fields = {}
    for spec in (
        {"name": "Название", "kind": "text", "key": "name", "unique": True},
        {"name": "Мощность", "kind": "number", "key": "power"},
        {"name": "Примечание", "kind": "text", "key": "note"},
        {"name": "ККС", "kind": "kks", "key": "kks", "kks_role": "object"},
    ):
        made = admin.post(f"/types/{kind['id']}/fields", spec)
        assert made.status == 201, made.json()
        fields[spec["key"]] = made.json()
    dataset = admin.post(
        "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": "Насосы"}
    ).json()
    records = {}
    for name, power in (("Насос 1", 100), ("Насос 3", 300)):
        made = admin.post(
            f"/datasets/{dataset['id']}/records",
            {"values": {"name": name, "power": power, "note": "старое"}},
        ).json()
        records[name] = made
    return {"site": site, "type": kind, "fields": fields, "dataset": dataset, "records": records}


def _book(rows, *, title=None, sheet="Лист1"):
    book = Workbook()
    page = book.active
    page.title = sheet
    if title:
        page.append([title])
        page.append([])
    for row in rows:
        page.append(list(row))
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _upload(client, data, name="книга.xlsx"):
    return client.request("POST", "/imports", raw=data, query={"name": name}, key="")


def _plan(client, batch_id, body):
    return client.request("POST", f"/imports/{batch_id}/plan", body, key="")


def _apply(client, batch_id, body=None):
    return client.request("POST", f"/imports/{batch_id}/apply", body or {}, key="")


PUMPS = [
    ("Название", "Мощность", "Примечание"),
    ("Насос 1", 100, None),  # без изменений; пустая ячейка не стирает «старое»
    ("Насос 2", 200, "новый"),  # новая запись
    ("Насос 3", 350, None),  # правка мощности
    ("Насос 1", 999, None),  # повтор ключа в книге
    ("Насос 4", "много", None),  # мощность не число
]
MAPPING = {"Название": "name", "Мощность": "power", "Примечание": "note"}


def _records(admin, dataset_id):
    page = admin.post(f"/datasets/{dataset_id}/records/query", {"limit": 50}).json()
    return {item["doc"]["name"]: item for item in page["items"]}


# Шаг 1: книга и листы ------------------------------------------------------------


def test_upload_lists_sheets_with_guessed_header_row(admin, plant):
    data = _book(PUMPS, title="Перечень насосов на 18.11.2026")
    reply = _upload(admin, data)
    assert reply.status == 201, reply.json()
    batch = reply.json()
    (sheet,) = batch["sheets"]
    assert sheet["name"] == "Лист1" and sheet["header_row"] == 3 and sheet["rows"] == 8
    assert sheet["top"][2][:2] == ["Название", "Мощность"]
    assert batch["status"] == "uploaded" and batch["file"]["name"] == "книга.xlsx"
    shown = admin.get(
        f"/imports/{batch['id']}/preview",
        sheet="Лист1",
        header_row=3,
        dataset_id=plant["dataset"]["id"],
    ).json()
    assert shown["headers"] == ["Название", "Мощность", "Примечание"]
    assert shown["suggested"]["mapping"] == MAPPING
    assert shown["sample"][0] == {"row": 4, "cells": {"Название": "Насос 1", "Мощность": 100}}
    assert shown["suggested"]["mode"] == "records" and shown["profiles"] == []


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("книга.csv", b"PK\x03\x04", "import_file_name"),
        ("книга.xlsx", b"not a zip", "xlsx_invalid"),
        ("книга.xlsx", b"PK\x03\x04broken", "xlsx_invalid"),
    ],
)
def test_bad_files_are_refused(admin, name, data, code):
    reply = _upload(admin, data, name)
    assert reply.status == 400 and reply.json()["error"]["code"] == code


# Шаги 2–6: обычный импорт в реестр -------------------------------------------------


def test_plan_classifies_rows_and_apply_writes_them_at_once(admin, plant):
    batch = _upload(admin, _book(PUMPS)).json()
    body = {
        "sheet": "Лист1",
        "mode": "records",
        "dataset_id": plant["dataset"]["id"],
        "mapping": MAPPING,
        "key": "name",
    }
    planned = _plan(admin, batch["id"], body)
    assert planned.status == 200, planned.json()
    counts = planned.json()["summary"]["counts"]
    assert counts == {"create": 1, "update": 1, "unchanged": 1, "conflict": 1, "error": 1}
    assert set(_records(admin, plant["dataset"]["id"])) == {"Насос 1", "Насос 3"}  # план не пишет
    rows = admin.get(f"/imports/{batch['id']}/rows").json()["items"]
    by_row = {item["row"]: item for item in rows}
    assert by_row[4]["changes"] == {"power": [300, 350]} and by_row[4]["action"] == "update"
    assert by_row[5]["reason"] == "key_duplicate" and "строкой 2" in by_row[5]["message"]
    assert by_row[6]["action"] == "error"
    conflicts = admin.get(f"/imports/{batch['id']}/rows", action="conflict").json()["items"]
    assert [item["row"] for item in conflicts] == [5]

    report = _apply(admin, batch["id"])
    assert report.status == 200, report.json()
    assert report.json()["counts"] == counts
    found = _records(admin, plant["dataset"]["id"])
    assert found["Насос 3"]["doc"]["power"] == 350 and found["Насос 2"]["doc"]["note"] == "новый"
    assert found["Насос 1"]["doc"]["note"] == "старое" and found["Насос 1"]["version"] == 1
    assert "Насос 4" not in found
    assert _apply(admin, batch["id"]).json() == report.json()  # повтор — тот же отчёт (IMP-06)
    assert admin.get(f"/imports/{batch['id']}/rows").json()["items"] == []

    again = _upload(admin, _book(PUMPS)).json()
    replanned = _plan(admin, again["id"], body).json()["summary"]["counts"]
    assert replanned["create"] == 0 and replanned["update"] == 0 and replanned["unchanged"] == 3


def test_stale_plan_is_not_applied(admin, plant):
    batch = _upload(admin, _book(PUMPS[:4])).json()
    body = {
        "sheet": "Лист1",
        "mode": "records",
        "dataset_id": plant["dataset"]["id"],
        "mapping": MAPPING,
        "key": "name",
    }
    _plan(admin, batch["id"], body)
    record = plant["records"]["Насос 3"]
    edited = admin.patch(
        f"/records/{record['id']}",
        {"values": {"power": 310}, "expected_version": record["version"]},
    )
    assert edited.status == 200, edited.json()
    stale = _apply(admin, batch["id"])
    assert stale.status == 409 and stale.json()["error"]["code"] == "import_stale"
    assert "Насос 2" not in _records(admin, plant["dataset"]["id"])  # ничего не записано (IMP-05)
    assert _plan(admin, batch["id"], body).status == 200  # новый план видит правку
    assert _apply(admin, batch["id"]).status == 200
    admin.post(f"/types/{plant['type']['id']}/fields", {"name": "Шифр", "kind": "text"})
    other = _upload(admin, _book(PUMPS[:2])).json()
    _plan(admin, other["id"], body)
    admin.post(f"/types/{plant['type']['id']}/fields", {"name": "Завод", "kind": "text"})
    moved = _apply(admin, other["id"])
    assert moved.json()["error"]["code"] == "import_stale"  # схема изменилась


def test_template_rows_find_records_by_uuid_and_detect_later_edits(admin, plant):
    first, third = plant["records"]["Насос 1"], plant["records"]["Насос 3"]
    rows = [
        ("UUID", "Версия", "Название", "Мощность"),
        (first["id"], 1, "Насос 1", 110),
        (third["id"], 1, "Насос 3", 330),
    ]
    edited = admin.patch(
        f"/records/{third['id']}", {"values": {"note": "правка"}, "expected_version": 1}
    )
    assert edited.status == 200, edited.json()
    batch = _upload(admin, _book(rows)).json()
    shown = admin.get(
        f"/imports/{batch['id']}/preview", sheet="Лист1", dataset_id=plant["dataset"]["id"]
    ).json()
    mapping = shown["suggested"]["mapping"]
    assert mapping == {"UUID": "@id", "Версия": "@version", "Название": "name", "Мощность": "power"}
    body = {
        "sheet": "Лист1",
        "mode": "records",
        "dataset_id": plant["dataset"]["id"],
        "mapping": mapping,
    }
    counts = _plan(admin, batch["id"], body).json()["summary"]["counts"]
    assert counts["update"] == 1 and counts["conflict"] == 1
    conflict = admin.get(f"/imports/{batch['id']}/rows", action="conflict").json()["items"][0]
    assert conflict["reason"] == "version_conflict" and conflict["record_id"] == third["id"]
    decided = admin.put(
        f"/imports/{batch['id']}/rows/{conflict['row']}/decision", {"decision": "apply"}
    )
    assert decided.json()["decision"] == "apply"
    report = _apply(admin, batch["id"]).json()
    assert report["counts"]["update"] == 2 and report["skipped"] == []  # спорная применена поверх
    assert _records(admin, plant["dataset"]["id"])["Насос 3"]["doc"]["power"] == 330


def test_duplicate_key_can_only_be_skipped(admin, plant):
    batch = _upload(admin, _book(PUMPS)).json()
    body = {
        "sheet": "Лист1",
        "mode": "records",
        "dataset_id": plant["dataset"]["id"],
        "mapping": MAPPING,
        "key": "name",
    }
    _plan(admin, batch["id"], body)
    refused = admin.put(f"/imports/{batch['id']}/rows/5/decision", {"decision": "apply"})
    assert refused.status == 400 and refused.json()["error"]["code"] == "import_decision"
    assert admin.put(f"/imports/{batch['id']}/rows/3/decision", {"decision": "skip"}).status == 409


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"key": "power"}, "import_key"),  # не уникальное
        ({"key": "kks", "mapping": {**MAPPING, "ККС": "kks"}}, "import_key"),  # ККС не ключ
        ({"mapping": {"Нет такого": "name"}}, "import_column"),
        ({"mapping": {"Название": "name", "Мощность": "name"}}, "import_mapping"),
        ({"mapping": {}}, "import_mapping"),
    ],
)
def test_bad_mapping_is_refused(admin, plant, change, code):
    rows = [(*PUMPS[0], "ККС"), ("Насос 5", 1, None, "10LAC10AP001")]
    batch = _upload(admin, _book(rows)).json()
    body = {
        "sheet": "Лист1",
        "mode": "records",
        "dataset_id": plant["dataset"]["id"],
        "mapping": MAPPING,
        "key": "name",
        **change,
    }
    reply = _plan(admin, batch["id"], body)
    assert reply.status == 400 and reply.json()["error"]["code"] == code


# Права (IMP-12) и чужие пакеты -------------------------------------------------------


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


def test_rights_follow_target_and_batches_are_private(app, admin, plant):
    dataset_id = plant["dataset"]["id"]
    viewer = _user(app, admin, "viewer", [{"scope": "dataset", "target": "*", "action": "view"}])
    assert _upload(viewer, _book(PUMPS)).status == 403
    other = admin.post(
        "/datasets",
        {"site_id": plant["site"]["id"], "type_id": plant["type"]["id"], "name": "Склад"},
    ).json()
    clerk = _user(
        app,
        admin,
        "clerk",
        [
            {"scope": "dataset", "target": other["id"], "action": "view"},
            {"scope": "dataset", "target": other["id"], "action": "import"},
            {"scope": "dataset", "target": other["id"], "action": "create"},
        ],
    )
    batch = _upload(clerk, _book(PUMPS)).json()
    body = {"sheet": "Лист1", "mode": "records", "dataset_id": dataset_id, "mapping": MAPPING}
    assert _plan(clerk, batch["id"], body).status == 403  # нет импорта в «Насосы»
    assert _plan(clerk, batch["id"], {**body, "dataset_id": other["id"]}).status == 200
    new_type = {
        "sheet": "Лист1",
        "mode": "new_type",
        "new_type": {
            "type_name": "Новое",
            "dataset_name": "Новое",
            "site_id": plant["site"]["id"],
            "fields": [{"column": "Название"}],
        },
    }
    assert _plan(clerk, batch["id"], new_type).status == 403  # нужна «Структура»
    assert admin.get(f"/imports/{batch['id']}").status == 404  # чужой пакет не виден


def test_expired_batch_cannot_be_planned(app, admin, plant):
    batch = _upload(admin, _book(PUMPS)).json()
    with app.state.db.write(__import__("engine").SYSTEM) as tx:
        tx.conn.execute(
            "UPDATE import_batches SET expires_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
            (batch["id"],),
        )
    body = {
        "sheet": "Лист1",
        "mode": "records",
        "dataset_id": plant["dataset"]["id"],
        "mapping": MAPPING,
    }
    reply = _plan(admin, batch["id"], body)
    assert reply.status == 409 and reply.json()["error"]["code"] == "import_expired"


# Новый тип ------------------------------------------------------------------------


def test_new_type_is_created_only_on_apply(admin, plant):
    rows = [("Шифр", "Ду", "ККС"), ("З-1", 100, "10LAB10AA101"), ("З-2", 150, "10LAB10AA102")]
    batch = _upload(admin, _book(rows, sheet="Задвижки")).json()
    body = {
        "sheet": "Задвижки",
        "mode": "new_type",
        "new_type": {
            "type_name": "Задвижка",
            "dataset_name": "Задвижки",
            "site_id": plant["site"]["id"],
            "fields": [
                {"column": "Шифр", "unique": True},
                {"column": "Ду", "name": "Диаметр", "kind": "integer"},
                {"column": "ККС", "kind": "kks"},
            ],
        },
    }
    planned = _plan(admin, batch["id"], body).json()
    assert planned["summary"]["counts"]["create"] == 2
    assert "Задвижка" not in {item["name"] for item in admin.get("/types").json()["items"]}
    report = _apply(admin, batch["id"]).json()
    page = admin.post(f"/datasets/{report['dataset_id']}/records/query", {"limit": 10}).json()
    assert len(page["items"]) == 2
    kind = admin.get(f"/types/{report['type_id']}").json()
    assert kind["name"] == "Задвижка"
    kinds = {item["name"]: item["kind"] for item in kind["fields"]}
    assert kinds == {"Шифр": "text", "Диаметр": "integer", "ККС": "kks"}


# Магнит ККС --------------------------------------------------------------------------

SIGNALS = [
    ("ККС", "ККС сигнала", "Шкаф ПТК", "№ кабеля", "Клеммник ПТК", "Клемма ПТК 1", "Клемма ПТК 2"),
    ("10ABC01CP001", "10ABC01CP001XQ01", "00ZZA90GH001", "K-25", "X25", "11", "12"),
    (None, "10ABC02AA101XB01", "00ZZA90GH002", "K-11", "X11", "31", "32"),
    (None, "10ABC02AA101XB02", "00ZZA90GH002", "K-11", "X11", "33", "34"),
]


def test_magnet_mode_lays_signals_and_cables_out(admin, plant):
    batch = _upload(admin, _book(SIGNALS, sheet="Кабели")).json()
    shown = admin.get(f"/imports/{batch['id']}/preview", sheet="Кабели").json()
    assert shown["suggested"]["mode"] == "magnet"
    assert shown["suggested"]["layout"]["signal"] == "ККС сигнала"
    body = {
        "sheet": "Кабели",
        "mode": "magnet",
        "name": "Кабельная база",
        "site_id": plant["site"]["id"],
    }
    planned = _plan(admin, batch["id"], body).json()["summary"]
    assert planned["datasets"] == ["Кабельная база — сигналы", "Кабельная база — кабели"]
    assert planned["counts"]["create"] == 5  # 3 сигнала и 2 кабеля
    names = {item["name"] for item in admin.get("/datasets").json()["items"]}
    assert "Кабельная база — сигналы" not in names  # план ничего не создал
    report = _apply(admin, batch["id"]).json()
    assert report["counts"]["create"] == 5
    names = {item["name"] for item in admin.get("/datasets").json()["items"]}
    assert {"Кабельная база — сигналы", "Кабельная база — кабели"} <= names

    again = _upload(admin, _book(SIGNALS, sheet="Кабели")).json()
    repeat = _plan(admin, again["id"], body).json()["summary"]["counts"]
    assert repeat["create"] == 0 and repeat["unchanged"] == 5
    admin.put("/kks/rules", {"signal_pattern": "X[A-Z][0-9]{2}|-[0-9]{2}"})
    stale = _apply(admin, again["id"])
    assert stale.status == 409 and stale.json()["error"]["code"] == "import_stale"


# Профиль (IMP-08) ---------------------------------------------------------------------


def test_profile_is_offered_for_next_book_of_the_same_kind(app, admin, plant):
    batch = _upload(admin, _book(PUMPS[:3])).json()
    body = {
        "sheet": "Лист1",
        "mode": "records",
        "dataset_id": plant["dataset"]["id"],
        "mapping": MAPPING,
        "key": "name",
    }
    _plan(admin, batch["id"], body)
    report = _apply(admin, batch["id"], {"save_profile": "Насосы из ПТО"}).json()
    assert report["profile"]["name"] == "Насосы из ПТО"
    following = _upload(admin, _book([PUMPS[0], ("Насос 9", 9, None)])).json()
    offered = admin.get(f"/imports/{following['id']}/preview", sheet="Лист1").json()["profiles"]
    assert [item["name"] for item in offered] == ["Насосы из ПТО"]
    assert offered[0]["settings"]["mapping"] == MAPPING and "sheet" not in offered[0]["settings"]
    clerk = _user(app, admin, "clerk", [{"scope": "dataset", "target": "*", "action": "import"}])
    profile_id = offered[0]["id"]
    assert clerk.delete(f"/import-profiles/{profile_id}").status == 403
    assert admin.delete(f"/import-profiles/{profile_id}").status == 204
    assert admin.get("/import-profiles").json()["items"] == []
