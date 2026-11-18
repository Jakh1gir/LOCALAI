"""Общий чат пользователей (ТЗ 11.3, CHAT-01…09) и события об изменениях (API-09)."""

from __future__ import annotations

from typing import Any

from engine import EngineError, chat, events
from engine.errors import FORBIDDEN
from server import schemas
from server.api import Call, Endpoint

READ, WRITE, MODERATE = "section.chat", "section.chat_write", "section.chat_moderate"


def page(call: Call) -> dict[str, Any]:
    """Лента общего чата: последние сообщения или страница по курсору."""
    before = call.query_int("before", -1)
    after = call.query_int("after", -1)
    return chat.page(
        call.conn,
        call.access,
        before=None if before < 0 else before,
        after=None if after < 0 else after,
        limit=call.query_int("limit", chat.PAGE),
    )


def post(call: Call) -> dict[str, Any]:
    """Отправить сообщение в общий чат."""
    _reader(call)
    return chat.post(call.tx, call.access, call.body.text)


def edit(call: Call) -> dict[str, Any]:
    """Исправить своё сообщение; прежний текст остаётся в истории."""
    _reader(call)
    return chat.edit(call.tx, call.access, call.params["id"], call.body.text, call.body.version)


def delete(call: Call) -> dict[str, Any]:
    """Удалить своё сообщение: у других останется отметка «удалено»."""
    _reader(call)
    version = call.query_int("version", 0)
    return chat.delete(call.tx, call.access, call.params["id"], version)


def hide(call: Call) -> dict[str, Any]:
    """Скрыть чужое сообщение (модератор)."""
    return chat.hide(call.tx, call.access, call.params["id"], hidden=True, reason=call.body.reason)


def unhide(call: Call) -> dict[str, Any]:
    """Вернуть скрытое сообщение (модератор)."""
    return chat.hide(call.tx, call.access, call.params["id"], hidden=False)


def history(call: Call) -> dict[str, Any]:
    """Все тексты сообщения: автору и модератору."""
    return {"items": chat.history(call.conn, call.access, call.params["id"])}


def search(call: Call) -> dict[str, Any]:
    """Поиск по сообщениям общего чата."""
    return chat.search(
        call.conn, call.access, call.query.get("q", ""), limit=call.query_int("limit", chat.PAGE)
    )


def mark_read(call: Call) -> dict[str, Any]:
    """Отметить ленту прочитанной до сообщения."""
    return chat.mark_read(call.tx, call.access, call.body.seq)


def changes(call: Call) -> dict[str, Any]:
    """События об изменениях после курсора; без курсора — текущий курсор.

    С параметром ``wait`` ответ ждёт до этого числа секунд, пока не появится
    событие (до 25 с). Клиент повторяет запрос с полученным ``cursor``.
    """
    if call.query.get("after") in (None, ""):
        return {"cursor": events.cursor(call.conn), "events": [], "more": False}
    after = call.query_int("after", 0)
    if after < 0:
        raise EngineError("request_invalid", "Параметр «after»: номер от 0", field="after")
    return events.since(call.conn, call.access, after)


def _reader(call: Call) -> None:
    """Писать в чат может только тот, кто его читает."""
    if not call.access.has("chat"):
        raise EngineError(
            "forbidden",
            "Недостаточно прав: общий чат: читать",
            status=FORBIDDEN,
            details={"permission": READ},
        )


def _e(method: str, path: str, handler: Any, permission: str, **extra: Any) -> Endpoint:
    return Endpoint(method, path, handler, permission, tag="Общий чат", **extra)


ENDPOINTS = [
    _e("GET", "/chat/messages", page, READ, query=("before", "after", "limit")),
    _e("POST", "/chat/messages", post, WRITE, body=schemas.ChatPost, status=201),
    _e("PATCH", "/chat/messages/{id}", edit, WRITE, body=schemas.ChatEdit),
    _e("DELETE", "/chat/messages/{id}", delete, WRITE, query=("version",)),
    _e("POST", "/chat/messages/{id}/hide", hide, MODERATE, body=schemas.ChatHide),
    _e("POST", "/chat/messages/{id}/unhide", unhide, MODERATE),
    _e("GET", "/chat/messages/{id}/history", history, READ),
    _e("GET", "/chat/search", search, READ, query=("q", "limit")),
    _e("POST", "/chat/read", mark_read, READ, body=schemas.ChatRead, idempotent=False),
    Endpoint(
        "GET",
        "/events",
        changes,
        "session",
        query=("after", "wait"),
        tag="События",
        poll=True,
    ),
]
