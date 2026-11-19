"""Заметки знаний и словарь станции (ТЗ 8, KNOW-06…10, SEC-06, AT-29, AT-30). Данные вымышленные."""

from __future__ import annotations

import pytest

from server.app import create_app
from server.worker import run_pending

from .asgi import Client

NEW_PASSWORD = "свой-пароль-пользователя"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "know.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


@pytest.fixture
def plant(admin):
    site = admin.post("/sites", {"name": "ТЭЦ"}).json()
    pump = admin.post(f"/sites/{site['id']}/kks", {"code": "10LAC10AP001"}).json()
    kind = admin.post("/types", {"name": "Насос"}).json()
    for spec in (
        {"name": "Название", "kind": "text", "key": "name"},
        {"name": "ККС", "kind": "kks", "key": "kks", "kks_role": "object"},
        {"name": "Давление", "kind": "number", "key": "pressure"},
    ):
        admin.post(f"/types/{kind['id']}/fields", spec)
    datasets = {
        name: admin.post(
            "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": name}
        ).json()
        for name in ("Насосы", "Закрытые")
    }
    pump_row = admin.post(
        f"/datasets/{datasets['Насосы']['id']}/records",
        {"values": {"name": "ПЭН-1", "kks": "10LAC10AP001", "pressure": 16}},
    ).json()
    secret = admin.post(
        f"/datasets/{datasets['Закрытые']['id']}/records", {"values": {"name": "резерв"}}
    ).json()
    return {
        "site": site,
        "pump": pump,
        "type": kind,
        "datasets": datasets,
        "row": pump_row,
        "secret": secret,
    }


def _user(app, admin, login, role_name=None, permissions=None):
    if role_name:
        role_id = {item["name"]: item["id"] for item in admin.get("/roles").json()["items"]}[
            role_name
        ]
    else:
        role_id = admin.post("/roles", {"name": f"Роль {login}"}).json()["id"]
        assert (
            admin.put(f"/roles/{role_id}/permissions", {"permissions": permissions}).status == 200
        )
    created = admin.post("/users", {"login": login, "name": login.title(), "role_id": role_id})
    client = Client(app, login=login, password=created.json()["temporary_password"])
    client.post(
        "/auth/password",
        {"current_password": created.json()["temporary_password"], "new_password": NEW_PASSWORD},
    )
    return client


def _note(client, **body):
    reply = client.post("/knowledge/notes", body)
    assert reply.status == 201, reply.json()
    return reply.json()


# Жизнь заметки (KNOW-06…09, AT-29) ------------------------------------------------


def test_note_goes_from_proposed_to_confirmed_to_stale_and_back(app, admin, plant):
    engineer = _user(app, admin, "petrov", role_name="Инженер")
    note = _note(
        engineer,
        title="Рабочее давление ПЭН-1",
        body="Рабочее давление насоса 10LAC10AP001 — 16 кгс/см², выше 18 — останов.",
        scope={"kind": "kks", "id": plant["pump"]["id"]},
        sources=[{"kind": "record", "id": plant["row"]["id"]}],
    )
    assert note["state"] == "proposed" and note["origin"] == "user" and note["author"] == "Petrov"
    assert note["sources"][0]["version"] == 1 and note["scope"]["code"] == "10LAC10AP001"
    assert admin.get("/knowledge/notes").json()["items"] == []  # ИИ видит только подтверждённые
    assert admin.get("/search", q="останов").json()["notes"] == []
    refused = engineer.post(f"/knowledge/notes/{note['id']}/confirm", {"version": 1})
    assert refused.status == 403  # подтверждать — отдельное право (KNOW-09)
    queue = admin.get("/knowledge/queue").json()
    assert queue["counts"] == {"proposed": 1, "stale": 0}

    confirmed = admin.post(f"/knowledge/notes/{note['id']}/confirm", {"version": 1}).json()
    assert (
        confirmed["state"] == "confirmed" and confirmed["confirmed_by"] == "Тестовый администратор"
    )
    found = admin.get("/search", q="давление останов").json()["notes"]
    assert [item["id"] for item in found] == [note["id"]]

    start = admin.get("/events").json()["cursor"]
    row = plant["row"]
    admin.patch(
        f"/records/{row['id']}", {"values": {"pressure": 17}, "expected_version": row["version"]}
    )
    stale = admin.get(f"/knowledge/notes/{note['id']}").json()
    assert (
        stale["state"] == "stale" and stale["stale_reason"] == "Запись-источник изменена"
    )  # KNOW-08
    assert stale["sources"][0] | {"current_version": 2} == stale["sources"][0]
    events = admin.get("/events", after=start).json()["events"]
    assert ("note.stale", note["id"]) in {(item["action"], item["id"]) for item in events}
    assert admin.get("/knowledge/queue").json()["counts"] == {"proposed": 0, "stale": 1}
    assert admin.get("/search", q="останов").json()["notes"] == []  # устаревшая — не опора

    checked = admin.post(
        f"/knowledge/notes/{note['id']}/confirm", {"version": stale["version"]}
    ).json()
    assert checked["state"] == "confirmed" and checked["sources"][0]["version"] == 2


def test_document_replacement_and_record_archive_make_notes_stale(app, admin, plant):
    document = admin.request(
        "POST", "/documents", raw="Паспорт ПЭН".encode(), query={"name": "p.txt"}, key=""
    ).json()
    run_pending(app.state.db, app.state.files)
    by_document = _note(
        admin, title="Из паспорта", body="Подача 250 м³/ч",
        sources=[{"kind": "document", "id": document["id"], "page": "1"}],
    )  # fmt: skip
    by_record = _note(
        admin,
        title="Из записи",
        body="Насос в работе",
        sources=[{"kind": "record", "id": plant["row"]["id"]}],
    )
    for item in (by_document, by_record):
        admin.post(f"/knowledge/notes/{item['id']}/confirm", {"version": item["version"]})
    admin.request(
        "PUT", f"/documents/{document['id']}/file", raw="Паспорт ПЭН, ред. 2".encode(),
        query={"name": "p.txt", "version": 1}, key="",
    )  # fmt: skip
    assert admin.get(f"/knowledge/notes/{by_document['id']}").json()["stale_reason"] == (
        "Документ-источник заменён новой версией"
    )
    row = plant["row"]
    admin.post(f"/records/{row['id']}/archive", {"expected_version": row["version"]})
    assert admin.get(f"/knowledge/notes/{by_record['id']}").json()["stale_reason"] == (
        "Запись-источник отправлена в архив"
    )


def test_purged_source_marks_note_and_blocks_confirmation(app, admin, plant):
    note = _note(
        admin,
        title="О резерве",
        body="Резерв",
        sources=[{"kind": "record", "id": plant["secret"]["id"]}],
    )
    secret = plant["secret"]
    archived = admin.post(
        f"/records/{secret['id']}/archive", {"expected_version": secret["version"]}
    ).json()
    purged = admin.post(
        f"/datasets/{plant['datasets']['Закрытые']['id']}/records/purge",
        {"items": [{"id": secret["id"], "expected_version": archived["version"]}]},
    )
    assert purged.status == 200, purged.json()
    gone = admin.get(f"/knowledge/notes/{note['id']}").json()
    assert gone["state"] == "stale" and gone["stale_reason"] == "Запись-источник удалена"
    refused = admin.post(f"/knowledge/notes/{note['id']}/confirm", {"version": gone["version"]})
    assert refused.status == 400 and refused.json()["error"]["code"] == "note_source_missing"


def test_editing_returns_to_proposed_and_only_author_or_moderator_edits(app, admin, plant):
    engineer = _user(app, admin, "petrov", role_name="Инженер")
    other = _user(app, admin, "sidorov", role_name="Инженер")
    note = _note(engineer, title="Черновик", body="Текст")
    assert (
        other.patch(f"/knowledge/notes/{note['id']}", {"version": 1, "body": "Чужое"}).status == 403
    )
    confirmed = admin.post(f"/knowledge/notes/{note['id']}/confirm", {"version": 1}).json()
    edited = engineer.patch(
        f"/knowledge/notes/{note['id']}", {"version": confirmed["version"], "body": "Уточнено"}
    ).json()
    assert (
        edited["state"] == "proposed"
        and edited["body"] == "Уточнено"
        and edited["confirmed_by"] is None
    )
    stale = engineer.patch(f"/knowledge/notes/{note['id']}", {"version": 1, "body": "x"})
    assert stale.status == 409
    rejected = admin.post(
        f"/knowledge/notes/{note['id']}/reject", {"reason": "Не подтверждено ПТО"}
    ).json()
    assert rejected["state"] == "rejected" and rejected["reject_reason"] == "Не подтверждено ПТО"
    again = admin.post(f"/knowledge/notes/{note['id']}/confirm", {"version": rejected["version"]})
    assert again.json()["error"]["code"] == "note_rejected"


# Права по источникам (SEC-06) -------------------------------------------------------


def test_note_is_visible_only_to_those_who_see_all_its_sources(app, admin, plant):
    note = _note(
        admin, title="Резервный насос", body="Резервный насос запускать после ПЭН-1",
        sources=[{"kind": "record", "id": plant["row"]["id"]}, {"kind": "record", "id": plant["secret"]["id"]}],
    )  # fmt: skip
    admin.post(f"/knowledge/notes/{note['id']}/confirm", {"version": 1})
    shift = _user(
        app,
        admin,
        "shift",
        permissions=[
            {"scope": "dataset", "target": plant["datasets"]["Насосы"]["id"], "action": "view"}
        ],
    )
    assert shift.get(f"/knowledge/notes/{note['id']}").status == 404
    assert shift.get("/knowledge/notes").json()["items"] == []
    assert shift.get("/search", q="резервный насос").json()["notes"] == []
    refused = shift.post(
        "/knowledge/notes",
        {"title": "x", "body": "y", "sources": [{"kind": "record", "id": plant["secret"]["id"]}]},
    )
    assert refused.status == 400 and refused.json()["error"]["code"] == "note_source_missing"
    assert len(admin.get("/knowledge/notes").json()["items"]) == 1


# Словарь станции (KNOW-10, AT-30) ----------------------------------------------------


def test_local_name_finds_object_by_official_code(app, admin, plant):
    term = admin.post(
        "/knowledge/glossary",
        {
            "term": "Пэн1",
            "meaning": "Питательный электронасос № 1",
            "kks_ids": [plant["pump"]["id"]],
        },
    )
    assert term.status == 201, term.json()
    found = admin.get("/search", q="пэн1").json()
    assert found["glossary"][0]["term"] == "Пэн1"
    first = found["kks"][0]
    assert first["code"] == "10LAC10AP001" and first["via"] == "glossary" and first["exact"]
    assert [item["id"] for item in found["records"]] == [plant["row"]["id"]]  # запись по коду
    sentence = admin.get("/search", q="давление пэн1").json()
    assert sentence["kks"][0]["code"] == "10LAC10AP001"  # термин внутри фразы
    duplicate = admin.post("/knowledge/glossary", {"term": "ПЭН1", "meaning": "другое"})
    assert duplicate.status == 409 and duplicate.json()["error"]["code"] == "term_taken"
    engineer = _user(app, admin, "petrov", role_name="Инженер")
    assert engineer.post("/knowledge/glossary", {"term": "x", "meaning": "y"}).status == 403
    assert [
        item["term"] for item in engineer.get("/knowledge/glossary", q="электро").json()["items"]
    ] == ["Пэн1"]
    term_id = term.json()["id"]
    updated = admin.put(
        f"/knowledge/glossary/{term_id}",
        {"term": "ПЭН-1", "meaning": "Питательный насос 1", "kks_ids": []},
    ).json()
    assert updated["term"] == "ПЭН-1" and updated["kks"] == []
    assert admin.delete(f"/knowledge/glossary/{term_id}").status == 204
    assert admin.get("/knowledge/glossary").json()["items"] == []
