"""Память ИИ: диалоги, ответы с источниками, оценки, черновики изменений.

ТЗ 9 и 8: AI-07 (источники ответа), AI-09 (ИИ только читает, изменение —
черновик, который принимает человек), KNOW-05 (оценка ответа), KNOW-12 (срок
хранения диалогов задаёт администратор; пользователь видит и удаляет свои).

Диалог принадлежит пользователю: чужой диалог, сообщение и черновик для
остальных не существуют (404), в том числе для администратора — это
переписка человека с ИИ, а не журнал.

Черновик правки записи хранит версию записи на момент предложения. Принять
его — то же, что правка записи человеком: те же права на реестр и поля, та
же проверка версии; автор изменения в журнале — принявший человек (AT-33).
Если запись успела измениться, черновик не применяется (409): предложение
было сделано по другим данным.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from engine import access, knowledge, records, structure, text
from engine.access import Access
from engine.database import Tx, dumps
from engine.errors import CONFLICT, FORBIDDEN, EngineError, not_found

MAX_QUESTION = 4000
MAX_ANSWER = 20000
MAX_TITLE = 200
DEFAULT_TITLE = "Новый диалог"
HISTORY = 6  # реплик прошлого диалога уходит модели
PAGE = 50
STATUSES = ("done", "limit", "no_model", "no_service", "error")
PATHS = ("fast", "model")
DRAFT_KINDS = ("record_edit", "note")
DRAFT_STATES = ("proposed", "accepted", "rejected")
MAX_RETENTION = 3650


# Настройки (KNOW-12, AI-15) --------------------------------------------------------


DEFAULTS: dict[str, Any] = {
    "retention_days": 180,
    "cloud_allowed": False,
    # Основная и запасная модель (AI-01): None — модель, с которой запущена служба ИИ.
    "model": None,
    "fallback": None,
}


def settings(conn: sqlite3.Connection, *, secrets: bool = False) -> dict[str, Any]:
    """Настройки ИИ. Ключ доступа к модели наружу не отдаётся: только признак, что он задан."""
    rows = conn.execute("SELECT key, value FROM ai_settings").fetchall()
    shown = DEFAULTS | {row["key"]: json.loads(row["value"]) for row in rows}
    if not secrets:
        for key in ("model", "fallback"):
            if isinstance(shown[key], dict):
                api_key = shown[key].get("api_key")
                shown[key] = {
                    item: value for item, value in shown[key].items() if item != "api_key"
                }
                shown[key]["api_key_set"] = bool(api_key)
    return shown


def set_settings(
    tx: Tx,
    *,
    retention_days: Any = None,
    cloud_allowed: Any = None,
    confirm_cloud: bool = False,
    models: dict[str, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Облачная модель включается только с явным согласием: данные уйдут из сети (AI-15).

    ``models`` — ``{"model": …, "fallback": …}``; настройку модели проверяет
    сервер (``ai.model.Config``), здесь она сохраняется как есть; ``None`` —
    модель запуска службы.
    """
    changes: dict[str, Any] = {}
    for key, value in (models or {}).items():
        if key in ("model", "fallback"):
            changes[key] = value
    if retention_days is not None:
        if (
            not isinstance(retention_days, int)
            or isinstance(retention_days, bool)
            or not 1 <= retention_days <= MAX_RETENTION
        ):
            raise EngineError(
                "ai_retention_invalid",
                f"Срок хранения диалогов — от 1 до {MAX_RETENTION} дней",
                field="retention_days",
            )
        changes["retention_days"] = retention_days
    if cloud_allowed is not None:
        if cloud_allowed and not confirm_cloud:
            raise EngineError(
                "ai_cloud_confirm_required",
                "Облачная модель получает вопросы и найденные данные: они уходят за пределы"
                " сети станции. Подтвердите включение",
                field="confirm_cloud",
            )
        changes["cloud_allowed"] = bool(cloud_allowed)
    for key, value in changes.items():
        tx.conn.execute(
            "INSERT INTO ai_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
    if changes:
        shown = {key: value for key, value in changes.items() if key not in ("model", "fallback")}
        for key in ("model", "fallback"):
            if key in changes:  # в журнал — без ключа доступа
                value = changes[key]
                shown[key] = {k: v for k, v in value.items() if k != "api_key"} if value else None
        tx.audit("ai.settings", "ai_settings", None, details=shown)
    return settings(tx.conn)


def purge_old(tx: Tx) -> int:
    """Диалоги старше срока хранения удаляются вместе с сообщениями (KNOW-12)."""
    days = int(settings(tx.conn).get("retention_days", 180))
    edge = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return tx.conn.execute("DELETE FROM ai_dialogs WHERE updated_at < ?", (edge,)).rowcount


# Диалоги ----------------------------------------------------------------------------


def create_dialog(tx: Tx, viewer: Access, title: Any = None) -> dict[str, Any]:
    shown = _title(title) if title not in (None, "") else DEFAULT_TITLE
    dialog_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO ai_dialogs (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (dialog_id, viewer.user_id, shown, tx.now, tx.now),
    )
    return get_dialog(tx.conn, viewer, dialog_id)


def list_dialogs(conn: sqlite3.Connection, viewer: Access, limit: int = PAGE) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM ai_messages m WHERE m.dialog_seq = d.seq) AS messages"
        " FROM ai_dialogs d WHERE d.user_id = ? ORDER BY d.updated_at DESC, d.seq DESC LIMIT ?",
        (viewer.user_id, max(1, min(limit, 200))),
    ).fetchall()
    return {"items": [_dialog(row) | {"messages": row["messages"]} for row in rows]}


def get_dialog(conn: sqlite3.Connection, viewer: Access, dialog_id: str) -> dict[str, Any]:
    row = _own_dialog(conn, viewer, dialog_id)
    messages = conn.execute(
        "SELECT m.*, f.verdict, f.correction FROM ai_messages m"
        " LEFT JOIN ai_feedback f ON f.message_id = m.id"
        " WHERE m.dialog_seq = ? ORDER BY m.seq",
        (row["seq"],),
    ).fetchall()
    return _dialog(row) | {"messages": [_message(item, row["id"]) for item in messages]}


def rename_dialog(tx: Tx, viewer: Access, dialog_id: str, title: Any) -> dict[str, Any]:
    row = _own_dialog(tx.conn, viewer, dialog_id)
    tx.conn.execute(
        "UPDATE ai_dialogs SET title = ?, updated_at = ? WHERE seq = ?",
        (_title(title), tx.now, row["seq"]),
    )
    return get_dialog(tx.conn, viewer, dialog_id)


def delete_dialog(tx: Tx, viewer: Access, dialog_id: str) -> None:
    row = _own_dialog(tx.conn, viewer, dialog_id)
    tx.conn.execute("DELETE FROM ai_dialogs WHERE seq = ?", (row["seq"],))


# Сообщения --------------------------------------------------------------------------


def add_question(
    tx: Tx, viewer: Access, dialog_id: str, question: Any, context: dict[str, Any] | None
) -> dict[str, Any]:
    """Вопрос пользователя. Первый вопрос даёт название диалогу без названия."""
    row = _own_dialog(tx.conn, viewer, dialog_id)
    shown = _question(question)
    message = _insert(tx, row["seq"], "user", shown, "done", None, context=context or {})
    title = row["title"]
    if title == DEFAULT_TITLE:
        title = _title(shown if len(shown) <= 80 else shown[:79] + "…")
    tx.conn.execute(
        "UPDATE ai_dialogs SET title = ?, updated_at = ? WHERE seq = ?",
        (title, tx.now, row["seq"]),
    )
    return _message(message, dialog_id)


def history(conn: sqlite3.Connection, dialog_id: str, before: str) -> list[dict[str, str]]:
    """Последние реплики диалога до вопроса ``before`` — контекст для модели."""
    rows = conn.execute(
        "SELECT m.role, m.text FROM ai_messages m JOIN ai_dialogs d ON d.seq = m.dialog_seq"
        " WHERE d.id = ? AND m.seq < (SELECT seq FROM ai_messages WHERE id = ?)"
        " AND m.status IN ('done', 'limit') ORDER BY m.seq DESC LIMIT ?",
        (dialog_id, before, HISTORY),
    ).fetchall()
    return [{"role": row["role"], "text": row["text"]} for row in reversed(rows)]


def add_answer(
    tx: Tx,
    viewer: Access,
    dialog_id: str,
    answer: Any,
    *,
    status: str,
    path: str | None,
    steps: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    took_ms: float | None,
    model: str | None = None,
) -> dict[str, Any]:
    """Ответ ИИ. Диалог мог быть удалён, пока шёл ответ: тогда ответ не сохраняется."""
    row = tx.conn.execute(
        "SELECT * FROM ai_dialogs WHERE id = ? AND user_id = ?", (dialog_id, viewer.user_id)
    ).fetchone()
    shown = str(answer or "")[:MAX_ANSWER]
    if status not in STATUSES:
        status = "error"
    if path not in PATHS:
        path = None
    if row is None:
        return {"id": None, "dialog_id": dialog_id, "role": "assistant", "text": shown,
                "status": status, "path": path, "model": model, "steps": steps, "sources": sources,
                "took_ms": took_ms, "created_at": tx.now, "feedback": None}  # fmt: skip
    message = _insert(
        tx, row["seq"], "assistant", shown, status, path,
        steps=steps, sources=sources, took_ms=took_ms, model=model,
    )  # fmt: skip
    tx.conn.execute("UPDATE ai_dialogs SET updated_at = ? WHERE seq = ?", (tx.now, row["seq"]))
    return _message(message, dialog_id)


def feedback(
    tx: Tx, viewer: Access, message_id: str, verdict: Any, correction: Any = None
) -> dict[str, Any]:
    """Оценка «верно» или «неверно» с исправлением; хранится с вопросом и ответом (KNOW-05)."""
    if verdict not in ("right", "wrong"):
        raise EngineError("ai_verdict_invalid", "Оценка — right или wrong", field="verdict")
    if correction is not None and (not isinstance(correction, str) or len(correction) > 4000):
        raise EngineError(
            "ai_correction_invalid", "Исправление — текст до 4000 знаков", field="correction"
        )
    message = tx.conn.execute(
        "SELECT m.*, d.user_id, d.id AS dialog_id FROM ai_messages m"
        " JOIN ai_dialogs d ON d.seq = m.dialog_seq WHERE m.id = ?",
        (message_id,),
    ).fetchone()
    if message is None or message["user_id"] != viewer.user_id or message["role"] != "assistant":
        raise not_found("ai_message", message_id)
    question = tx.conn.execute(
        "SELECT text FROM ai_messages WHERE dialog_seq = ? AND seq < ? AND role = 'user'"
        " ORDER BY seq DESC LIMIT 1",
        (message["dialog_seq"], message["seq"]),
    ).fetchone()
    tx.conn.execute(
        "INSERT INTO ai_feedback (message_id, user_id, verdict, correction, question, answer,"
        " sources, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (message_id) DO UPDATE SET verdict = excluded.verdict,"
        " correction = excluded.correction, updated_at = excluded.updated_at",
        (
            message_id, viewer.user_id, verdict, (correction or "").strip() or None,
            question["text"] if question else "", message["text"], message["sources"],
            tx.now, tx.now,
        ),
    )  # fmt: skip
    return {"message_id": message_id, "verdict": verdict, "correction": correction or None}


def feedback_export(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Оценённые ответы — заготовка набора проверочных вопросов (AI-13)."""
    rows = conn.execute("SELECT * FROM ai_feedback ORDER BY created_at").fetchall()
    return [
        {
            "question": row["question"],
            "answer": row["answer"],
            "verdict": row["verdict"],
            "correction": row["correction"],
            "sources": json.loads(row["sources"]),
            "at": row["updated_at"],
        }
        for row in rows
    ]


# Черновики изменений (AI-09) --------------------------------------------------------


def propose(
    tx: Tx,
    viewer: Access,
    *,
    kind: Any,
    summary: Any,
    payload: Any,
    message_id: str | None = None,
) -> dict[str, Any]:
    if kind not in DRAFT_KINDS:
        raise EngineError(
            "ai_draft_kind",
            "Черновик: правка записи (record_edit) или заметка (note)",
            field="kind",
        )
    if not isinstance(payload, dict):
        raise EngineError("ai_draft_invalid", "Содержимое черновика — объект", field="payload")
    shown = text.clean_name(summary, "Пояснение", limit=500)
    if kind == "record_edit":
        target_id, version, stored = _check_record_edit(tx.conn, viewer, payload)
    else:
        target_id, version, stored = None, None, _check_note(tx.conn, viewer, payload)
    draft_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO ai_drafts (id, kind, state, user_id, message_id, target_id, target_version,"
        " summary, payload, created_at) VALUES (?,?, 'proposed', ?,?,?,?,?,?,?)",
        (draft_id, kind, viewer.user_id, message_id, target_id, version, shown,
         dumps(stored), tx.now),
    )  # fmt: skip
    tx.audit("ai.draft_propose", "ai_draft", draft_id, details={"kind": kind})
    return get_draft(tx.conn, viewer, draft_id)


def list_drafts(
    conn: sqlite3.Connection, viewer: Access, state: str | None = None, limit: int = PAGE
) -> dict[str, Any]:
    if state is not None and state not in DRAFT_STATES:
        raise EngineError(
            "ai_draft_state", "Состояние: proposed, accepted, rejected", field="state"
        )
    where, params = ["user_id = ?"], [viewer.user_id]
    if state:
        where.append("state = ?")
        params.append(state)
    rows = conn.execute(
        f"SELECT * FROM ai_drafts WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
        (*params, max(1, min(limit, 200))),
    ).fetchall()
    return {"items": [_draft(row) for row in rows]}


def get_draft(conn: sqlite3.Connection, viewer: Access, draft_id: str) -> dict[str, Any]:
    return _draft(_own_draft(conn, viewer, draft_id))


def accept(tx: Tx, viewer: Access, draft_id: str) -> dict[str, Any]:
    """Человек принимает черновик: изменение выполняется от его имени и с его правами."""
    row = _open_draft(tx.conn, viewer, draft_id)
    payload = json.loads(row["payload"])
    if row["kind"] == "record_edit":
        record = records.get(tx.conn, row["target_id"])
        _record_rights(tx.conn, viewer, record, payload["values"])
        records.update(tx, row["target_id"], payload["values"], row["target_version"])
        result_id = row["target_id"]
    else:
        note = knowledge.create(
            tx, viewer, title=payload["title"], body=payload["body"],
            scope=payload["scope"], sources=payload["sources"], origin="ai",
        )  # fmt: skip
        result_id = note["id"]
    tx.conn.execute(
        "UPDATE ai_drafts SET state = 'accepted', result_id = ?, decided_at = ? WHERE seq = ?",
        (result_id, tx.now, row["seq"]),
    )
    tx.audit("ai.draft_accept", "ai_draft", draft_id, details={"kind": row["kind"]})
    return get_draft(tx.conn, viewer, draft_id)


def reject(tx: Tx, viewer: Access, draft_id: str, reason: Any = None) -> dict[str, Any]:
    row = _open_draft(tx.conn, viewer, draft_id)
    shown = None
    if reason not in (None, ""):
        shown = text.clean_name(reason, "Причина", limit=500)
    tx.conn.execute(
        "UPDATE ai_drafts SET state = 'rejected', reason = ?, decided_at = ? WHERE seq = ?",
        (shown, tx.now, row["seq"]),
    )
    tx.audit("ai.draft_reject", "ai_draft", draft_id, details={"kind": row["kind"]})
    return get_draft(tx.conn, viewer, draft_id)


def draft_owner(conn: sqlite3.Connection, draft_id: str) -> str | None:
    row = conn.execute("SELECT user_id FROM ai_drafts WHERE id = ?", (draft_id,)).fetchone()
    return row["user_id"] if row else None


# Внутреннее -------------------------------------------------------------------------


def _check_record_edit(
    conn: sqlite3.Connection, viewer: Access, payload: dict[str, Any]
) -> tuple[str, int, dict[str, Any]]:
    record_id, values = payload.get("record_id"), payload.get("values")
    if not text.is_uuid(record_id):
        raise EngineError("ai_draft_invalid", "Нужен UUID записи", field="payload.record_id")
    if not isinstance(values, dict) or not values or len(values) > 50:
        raise EngineError(
            "ai_draft_invalid", "Нужны новые значения полей (до 50)", field="payload.values"
        )
    try:
        record = records.get(conn, record_id)
    except EngineError:
        raise not_found("record", record_id) from None
    if not viewer.can(record["dataset_id"], "view"):
        raise not_found("record", record_id)  # невидимая запись для ИИ не существует
    schema = _record_rights(conn, viewer, record, values)
    unknown = sorted(key for key in values if schema.by_key(key) is None)
    if unknown:
        raise EngineError("ai_draft_invalid", f"Нет поля «{unknown[0]}»", field="payload.values")
    before = {key: record["doc"].get(key) for key in values}
    stored = {"record_id": record_id, "dataset_id": record["dataset_id"],
              "values": values, "before": before}  # fmt: skip
    return record_id, record["version"], stored


def _record_rights(
    conn: sqlite3.Connection, viewer: Access, record: dict[str, Any], values: dict[str, Any]
) -> structure.TypeSchema:
    if not viewer.can(record["dataset_id"], "edit"):
        raise EngineError(
            "forbidden",
            f"Недостаточно прав: {access.DATASET_ACTIONS['edit'].lower()}",
            status=FORBIDDEN,
            details={"permission": "dataset.edit"},
        )
    dataset = structure.get_dataset(conn, record["dataset_id"])
    schema = structure.load_schema(conn, dataset["type_id"])
    locked = sorted(set(values) & viewer.locked_keys(schema))
    if locked:
        raise EngineError(
            "field_forbidden", "Недостаточно прав на изменение поля", status=FORBIDDEN,
            field=locked[0],
        )  # fmt: skip
    return schema


def _check_note(
    conn: sqlite3.Connection, viewer: Access, payload: dict[str, Any]
) -> dict[str, Any]:
    scope = payload.get("scope") or {"kind": "station"}
    sources = payload.get("sources") or []
    if not isinstance(sources, list) or not isinstance(scope, dict):
        raise EngineError("ai_draft_invalid", "Заметка: scope — объект, sources — список")
    # Проверка тем же кодом, что и при создании заметки, но без записи.
    knowledge.check(conn, viewer, title=payload.get("title"), body=payload.get("body"),
                    scope=scope, sources=sources)  # fmt: skip
    return {"title": payload.get("title"), "body": payload.get("body"),
            "scope": scope, "sources": sources}  # fmt: skip


def _own_dialog(conn: sqlite3.Connection, viewer: Access, dialog_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM ai_dialogs WHERE id = ?", (dialog_id,)).fetchone()
    if row is None or row["user_id"] != viewer.user_id:
        raise not_found("ai_dialog", dialog_id)
    return row


def _own_draft(conn: sqlite3.Connection, viewer: Access, draft_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM ai_drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None or row["user_id"] != viewer.user_id:
        raise not_found("ai_draft", draft_id)
    return row


def _open_draft(conn: sqlite3.Connection, viewer: Access, draft_id: str) -> sqlite3.Row:
    row = _own_draft(conn, viewer, draft_id)
    if row["state"] != "proposed":
        raise EngineError(
            "ai_draft_decided",
            "Черновик уже принят или отклонён",
            status=CONFLICT,
            details={"state": row["state"]},
        )
    return row


def _insert(
    tx: Tx,
    dialog_seq: int,
    role: str,
    shown: str,
    status: str,
    path: str | None,
    *,
    context: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
    took_ms: float | None = None,
    model: str | None = None,
) -> sqlite3.Row:
    message_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO ai_messages (id, dialog_seq, role, text, status, path, model, context,"
        " steps, sources, took_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (message_id, dialog_seq, role, shown, status, path, (model or None) and str(model)[:200],
         dumps(context or {}), dumps(steps or []), dumps(sources or []), took_ms, tx.now),
    )  # fmt: skip
    return tx.conn.execute(
        "SELECT m.*, NULL AS verdict, NULL AS correction FROM ai_messages m WHERE id = ?",
        (message_id,),
    ).fetchone()


def _dialog(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _message(row: sqlite3.Row, dialog_id: str) -> dict[str, Any]:
    verdict = row["verdict"]
    return {
        "id": row["id"],
        "dialog_id": dialog_id,
        "role": row["role"],
        "text": row["text"],
        "status": row["status"],
        "path": row["path"],
        "model": row["model"],
        "context": json.loads(row["context"]),
        "steps": json.loads(row["steps"]),
        "sources": json.loads(row["sources"]),
        "took_ms": row["took_ms"],
        "created_at": row["created_at"],
        "feedback": {"verdict": verdict, "correction": row["correction"]} if verdict else None,
    }


def _draft(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "state": row["state"],
        "summary": row["summary"],
        "payload": json.loads(row["payload"]),
        "target_id": row["target_id"],
        "target_version": row["target_version"],
        "message_id": row["message_id"],
        "result_id": row["result_id"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def _title(value: Any) -> str:
    return text.clean_name(value, "Название диалога", limit=MAX_TITLE)


def _question(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EngineError("ai_question_empty", "Задайте вопрос", field="text")
    if len(value) > MAX_QUESTION:
        raise EngineError(
            "ai_question_too_long", f"Вопрос длиннее {MAX_QUESTION} знаков", field="text"
        )
    return value.strip()
