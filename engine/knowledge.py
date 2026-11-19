"""Заметки знаний и словарь станции (ТЗ 8, KNOW-06…10, SEC-06).

Заметка: предложена → подтверждена → (устарела | отклонена). ИИ опирается
только на подтверждённые (KNOW-06). Предложить заметку может любой
вошедший, в том числе ИИ от имени пользователя (``origin: "ai"``);
подтверждать, отклонять и вести словарь — раздел «Знания» (KNOW-09).

Источники хранятся с версией на момент подтверждения (KNOW-07). Изменение
источника делает заметку устаревшей — это делают триггеры миграции 0011.
Подтверждение устаревшей заметки означает «проверено, верно»: версии
источников обновляются до текущих.

Заметка видна только тому, кто видит все её источники (SEC-06): запись — по
праву на реестр, документ — по праву на тип; объекты ККС видны всем.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from engine import fulltext, records, text
from engine.access import Access
from engine.database import Tx, dumps
from engine.errors import CONFLICT, FORBIDDEN, EngineError, not_found

STATES = ("proposed", "confirmed", "stale", "rejected")
SCOPES = ("kks", "type", "station")
SOURCES = ("record", "document", "kks")
MAX_SOURCES = 20
_SELECT = (
    "SELECT n.*, a.name AS author_name, c.name AS confirmed_by_name FROM knowledge_notes n"
    " LEFT JOIN users a ON a.id = n.author_id LEFT JOIN users c ON c.id = n.confirmed_by"
)


# Заметки: запись -------------------------------------------------------------------


def create(
    tx: Tx,
    viewer: Access,
    *,
    title: Any,
    body: Any,
    scope: dict[str, Any],
    sources: list[dict[str, Any]],
    origin: str = "user",
) -> dict[str, Any]:
    kind, target = _scope(tx.conn, scope)
    found = _sources(tx.conn, viewer, sources)
    note_id = text.new_id()
    seq = tx.conn.execute(
        "INSERT INTO knowledge_notes (id, scope_kind, scope_id, title, body, state, origin,"
        " author_id, created_at, updated_at) VALUES (?,?,?,?,?, 'proposed', ?,?,?,?)",
        (note_id, kind, target, _title(title), _body(body), origin, viewer.user_id, tx.now, tx.now),
    ).lastrowid
    assert seq is not None
    _store_sources(tx, seq, found)
    _index(tx.conn, seq, _title(title), _body(body))
    tx.audit("note.propose", "knowledge_note", note_id, details={"origin": origin})
    return get(tx.conn, viewer, note_id)


def check(
    conn: sqlite3.Connection,
    viewer: Access,
    *,
    title: Any,
    body: Any,
    scope: dict[str, Any],
    sources: list[dict[str, Any]],
) -> None:
    """Те же проверки, что у ``create``, без записи: черновик заметки от ИИ."""
    _title(title)
    _body(body)
    _scope(conn, scope)
    _sources(conn, viewer, sources)


def edit(
    tx: Tx,
    viewer: Access,
    note_id: str,
    *,
    version: int,
    title: Any = None,
    body: Any = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Правка возвращает заметку в «предложена»: её снова нужно подтвердить."""
    row = _row(tx.conn, viewer, note_id)
    if row["author_id"] != viewer.user_id and not viewer.has("knowledge"):
        raise _forbidden("Править заметку может автор или роль с разделом «Знания»")
    _version(row, version)
    shown_title = _title(title) if title is not None else row["title"]
    shown_body = _body(body) if body is not None else row["body"]
    if sources is not None:
        found = _sources(tx.conn, viewer, sources)
        tx.conn.execute("DELETE FROM note_sources WHERE note_seq = ?", (row["seq"],))
        _store_sources(tx, row["seq"], found)
    tx.conn.execute(
        "UPDATE knowledge_notes SET title = ?, body = ?, state = 'proposed', version = version + 1,"
        " confirmed_by = NULL, confirmed_at = NULL, stale_reason = NULL, reject_reason = NULL,"
        " updated_at = ? WHERE seq = ?",
        (shown_title, shown_body, tx.now, row["seq"]),
    )
    _index(tx.conn, row["seq"], shown_title, shown_body)
    tx.audit("note.edit", "knowledge_note", note_id, details={"version": row["version"] + 1})
    return get(tx.conn, viewer, note_id)


def confirm(tx: Tx, viewer: Access, note_id: str, version: int) -> dict[str, Any]:
    """Подтвердить: источники должны существовать, их версии фиксируются заново."""
    _moderator(viewer)
    row = _row(tx.conn, viewer, note_id)
    _version(row, version)
    if row["state"] == "rejected":
        raise EngineError("note_rejected", "Отклонённую заметку сначала исправьте", status=CONFLICT)
    current = [
        {"kind": item["source_kind"], "id": item["source_id"], "page": item["page"]}
        for item in tx.conn.execute("SELECT * FROM note_sources WHERE note_seq = ?", (row["seq"],))
    ]
    found = _sources(tx.conn, viewer, current)  # удалённый источник не даст подтвердить
    tx.conn.execute("DELETE FROM note_sources WHERE note_seq = ?", (row["seq"],))
    _store_sources(tx, row["seq"], found)
    tx.conn.execute(
        "UPDATE knowledge_notes SET state = 'confirmed', confirmed_by = ?, confirmed_at = ?,"
        " stale_reason = NULL, version = version + 1, updated_at = ? WHERE seq = ?",
        (viewer.user_id, tx.now, tx.now, row["seq"]),
    )
    tx.audit("note.confirm", "knowledge_note", note_id, details={"was": row["state"]})
    return get(tx.conn, viewer, note_id)


def reject(tx: Tx, viewer: Access, note_id: str, reason: Any) -> dict[str, Any]:
    _moderator(viewer)
    row = _row(tx.conn, viewer, note_id)
    shown = reason.strip()[:500] if isinstance(reason, str) and reason.strip() else None
    tx.conn.execute(
        "UPDATE knowledge_notes SET state = 'rejected', reject_reason = ?, version = version + 1,"
        " updated_at = ? WHERE seq = ?",
        (shown, tx.now, row["seq"]),
    )
    tx.audit("note.reject", "knowledge_note", note_id, details={"reason": shown or ""})
    return get(tx.conn, viewer, note_id)


# Заметки: чтение -------------------------------------------------------------------


def get(conn: sqlite3.Connection, viewer: Access, note_id: str) -> dict[str, Any]:
    row = _row(conn, viewer, note_id)
    return _shown(conn, row, _source_rows(conn, row["seq"]))


def listing(
    conn: sqlite3.Connection,
    viewer: Access,
    *,
    states: tuple[str, ...] = ("confirmed",),
    scope_kind: str | None = None,
    scope_id: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    unknown = [state for state in states if state not in STATES]
    if unknown:
        raise EngineError("note_state", f"Состояние — одно из: {', '.join(STATES)}", field="state")
    sql = _SELECT + f" WHERE n.state IN ({','.join('?' * len(states))})"
    params: list[Any] = list(states)
    if scope_kind:
        sql += " AND n.scope_kind = ?"
        params.append(scope_kind)
        if scope_id:
            sql += " AND n.scope_id = ?"
            params.append(scope_id)
    tokens = _tokens(query)
    if tokens:
        sql += " AND n.seq IN (SELECT rowid FROM note_fts WHERE note_fts MATCH ?)"
        params.append(" AND ".join(fulltext.phrase(token) for token in tokens))
    rows = conn.execute(
        sql + " ORDER BY n.updated_at DESC LIMIT ?", (*params, limit * 4)
    ).fetchall()
    items = []
    for row in rows:
        sources = _source_rows(conn, row["seq"])
        if _visible(conn, viewer, sources):
            items.append(_shown(conn, row, sources))
        if len(items) > limit:
            break
    return {"items": items[:limit], "more": len(items) > limit}


def queue(conn: sqlite3.Connection, viewer: Access, limit: int = 100) -> dict[str, Any]:
    """Очередь проверки (KNOW-09): предложенные и устаревшие."""
    _moderator(viewer)
    found = listing(conn, viewer, states=("proposed", "stale"), limit=limit)
    counts = dict(
        conn.execute(
            "SELECT state, COUNT(*) FROM knowledge_notes WHERE state IN ('proposed', 'stale')"
            " GROUP BY state"
        ).fetchall()
    )
    return {
        **found,
        "counts": {"proposed": counts.get("proposed", 0), "stale": counts.get("stale", 0)},
    }


def search(
    conn: sqlite3.Connection, viewer: Access, tokens: list[str], limit: int
) -> list[dict[str, Any]]:
    """Подтверждённые заметки для общего поиска: на них опирается ИИ."""
    if not tokens:
        return []
    found = listing(conn, viewer, query=" ".join(tokens), limit=limit)["items"]
    return [
        {
            "id": item["id"],
            "title": item["title"],
            "scope": item["scope"],
            "snippet": item["body"][:240],
        }
        for item in found
    ]


# Словарь станции (KNOW-10) ----------------------------------------------------------


def glossary(conn: sqlite3.Connection, query: str | None = None) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM glossary_terms ORDER BY term_norm").fetchall()
    needle = text.fold(query or "").strip()
    items = [_term(conn, row) for row in rows]
    if needle:
        items = [
            item
            for item in items
            if needle in text.fold(item["term"]) or needle in text.fold(item["meaning"])
        ]
    return {"items": items}


def add_term(tx: Tx, viewer: Access, term: Any, meaning: Any, kks_ids: list[str]) -> dict[str, Any]:
    _moderator(viewer)
    shown, norm = _term_name(term)
    if tx.conn.execute("SELECT 1 FROM glossary_terms WHERE term_norm = ?", (norm,)).fetchone():
        raise EngineError(
            "term_taken", f"«{shown}» уже есть в словаре", status=CONFLICT, field="term"
        )
    term_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO glossary_terms (id, term, term_norm, meaning, kks_ids, created_by, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            term_id,
            shown,
            norm,
            _meaning(meaning),
            dumps(_kks(tx.conn, kks_ids)),
            viewer.user_id,
            tx.now,
            tx.now,
        ),
    )
    tx.audit("glossary.add", "glossary_term", term_id, details={"term": shown})
    return _term(tx.conn, _term_row(tx.conn, term_id))


def update_term(
    tx: Tx, viewer: Access, term_id: str, term: Any, meaning: Any, kks_ids: list[str]
) -> dict[str, Any]:
    _moderator(viewer)
    _term_row(tx.conn, term_id)
    shown, norm = _term_name(term)
    other = tx.conn.execute(
        "SELECT id FROM glossary_terms WHERE term_norm = ? AND id <> ?", (norm, term_id)
    ).fetchone()
    if other:
        raise EngineError(
            "term_taken", f"«{shown}» уже есть в словаре", status=CONFLICT, field="term"
        )
    tx.conn.execute(
        "UPDATE glossary_terms SET term = ?, term_norm = ?, meaning = ?, kks_ids = ?, updated_at = ?"
        " WHERE id = ?",
        (shown, norm, _meaning(meaning), dumps(_kks(tx.conn, kks_ids)), tx.now, term_id),
    )
    tx.audit("glossary.update", "glossary_term", term_id, details={"term": shown})
    return _term(tx.conn, _term_row(tx.conn, term_id))


def delete_term(tx: Tx, viewer: Access, term_id: str) -> None:
    _moderator(viewer)
    row = _term_row(tx.conn, term_id)
    tx.conn.execute("DELETE FROM glossary_terms WHERE id = ?", (term_id,))
    tx.audit("glossary.delete", "glossary_term", term_id, details={"term": row["term"]})


def expand(conn: sqlite3.Connection, needle: str) -> list[dict[str, Any]]:
    """Термины словаря в строке запроса: вся строка или отдельное слово (KNOW-10)."""
    norms = {text.norm_name(needle)}
    norms.update(text.norm_name(word) for word in needle.split() if word)
    norms.discard("")
    if not norms:
        return []
    rows = conn.execute(
        f"SELECT * FROM glossary_terms WHERE term_norm IN ({','.join('?' * len(norms))})"
        " ORDER BY term_norm",
        tuple(norms),
    ).fetchall()
    return [_term(conn, row) for row in rows]


# Мелочи -----------------------------------------------------------------------------


def visible(conn: sqlite3.Connection, viewer: Access, note_seq: int) -> bool:
    return _visible(conn, viewer, _source_rows(conn, note_seq))


def _row(conn: sqlite3.Connection, viewer: Access, note_id: str) -> sqlite3.Row:
    row = conn.execute(_SELECT + " WHERE n.id = ?", (note_id,)).fetchone()
    if row is None or not _visible(conn, viewer, _source_rows(conn, row["seq"])):
        raise not_found("knowledge_note", note_id)  # без права на источник заметки нет
    return row


def _source_rows(conn: sqlite3.Connection, note_seq: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM note_sources WHERE note_seq = ? ORDER BY source_kind, source_id", (note_seq,)
    ).fetchall()


def _visible(conn: sqlite3.Connection, viewer: Access, sources: list[sqlite3.Row]) -> bool:
    for item in sources:
        if item["source_kind"] == "record":
            row = conn.execute(
                "SELECT d.id FROM records r JOIN datasets d ON d.seq = r.dataset_seq WHERE r.id = ?",
                (item["source_id"],),
            ).fetchone()
            if row is not None and not viewer.can(row["id"], "view"):
                return False
        elif item["source_kind"] == "document":
            row = conn.execute(
                "SELECT type_id FROM documents WHERE id = ?", (item["source_id"],)
            ).fetchone()
            if row is not None and not viewer.sees_documents(row["type_id"]):
                return False
    return True


def _shown(
    conn: sqlite3.Connection, row: sqlite3.Row, sources: list[sqlite3.Row]
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "state": row["state"],
        "origin": row["origin"],
        "version": row["version"],
        "scope": _scope_view(conn, row["scope_kind"], row["scope_id"]),
        "sources": [_source_view(conn, item) for item in sources],
        "author": row["author_name"],
        "confirmed_by": row["confirmed_by_name"],
        "confirmed_at": row["confirmed_at"],
        "stale_reason": row["stale_reason"],
        "reject_reason": row["reject_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _scope_view(conn: sqlite3.Connection, kind: str, target: str | None) -> dict[str, Any]:
    if kind == "kks":
        row = conn.execute("SELECT code FROM kks_objects WHERE id = ?", (target,)).fetchone()
        return {"kind": kind, "id": target, "code": row["code"] if row else None}
    if kind == "type":
        row = conn.execute("SELECT name FROM types WHERE id = ?", (target,)).fetchone()
        return {"kind": kind, "id": target, "name": row["name"] if row else None}
    return {"kind": "station", "id": None}


def _source_view(conn: sqlite3.Connection, item: sqlite3.Row) -> dict[str, Any]:
    shown: dict[str, Any] = {
        "kind": item["source_kind"],
        "id": item["source_id"],
        "version": item["source_version"],
        "page": item["page"],
    }
    if item["source_kind"] == "record":
        row = conn.execute(
            "SELECT version FROM records WHERE id = ?", (item["source_id"],)
        ).fetchone()
        shown["current_version"] = row["version"] if row else None
    elif item["source_kind"] == "document":
        row = conn.execute(
            "SELECT version, title FROM documents WHERE id = ?", (item["source_id"],)
        ).fetchone()
        shown.update(
            current_version=row["version"] if row else None, title=row["title"] if row else None
        )
    else:
        row = conn.execute(
            "SELECT code FROM kks_objects WHERE id = ?", (item["source_id"],)
        ).fetchone()
        shown["code"] = row["code"] if row else None
    return shown


def _scope(conn: sqlite3.Connection, scope: dict[str, Any]) -> tuple[str, str | None]:
    kind = scope.get("kind", "station")
    target = scope.get("id")
    if kind not in SCOPES:
        raise EngineError("note_scope", f"Заметка — на {', '.join(SCOPES)}", field="scope")
    if kind == "station":
        return kind, None
    table = "kks_objects" if kind == "kks" else "types"
    if (
        not target
        or conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (target,)).fetchone() is None
    ):
        raise EngineError("note_scope", "Объект заметки не найден", field="scope")
    return kind, target


def _sources(
    conn: sqlite3.Connection, viewer: Access, sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Источники с текущими версиями; источник без права у автора — как несуществующий."""
    if len(sources) > MAX_SOURCES:
        raise EngineError("note_sources", f"Источников — не больше {MAX_SOURCES}", field="sources")
    found, seen = [], set()
    for item in sources:
        kind, target = item.get("kind"), str(item.get("id") or "")
        if kind not in SOURCES:
            raise EngineError(
                "note_sources", f"Источник — одно из: {', '.join(SOURCES)}", field="sources"
            )
        if (kind, target) in seen:
            continue
        seen.add((kind, target))
        version = None
        if kind == "record":
            try:
                record = records.get(conn, target)
            except EngineError:
                record = None
            if record is None or not viewer.can(record["dataset_id"], "view"):
                raise _missing(kind, target)
            version = record["version"]
        elif kind == "document":
            row = conn.execute(
                "SELECT version, type_id, archived_at FROM documents WHERE id = ?", (target,)
            ).fetchone()
            if row is None or row["archived_at"] or not viewer.sees_documents(row["type_id"]):
                raise _missing(kind, target)
            version = row["version"]
        elif conn.execute("SELECT 1 FROM kks_objects WHERE id = ?", (target,)).fetchone() is None:
            raise _missing(kind, target)
        page = item.get("page")
        found.append(
            {
                "kind": kind,
                "id": target,
                "version": version,
                "page": str(page)[:50] if page else None,
            }
        )
    return found


def _store_sources(tx: Tx, seq: int, found: list[dict[str, Any]]) -> None:
    tx.conn.executemany(
        "INSERT INTO note_sources (note_seq, source_kind, source_id, source_version, page)"
        " VALUES (?, ?, ?, ?, ?)",
        [(seq, item["kind"], item["id"], item["version"], item["page"]) for item in found],
    )


def _index(conn: sqlite3.Connection, seq: int, title: str, body: str) -> None:
    conn.execute("DELETE FROM note_fts WHERE rowid = ?", (seq,))
    conn.execute(
        "INSERT INTO note_fts (rowid, text) VALUES (?, ?)", (seq, text.fold(f"{title}\n{body}"))
    )


def _tokens(query: str | None) -> list[str]:
    return [token for token in text.fold(query or "").split() if len(token) >= fulltext.MIN_TOKEN]


def _title(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise EngineError("note_title", "Заголовок — от 1 до 200 знаков", field="title")
    return value.strip()


def _body(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 8000:
        raise EngineError("note_body", "Текст заметки — от 1 до 8000 знаков", field="body")
    return value.strip()


def _version(row: sqlite3.Row, version: Any) -> None:
    if version != row["version"]:
        raise EngineError(
            "version_conflict",
            "Заметка уже изменена; обновите её",
            status=CONFLICT,
            details={"current_version": row["version"]},
        )


def _moderator(viewer: Access) -> None:
    if not viewer.has("knowledge"):
        raise _forbidden("Недостаточно прав: знания — подтверждать заметки и вести словарь")


def _forbidden(message: str) -> EngineError:
    return EngineError(
        "forbidden", message, status=FORBIDDEN, details={"permission": "section.knowledge"}
    )


def _missing(kind: str, target: str) -> EngineError:
    names = {"record": "Запись", "document": "Документ", "kks": "Объект ККС"}
    return EngineError(
        "note_source_missing",
        f"{names[kind]} {target} не найден или недоступен",
        field="sources",
        details={"kind": kind, "id": target},
    )


def _term_name(term: Any) -> tuple[str, str]:
    if not isinstance(term, str) or not 1 <= len(term.strip()) <= 100:
        raise EngineError("term_invalid", "Термин — от 1 до 100 знаков", field="term")
    return term.strip(), text.norm_name(term)


def _meaning(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
        raise EngineError("term_meaning", "Значение — от 1 до 1000 знаков", field="meaning")
    return value.strip()


def _kks(conn: sqlite3.Connection, kks_ids: list[str]) -> list[str]:
    found = []
    for kks_id in dict.fromkeys(kks_ids):
        if conn.execute("SELECT 1 FROM kks_objects WHERE id = ?", (kks_id,)).fetchone() is None:
            raise EngineError("term_kks", f"Объект ККС {kks_id} не найден", field="kks_ids")
        found.append(kks_id)
    return found


def _term_row(conn: sqlite3.Connection, term_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM glossary_terms WHERE id = ?", (term_id,)).fetchone()
    if row is None:
        raise not_found("glossary_term", term_id)
    return row


def _term(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    kks = []
    for kks_id in json.loads(row["kks_ids"]):
        found = conn.execute(
            "SELECT id, code, site_id FROM kks_objects WHERE id = ?", (kks_id,)
        ).fetchone()
        if found is not None:
            kks.append(dict(found))
    return {
        "id": row["id"],
        "term": row["term"],
        "meaning": row["meaning"],
        "kks": kks,
        "updated_at": row["updated_at"],
    }
