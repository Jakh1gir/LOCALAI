"""Документы: файл, версия, чтение, фрагменты, привязки и упоминания ККС.

ТЗ 8 (KNOW-01…04), DATA-09. Документ видят роли с правом «видеть» на его тип
(``doc_type`` в матрице прав); загружать, заменять, привязывать и решать
по упоминаниям ККС — раздел «Документы».

Текст документа приходит постранично от читателя (сервер или процесс схем) и
делится на фрагменты по абзацам с адресом «документ, страница». Фрагменты
текущей версии лежат в полнотекстовом индексе; замена файла (новая версия)
и архив документа убирают прежние фрагменты из поиска (KNOW-04).

Коды ККС, найденные в тексте, становятся предложениями связи (KNOW-03);
связь «документ ↔ ККС» создаётся только решением человека.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import PurePath
from typing import Any

from engine import jobs, kks_rules, records, text
from engine.access import Access
from engine.database import Tx, dumps
from engine.errors import CONFLICT, FORBIDDEN, EngineError, not_found

DEFAULT_TYPE = "00000000-0000-4000-8000-0000000d0c01"
JOB_KIND = "document.read"
# Кто читает файл: сервер — простые форматы, процесс схем — остальное (ТЗ 18.2).
SERVER_FORMATS = {".txt", ".md", ".csv", ".xlsx", ".docx"}
VISION_FORMATS = {".pdf", ".dxf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
MAX_TITLE = 300
CHUNK = 1200  # знаков во фрагменте
OVERLAP = 150  # перекрытие соседних фрагментов, чтобы фраза на стыке нашлась
MAX_TEXT = 20_000_000
MAX_MENTION_TOKENS = 5000
_CODE = re.compile(r"(?<![\w-])[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_-]{4,39}(?![\w-])")
_SELECT = (
    "SELECT d.*, t.name AS type_name, u.name AS created_by_name FROM documents d"
    " JOIN document_types t ON t.id = d.type_id LEFT JOIN users u ON u.id = d.created_by"
)


def reader_for(file_name: str) -> str:
    suffix = PurePath(file_name).suffix.lower()
    if suffix in SERVER_FORMATS:
        return "server"
    if suffix in VISION_FORMATS:
        return "vision"
    known = ", ".join(sorted(SERVER_FORMATS | VISION_FORMATS))
    raise EngineError(
        "document_format", f"Формат {suffix or 'без расширения'} не читается; можно: {known}"
    )


# Запись -----------------------------------------------------------------------


def create(
    tx: Tx,
    viewer: Access,
    *,
    title: Any,
    file_name: str,
    sha256: str,
    size: int,
    media_type: str,
    type_id: str | None = None,
) -> dict[str, Any]:
    _writer(viewer)
    kind = _type(tx.conn, type_id or DEFAULT_TYPE)
    if not viewer.sees_documents(kind["id"]):
        raise _forbidden("Недостаточно прав: документы этого типа")
    reader = reader_for(file_name)
    shown = _title(title, file_name)
    _file(tx, sha256, size, media_type)
    document_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO documents (id, type_id, title, file_name, file_sha256, state, reader,"
        " created_by, created_at, updated_at) VALUES (?,?,?,?,?, 'queued', ?,?,?,?)",
        (document_id, kind["id"], shown, file_name, sha256, reader, viewer.user_id, tx.now, tx.now),
    )
    jobs.enqueue(tx, JOB_KIND, reader, document_id, 1)
    tx.audit("document.create", "document", document_id, details={"title": shown, "sha256": sha256})
    return get(tx.conn, viewer, document_id)


def replace(
    tx: Tx,
    viewer: Access,
    document_id: str,
    *,
    expected_version: int,
    file_name: str,
    sha256: str,
    size: int,
    media_type: str,
) -> dict[str, Any]:
    """Новый файл документа: новая версия и новое чтение; тот же файл — ничего (KNOW-04)."""
    _writer(viewer)
    row = _row(tx.conn, viewer, document_id)
    _open(row)
    if row["version"] != expected_version:
        raise EngineError(
            "version_conflict",
            "Документ уже изменён; обновите карточку",
            status=CONFLICT,
            details={"current_version": row["version"]},
        )
    if row["file_sha256"] == sha256:
        return get(tx.conn, viewer, document_id)
    reader = reader_for(file_name)
    _file(tx, sha256, size, media_type)
    version = row["version"] + 1
    _drop_text(tx, row["seq"])
    tx.conn.execute(
        "UPDATE documents SET file_name = ?, file_sha256 = ?, version = ?, state = 'queued',"
        " error = NULL, pages = NULL, reader = ?, read_at = NULL, updated_at = ? WHERE seq = ?",
        (file_name, sha256, version, reader, tx.now, row["seq"]),
    )
    jobs.enqueue(tx, JOB_KIND, reader, document_id, version)
    tx.audit(
        "document.replace", "document", document_id, details={"version": version, "sha256": sha256}
    )
    return get(tx.conn, viewer, document_id)


def archive(tx: Tx, viewer: Access, document_id: str) -> dict[str, Any]:
    """В архив: фрагменты уходят из поиска, задачи чтения отменяются (KNOW-04)."""
    _writer(viewer)
    row = _row(tx.conn, viewer, document_id)
    if row["archived_at"] is None:
        _drop_text(tx, row["seq"])
        jobs.cancel_for(tx, document_id)
        tx.conn.execute(
            "UPDATE documents SET archived_at = ?, updated_at = ? WHERE seq = ?",
            (tx.now, tx.now, row["seq"]),
        )
        tx.audit("document.archive", "document", document_id)
    return get(tx.conn, viewer, document_id)


def link(tx: Tx, viewer: Access, document_id: str, kind: str, target_id: str) -> dict[str, Any]:
    """Привязать документ к записи (нужно видеть её реестр) или к объекту ККС."""
    _writer(viewer)
    row = _row(tx.conn, viewer, document_id)
    _open(row)
    target_id = _target(tx.conn, viewer, kind, target_id)
    tx.conn.execute(
        "INSERT OR IGNORE INTO document_links (document_seq, target_kind, target_id, created_by,"
        " created_at) VALUES (?, ?, ?, ?, ?)",
        (row["seq"], kind, target_id, viewer.user_id, tx.now),
    )
    tx.audit("document.link", "document", document_id, details={"kind": kind, "target": target_id})
    return get(tx.conn, viewer, document_id)


def check_link(
    conn: sqlite3.Connection, viewer: Access, document_id: Any, kind: Any, target_id: Any
) -> str:
    """Проверки привязки без записи — для черновика ИИ «связь документа с ККС»."""
    _writer(viewer)
    if not isinstance(document_id, str) or not isinstance(target_id, str):
        raise EngineError("document_link_invalid", "Нужны документ и цель привязки")
    _open(_row(conn, viewer, document_id))
    return _target(conn, viewer, str(kind), target_id)


def unlink(tx: Tx, viewer: Access, document_id: str, kind: str, target_id: str) -> dict[str, Any]:
    _writer(viewer)
    row = _row(tx.conn, viewer, document_id)
    gone = tx.conn.execute(
        "DELETE FROM document_links WHERE document_seq = ? AND target_kind = ? AND target_id = ?",
        (row["seq"], kind, target_id),
    ).rowcount
    if gone:
        tx.audit(
            "document.unlink", "document", document_id, details={"kind": kind, "target": target_id}
        )
    return get(tx.conn, viewer, document_id)


def decide_mention(
    tx: Tx, viewer: Access, document_id: str, kks_id: str, confirm: bool
) -> dict[str, Any]:
    """Решение по найденному в тексте коду: подтверждение создаёт связь с ККС (KNOW-03)."""
    _writer(viewer)
    row = _row(tx.conn, viewer, document_id)
    _open(row)
    found = tx.conn.execute(
        "SELECT * FROM document_mentions WHERE document_seq = ? AND kks_id = ?",
        (row["seq"], kks_id),
    ).fetchone()
    if found is None:
        raise not_found("document_mention", kks_id)
    tx.conn.execute(
        "UPDATE document_mentions SET state = ?, decided_by = ?, decided_at = ?"
        " WHERE document_seq = ? AND kks_id = ?",
        ("confirmed" if confirm else "rejected", viewer.user_id, tx.now, row["seq"], kks_id),
    )
    if confirm:
        tx.conn.execute(
            "INSERT OR IGNORE INTO document_links (document_seq, target_kind, target_id, created_by,"
            " created_at) VALUES (?, 'kks', ?, ?, ?)",
            (row["seq"], kks_id, viewer.user_id, tx.now),
        )
    tx.audit(
        "document.mention",
        "document",
        document_id,
        details={"kks_id": kks_id, "decision": "confirmed" if confirm else "rejected"},
    )
    return mentions(tx.conn, viewer, document_id)


# Чтение: результат читателя ----------------------------------------------------------


def start_reading(tx: Tx, document_id: str, version: int) -> bool:
    """Документ в состоянии «читается»; ``False`` — версия уже сменилась."""
    return (
        tx.conn.execute(
            "UPDATE documents SET state = 'reading', updated_at = ? WHERE id = ? AND version = ?"
            " AND archived_at IS NULL",
            (tx.now, document_id, version),
        ).rowcount
        == 1
    )


def store_text(
    tx: Tx, document_id: str, version: int, pages: list[dict[str, Any]], reader: str
) -> dict[str, Any]:
    """Страницы текста → фрагменты, индекс и предложения связей ККС."""
    row = tx.conn.execute(
        "SELECT * FROM documents WHERE id = ? AND version = ? AND archived_at IS NULL",
        (document_id, version),
    ).fetchone()
    if row is None:
        raise EngineError(
            "document_version_gone",
            "Документ заменён или в архиве; результат прежней версии не принят",
            status=CONFLICT,
        )
    clean = _pages(pages)
    _drop_text(tx, row["seq"])
    pieces = chunk_pages(clean)
    for ordinal, (page, area, piece) in enumerate(pieces, start=1):
        seq = tx.conn.execute(
            "INSERT INTO chunks (document_seq, version, ordinal, page, area, text) VALUES (?,?,?,?,?,?)",
            (row["seq"], version, ordinal, page, dumps(area) if area else None, piece),
        ).lastrowid
        tx.conn.execute(
            "INSERT INTO chunk_fts (rowid, text) VALUES (?, ?)", (seq, text.fold(piece))
        )
    found = _mentions(tx, row["seq"], clean)
    state = "ready" if pieces else "empty"
    tx.conn.execute(
        "UPDATE documents SET state = ?, error = NULL, pages = ?, reader = ?, read_at = ?,"
        " updated_at = ? WHERE seq = ?",
        (state, len(clean), reader, tx.now, tx.now, row["seq"]),
    )
    tx.audit(
        "document.read",
        "document",
        document_id,
        details={"version": version, "chunks": len(pieces), "mentions": found},
    )
    return {"state": state, "pages": len(clean), "chunks": len(pieces), "mentions": found}


def reading_failed(tx: Tx, document_id: str, version: int, error: str, final: bool) -> None:
    tx.conn.execute(
        "UPDATE documents SET state = ?, error = ?, updated_at = ? WHERE id = ? AND version = ?",
        ("error" if final else "queued", error[:2000], tx.now, document_id, version),
    )


def chunk_pages(pages: list[dict[str, Any]]) -> list[tuple[str, Any, str]]:
    """Фрагменты по абзацам до ``CHUNK`` знаков с перекрытием; страница не смешивается."""
    found: list[tuple[str, Any, str]] = []
    for item in pages:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", item["text"]) if part.strip()]
        current = ""
        for paragraph in paragraphs:
            while len(paragraph) > CHUNK:  # длинный абзац режется по словам
                cut = paragraph.rfind(" ", 0, CHUNK)
                cut = cut if cut > CHUNK // 2 else CHUNK
                if current:
                    found.append((item["page"], item.get("area"), current))
                    current = ""
                found.append((item["page"], item.get("area"), paragraph[:cut].strip()))
                paragraph = paragraph[max(cut - OVERLAP, 1) :].strip()
            if current and len(current) + len(paragraph) + 2 > CHUNK:
                found.append((item["page"], item.get("area"), current))
                current = current[-OVERLAP:].split(" ", 1)[-1] + "\n\n" + paragraph
            else:
                current = f"{current}\n\n{paragraph}" if current else paragraph
        if current:
            found.append((item["page"], item.get("area"), current))
    return found


# Чтение для людей ----------------------------------------------------------------


def get(conn: sqlite3.Connection, viewer: Access, document_id: str) -> dict[str, Any]:
    row = _row(conn, viewer, document_id)
    return _shown(conn, viewer, row)


def listing(
    conn: sqlite3.Connection,
    viewer: Access,
    *,
    target_kind: str | None = None,
    target_id: str | None = None,
    state: str | None = None,
    archived: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    sql = _SELECT + " WHERE (d.archived_at IS NOT NULL) = ?"
    params: list[Any] = [archived]
    if target_kind and target_id:
        sql += (
            " AND d.seq IN (SELECT document_seq FROM document_links"
            " WHERE target_kind = ? AND target_id = ?)"
        )
        params += [target_kind, target_id]
    if state:
        sql += " AND d.state = ?"
        params.append(state)
    rows = conn.execute(
        sql + " ORDER BY d.updated_at DESC LIMIT ?", (*params, limit + 1)
    ).fetchall()
    visible = [row for row in rows if viewer.sees_documents(row["type_id"])]
    return {"items": [_brief(row) for row in visible[:limit]], "more": len(visible) > limit}


def mentions(conn: sqlite3.Connection, viewer: Access, document_id: str) -> dict[str, Any]:
    row = _row(conn, viewer, document_id)
    items = [
        {
            "kks_id": item["kks_id"],
            "code": item["code"],
            "site_id": item["site_id"],
            "pages": json.loads(item["pages"]),
            "hits": item["hits"],
            "state": item["state"],
        }
        for item in conn.execute(
            "SELECT m.*, k.code, k.site_id FROM document_mentions m"
            " JOIN kks_objects k ON k.id = m.kks_id WHERE m.document_seq = ?"
            " ORDER BY m.state = 'proposed' DESC, m.hits DESC, k.code",
            (row["seq"],),
        )
    ]
    return {"items": items}


def chunks(
    conn: sqlite3.Connection, viewer: Access, document_id: str, page: str | None = None
) -> dict[str, Any]:
    """Текст документа по фрагментам — для просмотра и ссылок «страница N»."""
    row = _row(conn, viewer, document_id)
    sql = "SELECT ordinal, page, area, text FROM chunks WHERE document_seq = ?"
    params: list[Any] = [row["seq"]]
    if page is not None:
        sql += " AND page = ?"
        params.append(page)
    return {
        "version": row["version"],
        "items": [
            {
                "ordinal": item["ordinal"],
                "page": item["page"],
                "area": json.loads(item["area"]) if item["area"] else None,
                "text": item["text"],
            }
            for item in conn.execute(sql + " ORDER BY ordinal", params)
        ],
    }


def search(
    conn: sqlite3.Connection, viewer: Access, tokens: list[str], limit: int
) -> list[dict[str, Any]]:
    """Фрагменты видимых документов, где есть все слова; по одному лучшему на страницу."""
    if not tokens:
        return []
    match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    rows = conn.execute(
        "SELECT c.seq, c.page, c.text, d.id, d.title, d.type_id, t.name AS type_name"
        " FROM chunks c JOIN documents d ON d.seq = c.document_seq"
        " JOIN document_types t ON t.id = d.type_id"
        " WHERE c.seq IN (SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ?)"
        " AND d.archived_at IS NULL AND c.version = d.version"
        " ORDER BY d.updated_at DESC, c.ordinal LIMIT ?",
        (match, limit * 5),
    ).fetchall()
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not viewer.sees_documents(row["type_id"]) or (row["id"], row["page"]) in seen:
            continue
        seen.add((row["id"], row["page"]))
        found.append(
            {
                "document_id": row["id"],
                "title": row["title"],
                "type": row["type_name"],
                "page": row["page"],
                "snippet": _snippet(row["text"], tokens),
            }
        )
        if len(found) >= limit:
            break
    return found


# Мелочи -----------------------------------------------------------------------


def _shown(conn: sqlite3.Connection, viewer: Access, row: sqlite3.Row) -> dict[str, Any]:
    item = _brief(row)
    links = []
    for link_row in conn.execute(
        "SELECT target_kind, target_id, created_at FROM document_links WHERE document_seq = ?"
        " ORDER BY created_at",
        (row["seq"],),
    ):
        links.append(_link_view(conn, viewer, link_row))
    job = jobs.active_for(conn, row["id"])
    item.update(
        {
            "file": {
                "name": row["file_name"],
                "sha256": row["file_sha256"],
                **_file_info(conn, row["file_sha256"]),
            },
            "error": row["error"],
            "reader": row["reader"],
            "read_at": row["read_at"],
            "created_by": row["created_by_name"],
            "links": links,
            "job": {"state": job["state"], "attempts": job["attempts"]} if job else None,
            "can": {"edit": viewer.has("documents") and row["archived_at"] is None},
        }
    )
    return item


def _brief(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "type": {"id": row["type_id"], "name": row["type_name"]},
        "version": row["version"],
        "state": row["state"],
        "pages": row["pages"],
        "file_name": row["file_name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived": row["archived_at"] is not None,
    }


def _link_view(conn: sqlite3.Connection, viewer: Access, row: sqlite3.Row) -> dict[str, Any]:
    if row["target_kind"] == "kks":
        found = conn.execute(
            "SELECT code FROM kks_objects WHERE id = ?", (row["target_id"],)
        ).fetchone()
        return {
            "kind": "kks",
            "id": row["target_id"],
            "code": found["code"] if found else None,
            "access": True,
        }
    found = conn.execute(
        "SELECT d.id AS dataset_id, d.name FROM records r JOIN datasets d ON d.seq = r.dataset_seq"
        " WHERE r.id = ?",
        (row["target_id"],),
    ).fetchone()
    if found is None or not viewer.can(found["dataset_id"], "view"):
        return {"kind": "record", "id": row["target_id"], "access": False}
    return {
        "kind": "record",
        "id": row["target_id"],
        "access": True,
        "dataset_id": found["dataset_id"],
        "dataset": found["name"],
    }


def _row(conn: sqlite3.Connection, viewer: Access, document_id: str) -> sqlite3.Row:
    row = conn.execute(_SELECT + " WHERE d.id = ?", (document_id,)).fetchone()
    if row is None or not viewer.sees_documents(row["type_id"]):
        raise not_found("document", document_id)  # без права документ не существует
    return row


def _open(row: sqlite3.Row) -> None:
    if row["archived_at"] is not None:
        raise EngineError("document_archived", "Документ в архиве", status=CONFLICT)


def _writer(viewer: Access) -> None:
    if not viewer.has("documents"):
        raise _forbidden("Недостаточно прав: документы — загружать и привязывать")


def _forbidden(message: str) -> EngineError:
    return EngineError(
        "forbidden", message, status=FORBIDDEN, details={"permission": "section.documents"}
    )


def _type(conn: sqlite3.Connection, type_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM document_types WHERE id = ?", (type_id,)).fetchone()
    if row is None or row["archived_at"]:
        raise EngineError("document_type", "Такого типа документов нет", field="type_id")
    return row


def _title(title: Any, file_name: str) -> str:
    shown = (
        title if isinstance(title, str) and title.strip() else PurePath(file_name).stem
    ).strip()
    if not shown or len(shown) > MAX_TITLE:
        raise EngineError("document_title", f"Название — от 1 до {MAX_TITLE} знаков", field="title")
    return shown


def _file(tx: Tx, sha256: str, size: int, media_type: str) -> None:
    tx.conn.execute(
        "INSERT OR IGNORE INTO files (sha256, size, media_type, created_at) VALUES (?, ?, ?, ?)",
        (sha256, size, media_type, tx.now),
    )


def _file_info(conn: sqlite3.Connection, sha256: str) -> dict[str, Any]:
    row = conn.execute("SELECT size, media_type FROM files WHERE sha256 = ?", (sha256,)).fetchone()
    return {"size": row["size"], "media_type": row["media_type"]} if row else {}


def _target(conn: sqlite3.Connection, viewer: Access, kind: str, target_id: str) -> str:
    if kind == "record":
        record = records.get(conn, target_id)
        if not viewer.can(record["dataset_id"], "view"):
            raise not_found("record", target_id)
        return record["id"]
    if kind == "kks":
        if conn.execute("SELECT 1 FROM kks_objects WHERE id = ?", (target_id,)).fetchone() is None:
            raise not_found("kks", target_id)
        return target_id
    raise EngineError(
        "document_link_kind", "Привязка — к записи (record) или ККС (kks)", field="kind"
    )


def _drop_text(tx: Tx, document_seq: int) -> None:
    for (seq,) in tx.conn.execute("SELECT seq FROM chunks WHERE document_seq = ?", (document_seq,)):
        tx.conn.execute("DELETE FROM chunk_fts WHERE rowid = ?", (seq,))
    tx.conn.execute("DELETE FROM chunks WHERE document_seq = ?", (document_seq,))
    tx.conn.execute(
        "DELETE FROM document_mentions WHERE document_seq = ? AND state = 'proposed'",
        (document_seq,),
    )


def _pages(pages: Any) -> list[dict[str, Any]]:
    if not isinstance(pages, list):
        raise EngineError("document_text", "Текст — список страниц {page, text}", field="pages")
    clean, total = [], 0
    for index, item in enumerate(pages, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("text", ""), str):
            raise EngineError("document_text", f"Страница {index}: нужен текст", field="pages")
        page = str(item.get("page") or index)[:50]
        body = item.get("text", "").replace("\r\n", "\n").replace("\x00", "")
        total += len(body)
        if total > MAX_TEXT:
            raise EngineError(
                "document_text", "Текст документа больше 20 млн знаков", field="pages"
            )
        area = item.get("area")
        clean.append({"page": page, "text": body, "area": area if isinstance(area, dict) else None})
    return clean


def _mentions(tx: Tx, document_seq: int, pages: list[dict[str, Any]]) -> int:
    """Коды ККС в тексте → предложения; решённые раньше остаются как есть."""
    sites = [row[0] for row in tx.conn.execute("SELECT id FROM sites WHERE archived_at IS NULL")]
    cache: dict[str, list[str]] = {}
    found: dict[str, dict[str, Any]] = {}
    checked = 0
    for item in pages:
        for match in _CODE.finditer(item["text"]):
            token = match.group(0)
            if not (any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token)):
                continue
            if token not in cache:
                checked += 1
                if checked > MAX_MENTION_TOKENS:
                    break
                cache[token] = _resolve(tx.conn, sites, token)
            for kks_id in cache[token]:
                entry = found.setdefault(kks_id, {"pages": [], "hits": 0})
                entry["hits"] += 1
                if item["page"] not in entry["pages"]:
                    entry["pages"].append(item["page"])
    for kks_id, entry in found.items():
        tx.conn.execute(
            "INSERT INTO document_mentions (document_seq, kks_id, pages, hits) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (document_seq, kks_id) DO UPDATE SET pages = excluded.pages,"
            " hits = excluded.hits",
            (document_seq, kks_id, dumps(entry["pages"][:200]), entry["hits"]),
        )
    return len(found)


def _resolve(conn: sqlite3.Connection, sites: list[str], token: str) -> list[str]:
    hits = []
    for site_id in sites:
        try:
            found = kks_rules.resolve(conn, site_id, token)
        except EngineError:
            found = None
        if found is not None:
            hits.append(found["id"])
    return hits


def _snippet(body: str, tokens: list[str], width: int = 240) -> str:
    folded = text.fold(body)
    start = min((folded.find(token) for token in tokens if token in folded), default=0)
    left = max(start - width // 3, 0)
    shown = body[left : left + width].replace("\n", " ").strip()
    return ("…" if left else "") + shown + ("…" if left + width < len(body) else "")
