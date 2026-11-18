"""Общий чат пользователей LOCALAI (ТЗ 11.3, CHAT-01…09).

Сообщения хранятся в базе, внешних служб нет (CHAT-01). Автор — учётная
запись, время ставит сервер (CHAT-03). Ссылки в тексте разбираются при
чтении с правами читающего (CHAT-04): код ККС открывает карточку, запись —
только если читающему виден её реестр, иначе «нет доступа». Содержимое
записи в сообщение не копируется.

Ссылка на запись или документ вставляется в текст разметкой
``[[record:UUID]]`` или ``[[document:UUID]]``; коды ККС находятся в тексте
сами — по каталогу и правилу кода станции (код сигнала тоже находит аппарат).

ИИ общий чат не читает (CHAT-09): этот модуль не входит в инструменты ИИ.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from engine import kks_rules, text
from engine.access import Access
from engine.database import Tx
from engine.errors import CONFLICT, FORBIDDEN, TOO_MANY, EngineError, not_found

MAX_TEXT = 4000
MAX_REASON = 500
RATE_LIMIT = 30  # сообщений в минуту от одной учётной записи (CHAT-07)
PAGE = 50
MAX_PAGE = 200
MAX_REFS = 30  # ссылок на сообщение: больше не разбираем
MIN_TOKEN = 3
_LINK = re.compile(r"\[\[(record|document):([0-9a-fA-F-]{36})\]\]")
# Кандидат в код ККС: латиница или кириллица с цифрами, 5–40 знаков.
_CODE = re.compile(r"(?<![\w-])[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_-]{4,39}(?![\w-])")
_SELECT = (
    "SELECT m.*, u.name AS author_name, h.name AS hidden_by_name FROM chat_messages m"
    " JOIN users u ON u.id = m.author_id LEFT JOIN users h ON h.id = m.hidden_by"
)


# Запись -----------------------------------------------------------------------


def post(tx: Tx, viewer: Access, body: Any) -> dict[str, Any]:
    """Новое сообщение от имени ``viewer``."""
    shown = _text(body)
    since = (_now() - timedelta(minutes=1)).isoformat(timespec="milliseconds")
    recent = tx.conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE author_id = ? AND created_at > ?",
        (viewer.user_id, since.replace("+00:00", "Z")),
    ).fetchone()[0]
    if recent >= RATE_LIMIT:
        raise EngineError(
            "chat_rate_limit",
            f"Не больше {RATE_LIMIT} сообщений в минуту; подождите немного",
            status=TOO_MANY,
        )
    message_id = text.new_id()
    cursor = tx.conn.execute(
        "INSERT INTO chat_messages (id, author_id, body, created_at) VALUES (?, ?, ?, ?)",
        (message_id, viewer.user_id, shown, tx.now),
    )
    seq = cursor.lastrowid
    assert seq is not None
    _index(tx.conn, seq, shown)
    tx.audit("chat.post", "chat_message", message_id, details={"seq": seq, "length": len(shown)})
    return get(tx.conn, viewer, message_id)


def edit(tx: Tx, viewer: Access, message_id: str, body: Any, version: int) -> dict[str, Any]:
    """Автор правит своё сообщение; прежний текст уходит в историю (CHAT-05)."""
    row = _own(tx.conn, viewer, message_id, version)
    shown = _text(body)
    if shown == row["body"]:
        return get(tx.conn, viewer, message_id)
    tx.conn.execute(
        "INSERT INTO chat_message_versions (message_seq, version, body, at) VALUES (?, ?, ?, ?)",
        (row["seq"], row["version"], row["body"], row["edited_at"] or row["created_at"]),
    )
    tx.conn.execute(
        "UPDATE chat_messages SET body = ?, version = version + 1, edited_at = ? WHERE seq = ?",
        (shown, tx.now, row["seq"]),
    )
    _index(tx.conn, row["seq"], shown)
    tx.audit("chat.edit", "chat_message", message_id, details={"version": row["version"] + 1})
    return get(tx.conn, viewer, message_id)


def delete(tx: Tx, viewer: Access, message_id: str, version: int) -> dict[str, Any]:
    """Автор удаляет своё сообщение: текст остаётся в истории, другие видят отметку."""
    row = _own(tx.conn, viewer, message_id, version)
    tx.conn.execute(
        "UPDATE chat_messages SET deleted_at = ?, version = version + 1 WHERE seq = ?",
        (tx.now, row["seq"]),
    )
    tx.audit("chat.delete", "chat_message", message_id)
    return get(tx.conn, viewer, message_id)


def hide(
    tx: Tx, viewer: Access, message_id: str, *, hidden: bool, reason: Any = None
) -> dict[str, Any]:
    """Модератор скрывает чужое сообщение или возвращает его (CHAT-05)."""
    row = _row(tx.conn, message_id)
    if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REASON):
        raise EngineError(
            "chat_reason_invalid", f"Причина — текст до {MAX_REASON} знаков", field="reason"
        )
    if hidden and row["hidden_at"] is None:
        tx.conn.execute(
            "UPDATE chat_messages SET hidden_at = ?, hidden_by = ?, hidden_reason = ?,"
            " version = version + 1 WHERE seq = ?",
            (tx.now, viewer.user_id, (reason or "").strip() or None, row["seq"]),
        )
        tx.audit("chat.hide", "chat_message", message_id, details={"reason": reason or ""})
    elif not hidden and row["hidden_at"] is not None:
        tx.conn.execute(
            "UPDATE chat_messages SET hidden_at = NULL, hidden_by = NULL, hidden_reason = NULL,"
            " version = version + 1 WHERE seq = ?",
            (row["seq"],),
        )
        tx.audit("chat.unhide", "chat_message", message_id)
    return get(tx.conn, viewer, message_id)


def mark_read(tx: Tx, viewer: Access, seq: Any) -> dict[str, Any]:
    """Лента прочитана до сообщения ``seq``; отметка назад не двигается."""
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise EngineError("chat_seq_invalid", "Номер сообщения — целое число", field="seq")
    top = tx.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM chat_messages").fetchone()[0]
    tx.conn.execute(
        "INSERT INTO chat_reads (user_id, last_seq, read_at) VALUES (?, ?, ?)"
        " ON CONFLICT (user_id) DO UPDATE SET last_seq = max(last_seq, excluded.last_seq),"
        " read_at = excluded.read_at",
        (viewer.user_id, min(seq, top), tx.now),
    )
    return {"last_read": _last_read(tx.conn, viewer), "unread": unread(tx.conn, viewer)}


# Чтение -----------------------------------------------------------------------


def page(
    conn: sqlite3.Connection,
    viewer: Access,
    *,
    before: int | None = None,
    after: int | None = None,
    limit: int = PAGE,
) -> dict[str, Any]:
    """Лента по курсору (CHAT-07): без курсора — последние сообщения.

    ``before`` — более ранние, ``after`` — новые после известного (догрузка
    после потери связи, CHAT-02). Сообщения идут по возрастанию номера.
    """
    limit = _limit(limit)
    if before is not None and after is not None:
        raise EngineError("chat_cursor", "Укажите before или after, но не оба")
    if after is not None:
        rows = conn.execute(
            f"{_SELECT} WHERE m.seq > ? ORDER BY m.seq LIMIT ?", (after, limit + 1)
        ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
    else:
        rows = conn.execute(
            f"{_SELECT} WHERE m.seq < ? ORDER BY m.seq DESC LIMIT ?",
            (before if before is not None else 2**62, limit + 1),
        ).fetchall()
        more = len(rows) > limit
        rows = list(reversed(rows[:limit]))
    refs = _Refs(conn, viewer)
    items = [_shown(row, viewer, refs) for row in rows]
    return {
        "items": items,
        "more": more,  # есть ещё в ту же сторону
        "first": items[0]["seq"] if items else None,
        "last": items[-1]["seq"] if items else None,
        "last_read": _last_read(conn, viewer),
        "unread": unread(conn, viewer),
    }


def get(conn: sqlite3.Connection, viewer: Access, message_id: str) -> dict[str, Any]:
    row = conn.execute(f"{_SELECT} WHERE m.id = ?", (message_id,)).fetchone()
    if row is None:
        raise not_found("chat_message", message_id)
    return _shown(row, viewer, _Refs(conn, viewer))


def history(conn: sqlite3.Connection, viewer: Access, message_id: str) -> list[dict[str, Any]]:
    """Все тексты сообщения: автору и модератору."""
    row = _row(conn, message_id)
    if row["author_id"] != viewer.user_id and not _moderator(viewer):
        raise EngineError(
            "chat_history_forbidden",
            "Историю сообщения видят автор и модератор",
            status=FORBIDDEN,
        )
    items = [
        {"version": item["version"], "text": item["body"], "at": item["at"]}
        for item in conn.execute(
            "SELECT * FROM chat_message_versions WHERE message_seq = ? ORDER BY version",
            (row["seq"],),
        )
    ]
    items.append(
        {
            "version": row["version"],
            "text": row["body"],
            "at": row["edited_at"] or row["created_at"],
        }
    )
    return items


def search(
    conn: sqlite3.Connection, viewer: Access, query: Any, *, limit: int = PAGE
) -> dict[str, Any]:
    """Сообщения, где встречается каждое слово запроса; новые сначала."""
    if not isinstance(query, str) or not query.strip():
        raise EngineError("chat_query_empty", "Введите, что искать", field="q")
    limit = _limit(limit)
    clauses, params = [], []
    for token in text.fold(query).split()[:10]:
        if len(token) >= MIN_TOKEN:
            clauses.append("m.seq IN (SELECT rowid FROM chat_fts WHERE chat_fts MATCH ?)")
            params.append('"' + token.replace('"', '""') + '"')
        else:
            clauses.append("instr(lai_fold(m.body), ?) > 0")
            params.append(token)
    if not _moderator(viewer):
        clauses.append("m.deleted_at IS NULL AND m.hidden_at IS NULL")
    rows = conn.execute(
        f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY m.seq DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    refs = _Refs(conn, viewer)
    return {"items": [_shown(row, viewer, refs) for row in rows]}


def unread(conn: sqlite3.Connection, viewer: Access) -> int:
    """Чужие видимые сообщения после отметки «прочитано»."""
    return conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE seq > ? AND author_id <> ?"
        " AND deleted_at IS NULL AND hidden_at IS NULL",
        (_last_read(conn, viewer), viewer.user_id),
    ).fetchone()[0]


# Вид сообщения для читающего --------------------------------------------------------


def _shown(row: sqlite3.Row, viewer: Access, refs: _Refs) -> dict[str, Any]:
    moderator = _moderator(viewer)
    mine = row["author_id"] == viewer.user_id
    state = "hidden" if row["hidden_at"] else "deleted" if row["deleted_at"] else "active"
    visible = state == "active" or moderator
    item: dict[str, Any] = {
        "id": row["id"],
        "seq": row["seq"],
        "author": {"id": row["author_id"], "name": row["author_name"]},
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "version": row["version"],
        "state": state,
        "text": row["body"] if visible else None,
        "refs": refs.find(row["body"]) if visible else [],
        "mine": mine,
        "can": {
            "edit": mine and state == "active" and viewer.has("chat_write"),
            "delete": mine and state == "active" and viewer.has("chat_write"),
            "hide": moderator and state != "hidden" and not mine,
            "unhide": moderator and state == "hidden",
            "history": mine or moderator,
        },
    }
    if state == "hidden":
        item["hidden"] = {
            "at": row["hidden_at"],
            "by": row["hidden_by_name"],
            "reason": row["hidden_reason"],
        }
    return item


class _Refs:
    """Ссылки в тексте с правами читающего; поиск в каталоге — один раз на код."""

    def __init__(self, conn: sqlite3.Connection, viewer: Access) -> None:
        self.conn, self.viewer = conn, viewer
        self.sites = [row[0] for row in conn.execute("SELECT id FROM sites ORDER BY created_at")]
        self._kks: dict[str, dict[str, Any] | None] = {}
        self._records: dict[str, dict[str, Any]] = {}

    def find(self, body: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        taken: list[tuple[int, int]] = []
        for match in _LINK.finditer(body):
            kind, target = match.group(1), match.group(2).lower()
            item = self._record(target) if kind == "record" else {"access": False}
            found.append(
                {"kind": kind, "id": target, "start": match.start(), "end": match.end(), **item}
            )
            taken.append(match.span())
        for match in _CODE.finditer(body):
            if len(found) >= MAX_REFS:
                break
            if any(start <= match.start() < end for start, end in taken):
                continue
            token = match.group(0)
            if not (any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token)):
                continue
            hit = self._code(token)
            if hit is not None:
                found.append({"start": match.start(), "end": match.end(), **hit})
        return sorted(found, key=lambda item: item["start"])[:MAX_REFS]

    def _code(self, token: str) -> dict[str, Any] | None:
        if token not in self._kks:
            hit = None
            for site_id in self.sites:
                try:
                    resolved = kks_rules.resolve(self.conn, site_id, token)
                except EngineError:
                    resolved = None
                if resolved is not None:
                    hit = {
                        "kind": "kks",
                        "id": resolved["id"],
                        "code": resolved["code"],
                        "site_id": site_id,
                        "signal": resolved.get("signal"),
                        "access": True,  # каталог ККС виден каждому вошедшему
                    }
                    break
            self._kks[token] = hit
        return self._kks[token]

    def _record(self, record_id: str) -> dict[str, Any]:
        """Запись без права на её реестр и несуществующая выглядят одинаково."""
        if record_id not in self._records:
            row = self.conn.execute(
                "SELECT d.id AS dataset_id, d.name FROM records r"
                " JOIN datasets d ON d.seq = r.dataset_seq WHERE r.id = ?",
                (record_id,),
            ).fetchone()
            if row is None or not self.viewer.can(row["dataset_id"], "view"):
                self._records[record_id] = {"access": False}
            else:
                self._records[record_id] = {
                    "access": True,
                    "dataset_id": row["dataset_id"],
                    "dataset": row["name"],
                }
        return self._records[record_id]


# Мелочи -----------------------------------------------------------------------


def _text(body: Any) -> str:
    if not isinstance(body, str):
        raise EngineError("chat_text_invalid", "Сообщение — текст", field="text")
    shown = body.replace("\r\n", "\n").strip()
    if not shown:
        raise EngineError("chat_text_empty", "Пустое сообщение не отправляется", field="text")
    if len(shown) > MAX_TEXT:
        raise EngineError(
            "chat_text_too_long", f"Сообщение длиннее {MAX_TEXT} знаков", field="text"
        )
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in shown):
        raise EngineError("chat_text_invalid", "В сообщении есть управляющие символы", field="text")
    return shown


def _row(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise not_found("chat_message", message_id)
    return row


def _own(conn: sqlite3.Connection, viewer: Access, message_id: str, version: Any) -> sqlite3.Row:
    row = _row(conn, message_id)
    if row["author_id"] != viewer.user_id:
        raise EngineError(
            "chat_not_author", "Править и удалять можно только свои сообщения", status=FORBIDDEN
        )
    if row["deleted_at"] or row["hidden_at"]:
        raise EngineError(
            "chat_message_closed",
            "Сообщение удалено или скрыто модератором; править его нельзя",
            status=CONFLICT,
        )
    if version != row["version"]:
        raise EngineError(
            "version_conflict",
            "Сообщение уже изменено; обновите ленту",
            status=CONFLICT,
            details={"current_version": row["version"]},
        )
    return row


def _index(conn: sqlite3.Connection, seq: int, body: str) -> None:
    conn.execute("DELETE FROM chat_fts WHERE rowid = ?", (seq,))
    conn.execute("INSERT INTO chat_fts (rowid, text) VALUES (?, ?)", (seq, text.fold(body)))


def _last_read(conn: sqlite3.Connection, viewer: Access) -> int:
    row = conn.execute(
        "SELECT last_seq FROM chat_reads WHERE user_id = ?", (viewer.user_id,)
    ).fetchone()
    return row[0] if row else 0


def _moderator(viewer: Access) -> bool:
    return viewer.has("chat_moderate")


def _limit(limit: int) -> int:
    if not 1 <= limit <= MAX_PAGE:
        raise EngineError("limit_invalid", f"Размер страницы — от 1 до {MAX_PAGE}", field="limit")
    return limit


def _now() -> datetime:
    return datetime.now(UTC)
