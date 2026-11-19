"""Заметки знаний и словарь станции (ТЗ 8, KNOW-06…10)."""

from __future__ import annotations

from typing import Any

from engine import knowledge
from server import schemas
from server.api import Call, Endpoint

KNOWLEDGE = "section.knowledge"


def _states(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ("confirmed",)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def notes(call: Call) -> dict[str, Any]:
    """Заметки, видимые пользователю; по умолчанию — подтверждённые."""
    return knowledge.listing(
        call.conn,
        call.access,
        states=_states(call.query.get("state")),
        scope_kind=call.query.get("scope_kind") or None,
        scope_id=call.query.get("scope_id") or None,
        query=call.query.get("q") or None,
        limit=min(max(call.query_int("limit", 50), 1), 200),
    )


def note(call: Call) -> dict[str, Any]:
    """Заметка: текст, состояние, источники с версиями."""
    return knowledge.get(call.conn, call.access, call.params["id"])


def propose(call: Call) -> dict[str, Any]:
    """Предложить заметку: она вступит в силу после подтверждения."""
    body = call.body
    return knowledge.create(
        call.tx,
        call.access,
        title=body.title,
        body=body.body,
        scope=body.scope.model_dump(),
        sources=[item.model_dump() for item in body.sources],
        origin=body.origin,
    )


def edit(call: Call) -> dict[str, Any]:
    """Исправить заметку; после правки её снова нужно подтвердить."""
    body = call.body
    return knowledge.edit(
        call.tx,
        call.access,
        call.params["id"],
        version=body.version,
        title=body.title,
        body=body.body,
        sources=None if body.sources is None else [item.model_dump() for item in body.sources],
    )


def confirm(call: Call) -> dict[str, Any]:
    """Подтвердить заметку (или устаревшую — «проверено, верно»)."""
    return knowledge.confirm(call.tx, call.access, call.params["id"], call.body.version)


def reject(call: Call) -> dict[str, Any]:
    """Отклонить заметку с причиной."""
    return knowledge.reject(call.tx, call.access, call.params["id"], call.body.reason)


def review_queue(call: Call) -> dict[str, Any]:
    """Очередь проверки: предложенные и устаревшие заметки."""
    return knowledge.queue(call.conn, call.access)


def terms(call: Call) -> dict[str, Any]:
    """Словарь станции."""
    return knowledge.glossary(call.conn, call.query.get("q") or None)


def add_term(call: Call) -> dict[str, Any]:
    """Добавить термин: местное название, значение, объекты ККС."""
    body = call.body
    return knowledge.add_term(call.tx, call.access, body.term, body.meaning, body.kks_ids)


def update_term(call: Call) -> dict[str, Any]:
    """Изменить термин словаря."""
    body = call.body
    return knowledge.update_term(
        call.tx, call.access, call.params["id"], body.term, body.meaning, body.kks_ids
    )


def delete_term(call: Call) -> None:
    """Удалить термин словаря."""
    knowledge.delete_term(call.tx, call.access, call.params["id"])


def _e(method: str, path: str, handler: Any, permission: str, **extra: Any) -> Endpoint:
    return Endpoint(method, path, handler, permission, tag="Знания", **extra)


ENDPOINTS = [
    _e(
        "GET",
        "/knowledge/notes",
        notes,
        "session",
        query=("state", "scope_kind", "scope_id", "q", "limit"),
    ),
    _e("POST", "/knowledge/notes", propose, "session", body=schemas.NoteCreate, status=201),
    _e("GET", "/knowledge/notes/{id}", note, "session"),
    _e("PATCH", "/knowledge/notes/{id}", edit, "session", body=schemas.NoteEdit),
    _e("POST", "/knowledge/notes/{id}/confirm", confirm, KNOWLEDGE, body=schemas.NoteConfirm),
    _e("POST", "/knowledge/notes/{id}/reject", reject, KNOWLEDGE, body=schemas.NoteReject),
    _e("GET", "/knowledge/queue", review_queue, KNOWLEDGE),
    _e("GET", "/knowledge/glossary", terms, "session", query=("q",)),
    _e("POST", "/knowledge/glossary", add_term, KNOWLEDGE, body=schemas.GlossaryTerm, status=201),
    _e("PUT", "/knowledge/glossary/{id}", update_term, KNOWLEDGE, body=schemas.GlossaryTerm),
    _e("DELETE", "/knowledge/glossary/{id}", delete_term, KNOWLEDGE, status=204),
]
