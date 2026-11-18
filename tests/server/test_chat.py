"""Общий чат и события об изменениях (ТЗ 11.3, CHAT-01…09, API-09, AT-44)."""

from __future__ import annotations

import threading
import time

import pytest

from engine import SYSTEM
from server.app import create_app

from .asgi import Client

NEW_PASSWORD = "свой-пароль-пользователя"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "chat.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


def _roles(admin):
    return {role["name"]: role["id"] for role in admin.get("/roles").json()["items"]}


def _user(app, admin, login, role_id):
    created = admin.post("/users", {"login": login, "name": login.title(), "role_id": role_id})
    assert created.status == 201, created.json()
    client = Client(app, login=login, password=created.json()["temporary_password"])
    changed = client.post(
        "/auth/password",
        {"current_password": created.json()["temporary_password"], "new_password": NEW_PASSWORD},
    )
    assert changed.status == 200, changed.json()
    return client


@pytest.fixture
def engineer(app, admin):
    return _user(app, admin, "petrov", _roles(admin)["Инженер"])


@pytest.fixture
def plant(admin):
    site = admin.post("/sites", {"name": "ТЭЦ"}).json()
    kind = admin.post("/types", {"name": "Насос"}).json()
    admin.post(f"/types/{kind['id']}/fields", {"name": "Название", "kind": "text", "key": "name"})
    datasets = [
        admin.post("/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": name}).json()
        for name in ("Насосы", "Секретные насосы")
    ]
    records = [
        admin.post(f"/datasets/{item['id']}/records", {"values": {"name": "Насос"}}).json()
        for item in datasets
    ]
    device = admin.post(f"/sites/{site['id']}/kks", {"code": "10LAC10AP001"}).json()
    return {"site": site, "datasets": datasets, "records": records, "kks": device}


def _post(client, text):
    reply = client.post("/chat/messages", {"text": text})
    assert reply.status == 201, reply.json()
    return reply.json()


# Лента, авторство, непрочитанное ---------------------------------------------------


def test_message_reaches_colleague_with_author_and_unread_count(admin, engineer):
    sent = _post(admin, "  Насос ПЭН-1 выведен в ремонт\n")
    assert sent["text"] == "Насос ПЭН-1 выведен в ремонт"  # пробелы по краям сняты
    assert sent["author"]["name"] == "Тестовый администратор" and sent["mine"]
    feed = engineer.get("/chat/messages").json()
    (item,) = feed["items"]
    assert item["id"] == sent["id"] and not item["mine"] and item["state"] == "active"
    assert item["can"] == {
        "edit": False,
        "delete": False,
        "hide": False,
        "unhide": False,
        "history": False,
    }
    assert feed["unread"] == 1 and admin.get("/chat/messages").json()["unread"] == 0
    read = engineer.post("/chat/read", {"seq": item["seq"]}).json()
    assert read == {"last_read": item["seq"], "unread": 0}
    assert engineer.post("/chat/read", {"seq": 0}).json()["last_read"] == item["seq"]  # не назад


def test_feed_pages_by_cursor_both_ways(admin):
    sent = [_post(admin, f"сообщение {number}")["seq"] for number in range(5)]
    last = admin.get("/chat/messages", limit=2).json()
    assert [item["seq"] for item in last["items"]] == sent[3:] and last["more"]
    earlier = admin.get("/chat/messages", before=last["first"], limit=2).json()
    assert [item["seq"] for item in earlier["items"]] == sent[1:3]
    missed = admin.get("/chat/messages", after=sent[1]).json()  # догрузка после разрыва
    assert [item["seq"] for item in missed["items"]] == sent[2:] and not missed["more"]
    both = admin.get("/chat/messages", before=5, after=1).json()
    assert both["error"]["code"] == "chat_cursor"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("   ", "chat_text_empty"),
        ("я" * 4001, "chat_text_too_long"),
        ("звонок\x07", "chat_text_invalid"),
    ],
)
def test_bad_text_is_refused(admin, text, code):
    reply = admin.post("/chat/messages", {"text": text})
    assert reply.status == 400 and reply.json()["error"]["code"] == code


def test_rate_limit_per_account(admin, engineer):
    for number in range(30):
        _post(engineer, f"№{number}")
    reply = engineer.post("/chat/messages", {"text": "ещё одно"})
    assert reply.status == 429 and reply.json()["error"]["code"] == "chat_rate_limit"
    _post(admin, "у администратора свой счёт")


# Ссылки на ККС, записи, документы (CHAT-04) -------------------------------------------


def test_links_open_cards_or_say_no_access(app, admin, plant):
    visible, secret = plant["records"]
    role = admin.post("/roles", {"name": "Смена"}).json()
    rights = [
        {"scope": "section", "target": "chat", "action": "use"},
        {"scope": "section", "target": "chat_write", "action": "use"},
        {"scope": "dataset", "target": plant["datasets"][0]["id"], "action": "view"},
    ]
    assert admin.put(f"/roles/{role['id']}/permissions", {"permissions": rights}).status == 200
    shift = _user(app, admin, "sidorov", role["id"])
    missing = "00000000-0000-4000-8000-000000000000"
    body = (
        f"Сигнал 10LAC10AP001XQ01 и аппарат 10lac10ap001, см. [[record:{visible['id']}]],"
        f" [[record:{secret['id']}]], [[record:{missing}]] и [[document:{missing}]]; X25 — не код"
    )
    _post(admin, body)
    (item,) = shift.get("/chat/messages").json()["items"]
    refs = item["refs"]
    kinds = [(ref["kind"], ref["access"]) for ref in refs]
    assert kinds == [
        ("kks", True),
        ("kks", True),
        ("record", True),
        ("record", False),
        ("record", False),
        ("document", False),
    ]
    assert refs[0]["id"] == plant["kks"]["id"] and refs[0]["signal"] == "XQ01"
    assert body[refs[0]["start"] : refs[0]["end"]] == "10LAC10AP001XQ01"
    assert refs[2]["dataset"] == "Насосы"
    assert refs[3] == {
        "kind": "record",
        "id": secret["id"],
        "start": refs[3]["start"],
        "end": refs[3]["end"],
        "access": False,
    }  # ни реестра, ни содержимого


# Правка, удаление, скрытие (CHAT-05) --------------------------------------------------


def test_author_edits_and_deletes_with_history(admin, engineer):
    sent = _post(engineer, "давление 12 атм")
    edited = engineer.patch(
        f"/chat/messages/{sent['id']}", {"text": "давление 1,2 МПа", "version": 1}
    ).json()
    assert edited["version"] == 2 and edited["edited_at"] and edited["text"] == "давление 1,2 МПа"
    stale = engineer.patch(f"/chat/messages/{sent['id']}", {"text": "x", "version": 1})
    assert stale.status == 409 and stale.json()["error"]["code"] == "version_conflict"
    alien = admin.patch(f"/chat/messages/{sent['id']}", {"text": "x", "version": 2})
    assert alien.status == 403 and alien.json()["error"]["code"] == "chat_not_author"
    history = engineer.get(f"/chat/messages/{sent['id']}/history").json()["items"]
    assert [item["text"] for item in history] == ["давление 12 атм", "давление 1,2 МПа"]

    other = Client(admin.app, login="petrov", password=NEW_PASSWORD)  # тот же автор, новая сессия
    deleted = other.request("DELETE", f"/chat/messages/{sent['id']}", query={"version": 2})
    assert deleted.status == 200 and deleted.json()["state"] == "deleted"
    again = engineer.patch(f"/chat/messages/{sent['id']}", {"text": "x", "version": 3})
    assert again.json()["error"]["code"] == "chat_message_closed"
    seen = {item["id"]: item for item in admin.get("/chat/messages").json()["items"]}
    assert seen[sent["id"]]["text"] == "давление 1,2 МПа"  # модератор видит текст
    peer = _user(admin.app, admin, "ivanova", _roles(admin)["Наблюдатель"])
    (shown,) = peer.get("/chat/messages").json()["items"]
    assert shown["state"] == "deleted" and shown["text"] is None and shown["refs"] == []
    assert peer.get(f"/chat/messages/{sent['id']}/history").status == 403


def test_moderator_hides_and_restores_and_journal_keeps_track(admin, engineer):
    sent = _post(engineer, "лишнее")
    refused = engineer.post(f"/chat/messages/{sent['id']}/hide", {})
    assert refused.status == 403  # у инженера нет модерирования
    hidden = admin.post(f"/chat/messages/{sent['id']}/hide", {"reason": "не по делу"}).json()
    assert hidden["state"] == "hidden" and hidden["hidden"]["reason"] == "не по делу"
    (mine,) = engineer.get("/chat/messages").json()["items"]
    assert mine["state"] == "hidden" and mine["text"] is None
    assert mine["hidden"]["by"] == "Тестовый администратор"
    assert engineer.get("/chat/messages").json()["unread"] == 0
    back = admin.post(f"/chat/messages/{sent['id']}/unhide").json()
    assert back["state"] == "active" and "hidden" not in back
    actions = [item["action"] for item in admin.get("/audit", entity_id=sent["id"]).json()["items"]]
    assert actions == ["chat.unhide", "chat.hide", "chat.post"]
    journal = admin.get("/audit", entity_id=sent["id"]).json()["items"][-1]
    assert "лишнее" not in str(journal["details"])  # текст в журнал не пишется


def test_search_finds_words_and_hides_deleted_from_others(admin, engineer):
    _post(admin, "Задвижка 10LAC10AA101 не открывается")
    gone = _post(engineer, "задвижка заела")
    engineer.request("DELETE", f"/chat/messages/{gone['id']}", query={"version": 1})
    assert [
        item["text"] for item in engineer.get("/chat/search", q="ЗАДВИЖКА").json()["items"]
    ] == ["Задвижка 10LAC10AA101 не открывается"]
    assert len(admin.get("/chat/search", q="задвижка").json()["items"]) == 2  # модератор
    assert len(engineer.get("/chat/search", q="не откр").json()["items"]) == 1  # короткое слово
    assert engineer.get("/chat/search", q=" ").json()["error"]["code"] == "chat_query_empty"


# Права (CHAT-06) --------------------------------------------------------------------


def test_start_roles_read_and_write_new_roles_need_rights(app, admin, engineer):
    sections = engineer.get("/auth/me").json()["permissions"]["sections"]
    assert {"chat", "chat_write"} <= set(sections) and "chat_moderate" not in sections
    assert "chat_moderate" in admin.get("/auth/me").json()["permissions"]["sections"]
    catalog = admin.get("/auth/permissions").json()["sections"]
    assert {"chat", "chat_write", "chat_moderate"} <= set(catalog)  # матрица прав их покажет
    role = admin.post("/roles", {"name": "Без чата"}).json()
    stranger = _user(app, admin, "guest", role["id"])
    assert stranger.get("/chat/messages").status == 403
    reader_rights = [{"scope": "section", "target": "chat", "action": "use"}]
    admin.put(f"/roles/{role['id']}/permissions", {"permissions": reader_rights})
    assert stranger.get("/chat/messages").status == 200
    refused = stranger.post("/chat/messages", {"text": "можно?"})
    assert refused.status == 403 and refused.json()["error"]["details"]["permission"] == (
        "section.chat_write"
    )


# События (API-09, CHAT-02, AT-44) ---------------------------------------------------


def test_events_follow_rights(app, admin, plant):
    role = admin.post("/roles", {"name": "Смена"}).json()
    rights = [
        {"scope": "section", "target": "chat", "action": "use"},
        {"scope": "dataset", "target": plant["datasets"][0]["id"], "action": "view"},
    ]
    admin.put(f"/roles/{role['id']}/permissions", {"permissions": rights})
    shift = _user(app, admin, "sidorov", role["id"])
    start = shift.get("/events").json()
    assert start["events"] == [] and start["cursor"] > 0
    for dataset in plant["datasets"]:
        admin.post(f"/datasets/{dataset['id']}/records", {"values": {"name": "новый"}})
    admin.post("/users", {"login": "kozlov", "name": "Козлов", "role_id": role["id"]})
    message = _post(admin, "смена приняла")
    seen = shift.get("/events", after=start["cursor"]).json()
    kinds = [(event["action"], event["dataset_id"]) for event in seen["events"]]
    assert kinds == [("record.create", plant["datasets"][0]["id"]), ("chat.post", None)]
    assert seen["events"][-1]["id"] == message["id"] and seen["cursor"] >= seen["events"][-1]["seq"]
    everything = admin.get("/events", after=start["cursor"]).json()["events"]
    assert {"user.create", "record.create", "chat.post"} <= {
        event["action"] for event in everything
    }
    assert shift.get("/events", after=seen["cursor"]).json()["events"] == []
    assert shift.get("/events", after=0, wait=30).json()["error"]["field"] == "wait"


def test_waiting_ends_on_write_and_on_timeout(app, admin, engineer):
    cursor = engineer.get("/events").json()["cursor"]
    started = time.monotonic()
    empty = engineer.get("/events", after=cursor, wait=0.3).json()
    assert empty["events"] == [] and time.monotonic() - started >= 0.3

    def later():
        time.sleep(0.3)
        _post(admin, "внимание всем")

    writer = threading.Thread(target=later)
    started = time.monotonic()
    writer.start()
    woke = engineer.get("/events", after=cursor, wait=10).json()
    elapsed = time.monotonic() - started
    writer.join()
    assert [event["action"] for event in woke["events"]] == ["chat.post"]
    assert elapsed < 1.5  # AT-44: до 1 с после записи, а не по истечении ожидания


def test_write_from_another_process_is_noticed(app, engineer):
    cursor = engineer.get("/events").json()["cursor"]
    db = app.state.db

    def outside():
        time.sleep(0.2)
        # Запись мимо сервера, как у переноса из командной строки: без сигнала.
        import sqlite3

        conn = sqlite3.connect(db.path)
        conn.execute(
            "INSERT INTO audit_log (at, actor_label, origin, action, entity_kind, entity_id)"
            " VALUES ('2026-11-18T00:00:00.000Z', ?, 'system', 'kks.create', 'kks', 'x')",
            (SYSTEM.label,),
        )
        conn.commit()
        conn.close()

    writer = threading.Thread(target=outside)
    started = time.monotonic()
    writer.start()
    woke = engineer.get("/events", after=cursor, wait=10).json()
    writer.join()
    assert [event["action"] for event in woke["events"]] == ["kks.create"]
    assert time.monotonic() - started < 2.5
