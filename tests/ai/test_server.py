"""Сервер для ИИ: делегированный доступ, диалоги, оценки, настройки, события.

ТЗ 9: API-10, AI-09, AI-10, AI-15, KNOW-05, KNOW-12, SEC-05. Данные вымышленные.
"""

from __future__ import annotations

from ai.api import Api
from ai.model import Chunk
from engine import SYSTEM
from server import ai_link

from .conftest import AsgiTransport, ask, tool, tool_results, user


def _me(client):
    return client.get("/auth/me").json()["user"]["id"]


# Делегированный доступ (API-10, AI-09, AI-10) -----------------------------------------


def test_delegated_access_reads_with_user_rights_and_never_writes(app, admin, plant):
    pumps = plant["datasets"]["Насосы"]["id"]
    shift = user(app, admin, "shift", [
        {"scope": "section", "target": "ai", "action": "use"},
        {"scope": "dataset", "target": pumps, "action": "view"},
        {"scope": "dataset", "target": pumps, "action": "edit"},
    ])  # fmt: skip
    token = app.state.grants.issue(_me(shift), "dialog", "message")
    api = Api(AsgiTransport(app), token, max_calls=100)
    schema = api.get("/ai/schema").body
    assert [item["name"] for item in schema["datasets"]] == ["Насосы"]  # только видимое
    cable = plant["rows"]["cable1"]["id"]
    assert api.get(f"/records/{cable}").status == 403  # права пользователя, не службы
    pen1 = plant["rows"]["pen1"]
    patched = api.request(
        "PATCH", f"/records/{pen1['id']}",
        body={"values": {"pressure": 1}, "expected_version": pen1["version"]},
    )  # fmt: skip
    assert patched.status == 403 and patched.error == "ai_read_only"
    asked = api.post("/ai/dialogs", {})
    assert asked.status == 403 and asked.error == "ai_read_only"
    assert api.post(f"/datasets/{pumps}/records/count", {}).body == {"count": 3}  # чтение POST
    direct = shift.post(
        "/ai/drafts",
        {"kind": "record_edit", "summary": "x",
         "payload": {"record_id": pen1["id"], "values": {"pressure": 1}}},
    )  # fmt: skip
    assert direct.status == 403 and direct.json()["error"]["code"] == "ai_draft_from_ai"


def test_grant_has_call_limit_and_ends_with_the_answer(app, admin, plant, link):
    grants = app.state.grants
    token = grants.issue(_me(admin), "dialog", "message")
    api = Api(AsgiTransport(app), token, max_calls=100)
    for _ in range(ai_link.MAX_CALLS):
        assert api.get("/sites").ok
    refused = api.get("/sites")
    assert refused.status == 429 and refused.error == "ai_call_limit"
    grants.revoke(token)
    assert api.get("/sites").error == "ai_grant_invalid"

    ask(admin, "10LAC10AP001")
    used = link.payloads[-1]["token"]
    assert used.startswith(ai_link.PREFIX) and len(grants) == 0  # отозван после ответа
    late = Api(AsgiTransport(app), used).get("/sites")
    assert late.status == 401


def test_blocked_user_loses_delegated_access(app, admin, plant):
    shift = user(app, admin, "shift", [{"scope": "section", "target": "ai", "action": "use"}])
    shift_id = _me(shift)
    token = app.state.grants.issue(shift_id, "dialog", "message")
    admin.post(f"/users/{shift_id}/block")
    assert Api(AsgiTransport(app), token).get("/sites").error == "session_invalid"


# Диалоги и оценки (KNOW-05, KNOW-12) ----------------------------------------------------


def test_dialogs_are_private_and_deletable(app, admin, plant):
    _events, answer = ask(admin, "10LAC10AP001")
    dialog = answer["dialog_id"]
    listed = admin.get("/ai/dialogs").json()["items"]
    assert [(item["id"], item["messages"]) for item in listed] == [(dialog, 2)]
    full = admin.get(f"/ai/dialogs/{dialog}").json()
    assert [item["role"] for item in full["messages"]] == ["user", "assistant"]
    assert full["messages"][1]["sources"] == answer["sources"]
    other = user(app, admin, "petrov", [{"scope": "section", "target": "ai", "action": "use"}])
    assert other.get(f"/ai/dialogs/{dialog}").status == 404
    assert other.get("/ai/dialogs").json()["items"] == []
    assert other.delete(f"/ai/dialogs/{dialog}").status == 404
    renamed = admin.patch(f"/ai/dialogs/{dialog}", {"title": "Насос 1"}).json()
    assert renamed["title"] == "Насос 1"
    assert admin.delete(f"/ai/dialogs/{dialog}").status == 204
    assert admin.get("/ai/dialogs").json()["items"] == []


def test_feedback_keeps_question_answer_and_sources(app, admin, plant):
    _, answer = ask(admin, "Сколько насосов в работе?")
    rated = admin.put(
        f"/ai/messages/{answer['id']}/feedback", {"verdict": "wrong", "correction": "Три насоса"}
    ).json()
    assert rated == {"message_id": answer["id"], "verdict": "wrong", "correction": "Три насоса"}
    shown = admin.get(f"/ai/dialogs/{answer['dialog_id']}").json()["messages"][1]
    assert shown["feedback"] == {"verdict": "wrong", "correction": "Три насоса"}
    admin.put(f"/ai/messages/{answer['id']}/feedback", {"verdict": "right"})
    admin.delete(f"/ai/dialogs/{answer['dialog_id']}")
    exported = admin.get("/ai/feedback").json()["items"]  # переживает удаление диалога
    assert len(exported) == 1 and exported[0]["verdict"] == "right"
    assert exported[0]["question"] == "Сколько насосов в работе?"
    assert exported[0]["answer"] == answer["text"] and exported[0]["sources"] == answer["sources"]
    engineer = user(app, admin, "petrov", [{"scope": "section", "target": "ai", "action": "use"}])
    assert engineer.get("/ai/feedback").status == 403


def test_old_dialogs_are_purged_by_retention(app, admin, plant):
    _, old = ask(admin, "10LAC10AP001")
    assert admin.put("/ai/settings", {"retention_days": 30}).json()["retention_days"] == 30
    with app.state.db.write(SYSTEM) as tx:
        tx.conn.execute(
            "UPDATE ai_dialogs SET updated_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (old["dialog_id"],),
        )
    ask(admin, "ПЭН-1")
    assert old["dialog_id"] not in [item["id"] for item in admin.get("/ai/dialogs").json()["items"]]
    assert admin.get(f"/ai/dialogs/{old['dialog_id']}").status == 404
    bad = admin.put("/ai/settings", {"retention_days": 0})
    assert bad.status == 400 and bad.json()["error"]["code"] == "ai_retention_invalid"


# Облачная модель (AI-15) ----------------------------------------------------------------


def test_cloud_model_needs_explicit_confirmation(app, admin, plant, model):
    assert admin.get("/ai/settings").json() == {
        "retention_days": 180, "cloud_allowed": False, "model": None, "fallback": None,
        "embedding": None,
    }  # fmt: skip
    refused = admin.put("/ai/settings", {"cloud_allowed": True})
    assert refused.status == 400 and refused.json()["error"]["code"] == "ai_cloud_confirm_required"
    model.remote = True
    _, blocked = ask(admin, "Почему насосы в резерве?")
    assert blocked["status"] == "no_model" and "облачная модель не разрешена" in blocked["text"]
    assert model.seen == []
    allowed = admin.put("/ai/settings", {"cloud_allowed": True, "confirm_cloud": True}).json()
    assert allowed["cloud_allowed"] is True
    _, answered = ask(admin, "Почему насосы в резерве?")
    assert answered["path"] == "model" and len(model.seen) == 1
    engineer = user(app, admin, "petrov", [{"scope": "section", "target": "ai", "action": "use"}])
    assert engineer.put("/ai/settings", {"cloud_allowed": False}).status == 403


# Права раздела и события ----------------------------------------------------------------


def test_ai_section_is_required(app, admin, plant):
    pumps = plant["datasets"]["Насосы"]["id"]
    reader = user(app, admin, "reader", [{"scope": "dataset", "target": pumps, "action": "view"}])
    assert reader.get("/ai/dialogs").status == 403
    assert reader.post("/ai/dialogs", {}).status == 403


def test_draft_events_reach_only_the_asker(app, admin, plant, model):
    other = user(app, admin, "petrov", [{"scope": "section", "target": "ai", "action": "use"}])
    start = admin.get("/events").json()["cursor"]
    pen1 = plant["rows"]["pen1"]
    model.script = lambda messages, tools: (
        [tool("propose", kind="record_edit", summary="Давление", record_id=pen1["id"],
              values={"pressure": 17})]
        if not tool_results(messages) else [Chunk("Черновик готов.")]
    )  # fmt: skip
    ask(admin, "Измени давление ПЭН-1 на 17")
    mine = admin.get("/events", after=start).json()["events"]
    assert "ai.draft_propose" in [item["action"] for item in mine]
    theirs = other.get("/events", after=start).json()["events"]
    assert "ai.draft_propose" not in [item["action"] for item in theirs]


def test_model_access_key_is_never_shown(app, admin, plant):
    from engine import ai_memory

    remote = {"provider": "openai", "url": "https://models.example.org", "name": "m",
              "api_key": "секрет-ключа"}  # fmt: skip
    saved = admin.put("/ai/settings", {"model": remote}).json()
    assert saved["model"]["api_key_set"] is True and "api_key" not in saved["model"]
    admin.put("/ai/settings", {"model": {**remote, "api_key": None, "think": "low"}})
    with app.state.db.read() as conn:
        stored = ai_memory.settings(conn, secrets=True)["model"]
    assert stored["api_key"] == "секрет-ключа" and stored["think"] == "low"  # ключ сохранён
    audit = admin.get("/audit", action="ai.settings").json()["items"]
    assert audit and all("секрет" not in str(item) for item in audit)
    assert admin.put("/ai/settings", {"model": None}).json()["model"] is None
    status = admin.get("/ai/status").json()
    assert status["model"]["name"] == "сценарий" and status["fallback"] is None
