"""События об изменениях для открытых экранов (API-09, UI-06, CHAT-02).

Источник событий — журнал аудита: он пишется в той же транзакции, что и
само изменение, поэтому событие не теряется и не опережает данные. Номер
строки журнала — курсор: после потери связи клиент просит события после
последнего известного номера и догружает пропущенное.

Событие говорит только, что и где изменилось; сами данные клиент читает
обычными маршрутами со своими правами. Событие видно, если читающему виден
предмет изменения:

- запись и реестр — право «видеть» на реестр;
- сообщение чата — раздел «Общий чат»;
- документ (загрузка, чтение, замена, привязки) — право «видеть» на его тип;
- заметка знаний — тому, кто видит все её источники (SEC-06);
- черновик изменения от ИИ — только тому, кто задал вопрос;
- ККС и структура (поля, типы, справочники) — каждому вошедшему: без них
  экран не перестроится;
- остальное (пользователи, роли, сети, копии, перенос) — администратору.

Журнал, перенесённый из Local_DB, событий не даёт: это прошлое.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from engine import ai_memory, knowledge
from engine.access import Access

SCAN = 500  # строк журнала за один запрос: курсор двигается и по невидимым
MAX_EVENTS = 200
_SHARED = frozenset({"kks", "kks_rules", "site", "type", "field", "dictionary", "dictionary_value"})
_SKIPPED_ORIGINS = ("localdb",)


def cursor(conn: sqlite3.Connection) -> int:
    """Текущий номер журнала: с него клиент начинает слушать."""
    return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM audit_log").fetchone()[0]


def since(conn: sqlite3.Connection, viewer: Access, after: int) -> dict[str, Any]:
    """События после ``after``, видимые ``viewer``. ``cursor`` — с чего продолжать."""
    rows = conn.execute(
        "SELECT seq, at, actor_label, origin, action, entity_kind, entity_id, dataset_id"
        " FROM audit_log WHERE seq > ? ORDER BY seq LIMIT ?",
        (after, SCAN),
    ).fetchall()
    events: list[dict[str, Any]] = []
    last = after
    for row in rows:
        if len(events) >= MAX_EVENTS:
            break
        last = row["seq"]
        if row["origin"] in _SKIPPED_ORIGINS or not _visible(conn, row, viewer):
            continue
        events.append(
            {
                "seq": row["seq"],
                "at": row["at"],
                "action": row["action"],
                "kind": row["entity_kind"],
                "id": row["entity_id"],
                "dataset_id": row["dataset_id"],
                "actor": row["actor_label"],
            }
        )
    more = len(rows) == SCAN or len(events) >= MAX_EVENTS and last < rows[-1]["seq"]
    return {"cursor": last, "events": events, "more": bool(more)}


def _visible(conn: sqlite3.Connection, row: sqlite3.Row, viewer: Access) -> bool:
    kind = row["entity_kind"]
    if kind == "record":
        return row["dataset_id"] is not None and viewer.can(row["dataset_id"], "view")
    if kind == "dataset":
        dataset_id = row["dataset_id"] or row["entity_id"]
        return dataset_id is not None and viewer.can(dataset_id, "view")
    if kind == "chat_message":
        return viewer.has("chat")
    if kind == "knowledge_note":
        found = conn.execute(
            "SELECT seq FROM knowledge_notes WHERE id = ?", (row["entity_id"],)
        ).fetchone()
        return found is not None and knowledge.visible(conn, viewer, found["seq"])
    if kind == "document":
        found = conn.execute(
            "SELECT type_id FROM documents WHERE id = ?", (row["entity_id"],)
        ).fetchone()
        return found is not None and viewer.sees_documents(found["type_id"])
    if kind == "ai_draft":  # черновик ИИ — только тому, кто задал вопрос
        return ai_memory.draft_owner(conn, row["entity_id"]) == viewer.user_id
    if kind in _SHARED:
        return True
    return viewer.has("admin")
