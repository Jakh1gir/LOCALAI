"""ИИ: диалоги, вопросы, оценки, черновики изменений, настройки (ТЗ 9, `/api/v1/ai`).

Вопрос уходит ИИ-службе вместе с делегированным доступом от имени
спросившего (API-10, server/ai_link.py). Ответ идёт потоком строк JSON
(NDJSON, AI-11): интерфейс видит, какой инструмент выполняется, и текст по
мере появления. Последняя строка — сохранённый ответ с источниками.
Без службы ИИ ответ сразу сообщает, что служба недоступна (NFR-07).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from starlette.responses import StreamingResponse

from engine import EngineError, ai_memory, structure
from engine.access import Access
from engine.database import Database
from engine.errors import FORBIDDEN
from server import ai_link, schemas
from server.api import Call, Endpoint

AI = "section.ai"
ADMIN = "section.admin"
MAX_STEPS = 8  # шагов модели на ответ (AI-10)
NO_SERVICE = (
    "ИИ-служба сейчас недоступна. Таблицы, поиск и карточки работают; попросите"
    " администратора проверить, запущена ли служба ИИ."
)
BROKEN = "\n\nСвязь со службой ИИ прервалась, ответ неполный."
_CHOICES = 50


# Состояние и схема ------------------------------------------------------------------


def status(call: Call) -> dict[str, Any]:
    """Работает ли ИИ-служба, какая модель подключена и разрешена ли облачная модель."""
    health = call.ai.health() if call.ai is not None else None
    return {
        "service": health is not None,
        "model": (health or {}).get("model"),
        "cloud_allowed": bool(ai_memory.settings(call.conn).get("cloud_allowed")),
        "limits": {"steps": MAX_STEPS, "calls": ai_link.MAX_CALLS},
    }


def schema(call: Call) -> dict[str, Any]:
    """Реестры и поля, о которых пользователь может спрашивать ИИ (AI-02).

    Строится из схемы типов на каждый запрос: новый реестр доступен ИИ сразу.
    Реестр без права «видеть» и скрытые от роли поля сюда не попадают.
    """
    conn, viewer = call.conn, call.access
    sites = {item["id"]: item for item in structure.list_sites(conn) if not item["archived_at"]}
    loaded: dict[str, structure.TypeSchema] = {}
    datasets = []
    for item in structure.list_datasets(conn):
        if item["archived_at"] or item["site_id"] not in sites:
            continue
        if not viewer.can(item["id"], "view"):
            continue
        if item["type_id"] not in loaded:
            loaded[item["type_id"]] = structure.load_schema(conn, item["type_id"])
        kind = loaded[item["type_id"]]
        hidden = viewer.hidden_keys(kind)
        datasets.append(
            {
                "id": item["id"],
                "name": item["name"],
                "site_id": item["site_id"],
                "site": sites[item["site_id"]]["name"],
                "type": kind.name,
                "can_edit": viewer.can(item["id"], "edit"),
                "fields": [
                    _field(conn, field) for field in kind.active() if field.key not in hidden
                ],
            }
        )
    return {
        "sites": [{"id": item["id"], "name": item["name"]} for item in sites.values()],
        "datasets": datasets,
    }


def _field(conn: Any, field: structure.Field) -> dict[str, Any]:
    shown: dict[str, Any] = {
        "key": field.key,
        "name": field.name,
        "kind": field.kind,
        "multiple": field.multiple,
        "unique": field.unique_values,
    }
    if field.kks_role:
        shown["kks_role"] = field.kks_role
    if field.kind == "choice" and field.dictionary_id:
        rows = conn.execute(
            "SELECT value FROM dictionary_values WHERE dictionary_id = ? AND archived_at IS NULL"
            " ORDER BY position LIMIT ?",
            (field.dictionary_id, _CHOICES),
        ).fetchall()
        shown["choices"] = [row["value"] for row in rows]
    return shown


# Диалоги ----------------------------------------------------------------------------


def dialogs(call: Call) -> dict[str, Any]:
    """Свои диалоги с ИИ, свежие сначала (KNOW-12)."""
    return ai_memory.list_dialogs(call.conn, call.access, call.query_int("limit", 50))


def create_dialog(call: Call) -> dict[str, Any]:
    """Начать диалог."""
    return ai_memory.create_dialog(call.tx, call.access, call.body.title)


def dialog(call: Call) -> dict[str, Any]:
    """Диалог с вопросами, ответами, источниками и оценками."""
    return ai_memory.get_dialog(call.conn, call.access, call.params["id"])


def rename_dialog(call: Call) -> dict[str, Any]:
    """Переименовать диалог."""
    return ai_memory.rename_dialog(call.tx, call.access, call.params["id"], call.body.title)


def delete_dialog(call: Call) -> None:
    """Удалить свой диалог со всеми сообщениями (KNOW-12)."""
    ai_memory.delete_dialog(call.tx, call.access, call.params["id"])


def ask(call: Call) -> StreamingResponse:
    """Задать вопрос; ответ — поток NDJSON (AI-11).

    Строки потока: ``question`` (сохранённый вопрос), ``tool`` (выполняется
    инструмент), ``text`` (кусок ответа), ``draft`` (создан черновик
    изменения), ``message`` (сохранённый ответ — последняя строка).
    """
    db, viewer, dialog_id = call.db, call.access, call.params["id"]
    assert db is not None
    body = call.body
    context = body.context.model_dump(exclude_defaults=True)
    with db.write(viewer.actor) as tx:
        ai_memory.purge_old(tx)
        question = ai_memory.add_question(tx, viewer, dialog_id, body.text, context)
        previous = ai_memory.history(tx.conn, dialog_id, question["id"])
        cloud = bool(ai_memory.settings(tx.conn).get("cloud_allowed"))
    token = call.grants.issue(viewer.user_id, dialog_id, question["id"])
    payload = {
        "question": question["text"],
        "context": context,
        "history": previous,
        "token": token,
        "cloud_allowed": cloud,
        "limits": {"steps": MAX_STEPS, "calls": ai_link.MAX_CALLS},
    }
    return StreamingResponse(
        _relay(db, call.ai, call.grants, token, viewer, question, payload),
        media_type="application/x-ndjson",
    )


def _relay(
    db: Database,
    link: Any,
    grants: ai_link.Grants,
    token: str,
    viewer: Access,
    question: dict[str, Any],
    payload: dict[str, Any],
) -> Iterator[bytes]:
    started = time.perf_counter()
    yield _line({"type": "question", "message": question})
    parts: list[str] = []
    done: dict[str, Any] = {}
    stored: dict[str, Any] | None = None
    try:
        if link is None:
            raise ai_link.AiUnavailable("служба не настроена")
        for event in link.ask(payload):
            kind = event.get("type")
            if kind == "text":
                parts.append(str(event.get("delta", "")))
            if kind == "done":
                done = event
                continue
            if kind in ("text", "tool", "draft"):
                yield _line(event)
    except ai_link.AiUnavailable:
        if parts:  # ответ начался и оборвался
            done = {**done, "status": "error"}
            parts.append(BROKEN)
            yield _line({"type": "text", "delta": BROKEN})
        else:
            done = {"status": "no_service"}
            parts = [NO_SERVICE]
            yield _line({"type": "text", "delta": NO_SERVICE})
    finally:
        grants.revoke(token)
        # Ответ сохраняется и тогда, когда интерфейс закрыл поток раньше конца.
        with db.write(viewer.actor) as tx:
            stored = ai_memory.add_answer(
                tx,
                viewer,
                question["dialog_id"],
                "".join(parts).strip(),
                status=done.get("status") or "error",
                path=done.get("path"),
                steps=list(done.get("steps") or []),
                sources=list(done.get("sources") or []),
                took_ms=round((time.perf_counter() - started) * 1000, 1),
            )
    yield _line({"type": "message", "message": stored})


def _line(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


# Оценки -----------------------------------------------------------------------------


def rate(call: Call) -> dict[str, Any]:
    """Отметить ответ «верно» или «неверно» и исправить (KNOW-05)."""
    body = call.body
    return ai_memory.feedback(
        call.tx, call.access, call.params["id"], body.verdict, body.correction
    )


def feedback_export(call: Call) -> dict[str, Any]:
    """Все оценки с вопросами и ответами — основа набора проверочных вопросов (AI-13)."""
    return {"items": ai_memory.feedback_export(call.conn)}


# Черновики (AI-09) ------------------------------------------------------------------


def propose(call: Call) -> dict[str, Any]:
    """Черновик изменения от ИИ. Создаёт только ИИ-служба по делегированному доступу."""
    if call.grant is None:
        raise EngineError(
            "ai_draft_from_ai",
            "Черновики создаёт ИИ; изменить запись можно напрямую",
            status=FORBIDDEN,
        )
    body = call.body
    return ai_memory.propose(
        call.tx,
        call.access,
        kind=body.kind,
        summary=body.summary,
        payload=body.payload,
        message_id=call.grant.message_id,
    )


def drafts(call: Call) -> dict[str, Any]:
    """Свои черновики изменений; фильтр по состоянию."""
    return ai_memory.list_drafts(
        call.conn, call.access, call.query.get("state") or None, call.query_int("limit", 50)
    )


def draft(call: Call) -> dict[str, Any]:
    """Черновик изменения."""
    return ai_memory.get_draft(call.conn, call.access, call.params["id"])


def accept(call: Call) -> dict[str, Any]:
    """Принять черновик: изменение выполняется от имени принявшего (AT-33)."""
    return ai_memory.accept(call.tx, call.access, call.params["id"])


def reject(call: Call) -> dict[str, Any]:
    """Отклонить черновик."""
    return ai_memory.reject(call.tx, call.access, call.params["id"], call.body.reason)


# Настройки --------------------------------------------------------------------------


def get_settings(call: Call) -> dict[str, Any]:
    """Срок хранения диалогов и разрешение облачной модели."""
    return ai_memory.settings(call.conn)


def put_settings(call: Call) -> dict[str, Any]:
    """Изменить настройки ИИ; облачная модель — только с подтверждением (AI-15)."""
    body = call.body
    return ai_memory.set_settings(
        call.tx,
        retention_days=body.retention_days,
        cloud_allowed=body.cloud_allowed,
        confirm_cloud=body.confirm_cloud,
    )


def _e(method: str, path: str, handler: Any, permission: str, **extra: Any) -> Endpoint:
    return Endpoint(method, path, handler, permission, tag="ИИ", **extra)


ENDPOINTS = [
    _e("GET", "/ai/status", status, AI),
    _e("GET", "/ai/schema", schema, AI),
    _e("GET", "/ai/dialogs", dialogs, AI, query=("limit",)),
    _e("POST", "/ai/dialogs", create_dialog, AI, body=schemas.AiDialogCreate, status=201),
    _e("GET", "/ai/dialogs/{id}", dialog, AI),
    _e("PATCH", "/ai/dialogs/{id}", rename_dialog, AI, body=schemas.AiDialogRename),
    _e("DELETE", "/ai/dialogs/{id}", delete_dialog, AI, status=204),
    _e(
        "POST",
        "/ai/dialogs/{id}/messages",
        ask,
        AI,
        body=schemas.AiAsk,
        manual=True,
        idempotent=False,
    ),
    _e("PUT", "/ai/messages/{id}/feedback", rate, AI, body=schemas.AiFeedback),
    _e("GET", "/ai/feedback", feedback_export, ADMIN),
    _e("GET", "/ai/drafts", drafts, AI, query=("state", "limit")),
    _e(
        "POST",
        "/ai/drafts",
        propose,
        AI,
        body=schemas.AiDraftCreate,
        status=201,
        delegate=True,
    ),
    _e("GET", "/ai/drafts/{id}", draft, AI),
    _e("POST", "/ai/drafts/{id}/accept", accept, AI),
    _e("POST", "/ai/drafts/{id}/reject", reject, AI, body=schemas.AiDraftReject),
    _e("GET", "/ai/settings", get_settings, ADMIN),
    _e("PUT", "/ai/settings", put_settings, ADMIN, body=schemas.AiSettings),
]
