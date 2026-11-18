"""Выгрузка реестра и круговой обмен с мастером импорта (ТЗ 6.3, EXP-01…05, EXP-08, UI-14, AT-42).

Данные вымышленные.
"""

from __future__ import annotations

import io
import json

import pytest
from openpyxl import load_workbook

from server import export
from server.app import create_app

from .asgi import Client

NEW_PASSWORD = "свой-пароль-пользователя"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "export.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


def _field(admin, type_id, spec):
    made = admin.post(f"/types/{type_id}/fields", spec)
    assert made.status == 201, made.json()
    return made.json()


@pytest.fixture
def plant(admin):
    site = admin.post("/sites", {"name": "ТЭЦ"}).json()
    cable_type = admin.post("/types", {"name": "Кабель"}).json()
    _field(
        admin, cable_type["id"], {"name": "Номер", "kind": "text", "key": "number", "unique": True}
    )
    cables = admin.post(
        "/datasets", {"site_id": site["id"], "type_id": cable_type["id"], "name": "Кабели"}
    ).json()
    cable = admin.post(f"/datasets/{cables['id']}/records", {"values": {"number": "K-11"}}).json()
    states = admin.post("/dictionaries", {"name": "Состояние"}).json()
    for value in ("В работе", "Резерв"):
        admin.post(f"/dictionaries/{states['id']}/values", {"value": value})
    kind = admin.post("/types", {"name": "Сигнал"}).json()
    for spec in (
        {"name": "Код", "kind": "text", "key": "code", "unique": True},
        {"name": "ККС", "kind": "kks", "key": "kks", "kks_role": "object", "filterable": True},
        {"name": "Шкаф", "kind": "text", "key": "cabinet"},
        {"name": "Ток", "kind": "number", "key": "current", "filterable": True},
        {"name": "Проверен", "kind": "date", "key": "checked"},
        {"name": "Резервный", "kind": "boolean", "key": "spare"},
        {"name": "Состояние", "kind": "choice", "key": "state", "dictionary_id": states["id"]},
        {"name": "Метки", "kind": "text", "key": "tags", "multiple": True},
        {"name": "Кабель", "kind": "record", "key": "cable", "target_type_id": cable_type["id"]},
        {"name": "Примечание", "kind": "text", "key": "note"},
    ):
        _field(admin, kind["id"], spec)
    dataset = admin.post(
        "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": "Сигналы"}
    ).json()
    rows = [
        ("10ABC02AA101XB01", "10ABC02AA101", "00ZZA90GH002", 4.5, "2026-03-01", False, "В работе", ["DI", "НО"], "K-11", "=СУММ(A1:A2)"),
        ("10ABC02AA101XB02", "10ABC02AA101", "00ZZA90GH002", 4.5, "2026-03-02", False, "В работе", ["DI"], "K-11", "+7 900"),
        ("10ABC01CP001XQ01", "10ABC01CP001", "00ZZA90GH001", 20, None, True, "Резерв", None, None, "обычный текст"),
    ]  # fmt: skip
    keys = (
        "code",
        "kks",
        "cabinet",
        "current",
        "checked",
        "spare",
        "state",
        "tags",
        "cable",
        "note",
    )
    records = []
    for row in rows:
        values = {key: value for key, value in zip(keys, row, strict=True) if value is not None}
        made = admin.post(f"/datasets/{dataset['id']}/records", {"values": values})
        assert made.status == 201, made.json()
        records.append(made.json())
    return {"site": site, "type": kind, "dataset": dataset, "records": records, "cable": cable}


def _export(client, dataset_id, **body):
    return client.post(f"/datasets/{dataset_id}/export", body, key="")


def _book(reply):
    return load_workbook(io.BytesIO(reply.body))


def _upload(client, data):
    return client.request("POST", "/imports", raw=data, query={"name": "правка.xlsx"}, key="")


# Excel и круговой обмен (EXP-01, EXP-02) ------------------------------------------------


def test_xlsx_has_hidden_uuid_and_version_and_values_as_people_read_them(admin, plant):
    reply = _export(admin, plant["dataset"]["id"])
    assert reply.status == 200, reply.json()
    headers = dict(reply.headers)
    assert headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "filename*=UTF-8''" in headers["content-disposition"]
    page = _book(reply).active
    assert page.title == "Сигналы" and page.freeze_panes == "C2"
    assert page.column_dimensions["A"].hidden and page.column_dimensions["B"].hidden
    header = [cell.value for cell in page[1]]
    assert header[:5] == ["UUID", "Версия", "Код", "ККС", "Шкаф"]
    first = dict(zip(header, [cell.value for cell in page[2]], strict=True))
    assert first["UUID"] == plant["records"][0]["id"] and first["Версия"] == 1
    assert first["ККС"] == "10ABC02AA101" and first["Состояние"] == "В работе"
    assert first["Метки"] == "DI; НО" and first["Кабель"] == "K-11"
    assert first["Проверен"].date().isoformat() == "2026-03-01" and first["Резервный"] is False
    assert (
        first["Примечание"] == "=СУММ(A1:A2)" and page["L2"].data_type == "s"
    )  # не формула (EXP-05)


def test_edited_export_goes_back_through_the_wizard_without_new_versions(admin, plant):
    book = _book(_export(admin, plant["dataset"]["id"]))
    page = book.active
    header = [cell.value for cell in page[1]]
    page.cell(3, header.index("Ток") + 1).value = 5.0  # правка одного значения
    buffer = io.BytesIO()
    book.save(buffer)
    batch = _upload(admin, buffer.getvalue()).json()
    shown = admin.get(
        f"/imports/{batch['id']}/preview", sheet="Сигналы", dataset_id=plant["dataset"]["id"]
    ).json()
    mapping = shown["suggested"]["mapping"]
    assert mapping["UUID"] == "@id" and mapping["Версия"] == "@version" and len(mapping) == 12
    planned = admin.request(
        "POST",
        f"/imports/{batch['id']}/plan",
        {
            "sheet": "Сигналы",
            "mode": "records",
            "dataset_id": plant["dataset"]["id"],
            "mapping": mapping,
        },
        key="",
    ).json()
    assert planned["summary"]["counts"] == {
        "create": 0,
        "update": 1,
        "unchanged": 2,
        "conflict": 0,
        "error": 0,
    }  # каждое значение выгрузки читается импортом так же — без лишних версий
    (changed,) = admin.get(f"/imports/{batch['id']}/rows", action="update").json()["items"]
    assert changed["changes"] == {"current": [4.5, 5.0]}


def test_group_export_merges_common_values_and_reads_back_unchanged(admin, plant):
    reply = _export(admin, plant["dataset"]["id"], group_by="kks", template=True)
    assert reply.status == 200, reply.json()
    page = _book(reply).active
    header = [cell.value for cell in page[1]]
    merged = {str(item) for item in page.merged_cells.ranges}
    column = {name: chr(ord("A") + index) for index, name in enumerate(header)}
    # Строки по ККС: 10ABC01… (строка 2), затем два сигнала 10ABC02… (строки 3–4).
    assert f"{column['ККС']}3:{column['ККС']}4" in merged  # один аппарат — одна ячейка (UI-14)
    assert (
        f"{column['Шкаф']}3:{column['Шкаф']}4" in merged
        and f"{column['Ток']}3:{column['Ток']}4" in merged
    )
    assert not any(item.startswith(f"{column['Код']}3") for item in merged)  # коды разные
    assert not any(item.startswith(("A3", "B3")) for item in merged)  # UUID и версия — нет
    batch = _upload(admin, reply.body).json()
    mapping = admin.get(
        f"/imports/{batch['id']}/preview", sheet="Сигналы", dataset_id=plant["dataset"]["id"]
    ).json()["suggested"]["mapping"]
    planned = admin.request(
        "POST",
        f"/imports/{batch['id']}/plan",
        {
            "sheet": "Сигналы",
            "mode": "records",
            "dataset_id": plant["dataset"]["id"],
            "mapping": mapping,
        },
        key="",
    ).json()
    assert planned["summary"]["counts"]["unchanged"] == 3  # объединённые ячейки заполнены обратно


# Отбор, столбцы, форматы ----------------------------------------------------------


def test_filters_columns_and_headers(admin, plant):
    body = {
        "query": {"where": [{"field": "current", "op": "gt", "value": 10}]},
        "columns": ["code", "current"],
        "headers": {"code": "Код сигнала"},
        "template": False,
    }
    page = _book(_export(admin, plant["dataset"]["id"], **body)).active
    rows = [[cell.value for cell in row] for row in page.iter_rows()]
    assert rows == [["Код сигнала", "Ток"], ["10ABC01CP001XQ01", 20]]
    bad = _export(admin, plant["dataset"]["id"], columns=["nope"])
    assert bad.status == 400 and bad.json()["error"]["code"] == "export_columns"
    multi = _export(admin, plant["dataset"]["id"], group_by="tags")
    assert multi.json()["error"]["code"] == "export_group"


def test_csv_is_excel_friendly_and_escapes_formulas(admin, plant):
    reply = _export(
        admin, plant["dataset"]["id"], format="csv", sort={"field": "code", "dir": "asc"}
    )
    assert dict(reply.headers)["content-type"].startswith("text/csv")
    content = reply.body.decode("utf-8")
    assert content.startswith("﻿UUID;Версия;Код;ККС")
    lines = content.lstrip("﻿").split("\r\n")
    assert "'=СУММ(A1:A2)" in lines[2] and "'+7 900" in lines[3]  # EXP-05
    assert ";да;" in lines[1] and ";2026-03-01;" in lines[2]


def test_json_is_open_exchange(admin, plant):
    reply = _export(admin, plant["dataset"]["id"], format="json")
    data = json.loads(reply.body)
    assert data["dataset"]["name"] == "Сигналы" and len(data["records"]) == 3
    first = data["records"][0]
    assert first["values"]["tags"] == "DI; НО" and first["values"]["spare"] is False
    assert {item["key"] for item in data["fields"]} >= {"code", "kks", "cable"}


# Права и пределы (EXP-04) -------------------------------------------------------------


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


def test_hidden_fields_and_missing_rights(app, admin, plant):
    dataset_id = plant["dataset"]["id"]
    fields = {
        item["key"]: item["id"]
        for item in admin.get(f"/types/{plant['type']['id']}").json()["fields"]
    }
    reader = _user(
        app,
        admin,
        "reader",
        [
            {"scope": "dataset", "target": dataset_id, "action": "view"},
            {"scope": "dataset", "target": dataset_id, "action": "export"},
            {"scope": "field", "target": fields["note"], "action": "hidden"},
        ],
    )
    header = [cell.value for cell in _book(_export(reader, dataset_id)).active[1]]
    assert "Примечание" not in header and "Код" in header
    hidden_column = _export(reader, dataset_id, columns=["note"])
    assert hidden_column.json()["error"]["code"] == "export_columns"
    viewer = _user(
        app, admin, "viewer", [{"scope": "dataset", "target": dataset_id, "action": "view"}]
    )
    assert _export(viewer, dataset_id).status == 403
    blind = _user(
        app, admin, "blind", [{"scope": "dataset", "target": dataset_id, "action": "export"}]
    )
    assert _export(blind, dataset_id).status == 403


def test_too_many_rows_is_a_clear_error(admin, plant, monkeypatch):
    monkeypatch.setattr(export, "MAX_ROWS", 2)
    reply = _export(admin, plant["dataset"]["id"], format="csv")
    assert reply.status == 400 and reply.json()["error"]["code"] == "export_too_large"
