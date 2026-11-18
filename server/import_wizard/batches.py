"""Пакеты мастера импорта: загруженная книга, её листы, строки плана, решения, профили.

Пакет принадлежит тому, кто загрузил книгу: чужой пакет не виден (404).
Книга лежит файлом ``<id>.xlsx`` в папке ``imports`` рядом с базой и живёт
сутки; применённый пакет хранит отчёт, а файл и строки плана удаляются.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from engine import Database, EngineError, Tx, text
from engine.access import Access
from engine.database import dumps
from engine.errors import CONFLICT, FORBIDDEN, not_found
from server import xlsx

LIFETIME = timedelta(hours=24)
MAX_FILE = xlsx.MAX_BYTES
PAGE = 100
MAX_PAGE = 500
ACTIONS = ("create", "update", "unchanged", "conflict", "error")
_ZIP = b"PK\x03\x04"  # книга .xlsx — архив ZIP


def can_import(access: Access) -> bool:
    """Мастер открыт тому, кто может импортировать хоть куда-то или создать тип."""
    return access.has("structure") or any(
        "import" in actions for actions in access.datasets.values()
    )


def upload(db: Database, folder: Path, access: Access, name: Any, data: bytes) -> dict[str, Any]:
    """Шаг 1: книга сохраняется, листы разбираются для предпросмотра.

    Разбор книги идёт вне транзакции записи (API-07): другие пользователи не
    ждут, пока сервер читает 25 МиБ Excel.
    """
    if not can_import(access):
        raise EngineError("forbidden", "Недостаточно прав: импорт в реестры", status=FORBIDDEN)
    shown = str(name or "").strip()
    if not shown.lower().endswith(".xlsx") or len(shown) > 255 or "/" in shown or "\\" in shown:
        raise EngineError("import_file_name", "Нужна книга Excel .xlsx", field="name")
    if not data:
        raise EngineError("import_file_empty", "Файл пуст")
    if len(data) > MAX_FILE:
        raise EngineError("xlsx_too_large", "Книга больше 25 МиБ")
    if not data.startswith(_ZIP):
        raise EngineError("xlsx_invalid", "Файл не является книгой Excel .xlsx")
    with db.write(access.actor) as tx:
        expire(tx, folder)
    batch_id = text.new_id()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{batch_id}.xlsx"
    part = path.with_name(f"{batch_id}.uploading.xlsx")  # openpyxl смотрит на расширение
    part.write_bytes(data)
    try:
        sheets = xlsx.outline(part)
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)
    expires = (datetime.now(UTC) + LIFETIME).isoformat(timespec="milliseconds")
    try:
        with db.write(access.actor) as tx:
            _insert(tx, access, batch_id, shown, data, sheets, expires)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    with db.read() as conn:
        return view(conn, access, batch_id)


def _insert(
    tx: Tx,
    access: Access,
    batch_id: str,
    shown: str,
    data: bytes,
    sheets: list[dict[str, Any]],
    expires: str,
) -> None:
    tx.conn.execute(
        "INSERT INTO import_batches (id, user_id, file_name, file_sha256, file_size, status,"
        " preview, created_at, expires_at) VALUES (?,?,?,?,?, 'uploaded', ?,?,?)",
        (
            batch_id,
            access.user_id,
            shown,
            hashlib.sha256(data).hexdigest(),
            len(data),
            dumps({"sheets": sheets}),
            tx.now,
            expires.replace("+00:00", "Z"),
        ),
    )
    tx.audit("import.upload", "import", batch_id, details={"file": shown, "size": len(data)})


def row(conn: Any, access: Access, batch_id: str) -> dict[str, Any]:
    found = conn.execute(
        "SELECT * FROM import_batches WHERE id = ? AND user_id = ?", (batch_id, access.user_id)
    ).fetchone()
    if found is None:
        raise not_found("import", batch_id)
    item = dict(found)
    for key in ("settings", "preview", "summary", "plan_schema", "report"):
        item[key] = json.loads(item[key]) if item[key] else None
    return item


def view(conn: Any, access: Access, batch_id: str) -> dict[str, Any]:
    item = row(conn, access, batch_id)
    return {
        "id": item["id"],
        "file": {"name": item["file_name"], "size": item["file_size"]},
        "status": item["status"],
        "sheets": item["preview"].get("sheets", []),
        "settings": item["settings"],
        "summary": item["summary"],
        "report": item["report"],
        "created_at": item["created_at"],
        "planned_at": item["planned_at"],
        "applied_at": item["applied_at"],
        "expires_at": item["expires_at"],
    }


def file_of(folder: Path, batch: dict[str, Any]) -> Path:
    path = folder / f"{batch['id']}.xlsx"
    if not path.is_file():
        raise EngineError(
            "import_expired",
            "Книга этого импорта уже удалена; загрузите её заново",
            status=CONFLICT,
        )
    return path


def check_open(batch: dict[str, Any]) -> None:
    """Пакет ещё можно планировать и применять."""
    if batch["expires_at"] < text.now():
        raise EngineError(
            "import_expired", "Срок импорта истёк; загрузите книгу заново", status=CONFLICT
        )


def rows_page(
    conn: Any,
    access: Access,
    batch_id: str,
    *,
    action: str | None = None,
    after: int = 0,
    limit: int = PAGE,
) -> dict[str, Any]:
    """Шаг 4: строки плана по номеру строки листа, с фильтром по действию."""
    row(conn, access, batch_id)
    if action is not None and action not in ACTIONS:
        raise EngineError(
            "request_invalid", "Параметр «action»: " + ", ".join(ACTIONS), field="action"
        )
    if not 1 <= limit <= MAX_PAGE:
        raise EngineError("limit_invalid", f"Размер страницы — от 1 до {MAX_PAGE}", field="limit")
    sql = "SELECT * FROM import_rows WHERE batch_id = ? AND row_no > ?"
    params: list[Any] = [batch_id, after]
    if action:
        sql += " AND action = ?"
        params.append(action)
    found = conn.execute(f"{sql} ORDER BY row_no LIMIT ?", (*params, limit + 1)).fetchall()
    items = [_row_view(item) for item in found[:limit]]
    return {"items": items, "more": len(found) > limit, "last": items[-1]["row"] if items else None}


def decide(tx: Tx, access: Access, batch_id: str, row_no: int, decision: str) -> dict[str, Any]:
    """Решение по спорной строке: пропустить или применить поверх чужой правки."""
    batch = row(tx.conn, access, batch_id)
    if batch["status"] != "planned":
        raise EngineError(
            "import_not_planned", "Решения принимаются по плану до применения", status=CONFLICT
        )
    found = tx.conn.execute(
        "SELECT * FROM import_rows WHERE batch_id = ? AND row_no = ?", (batch_id, row_no)
    ).fetchone()
    if found is None:
        raise not_found("import_row", str(row_no))
    if found["action"] != "conflict":
        raise EngineError(
            "import_not_conflict", "Решение нужно только спорной строке", status=CONFLICT
        )
    if decision == "apply" and found["reason"] != "version_conflict":
        raise EngineError(
            "import_decision",
            "Применить можно только строку, запись которой изменили после выгрузки;"
            " повтор ключа в книге можно только пропустить",
        )
    tx.conn.execute(
        "UPDATE import_rows SET decision = ? WHERE batch_id = ? AND row_no = ?",
        (decision, batch_id, row_no),
    )
    item = tx.conn.execute(
        "SELECT * FROM import_rows WHERE batch_id = ? AND row_no = ?", (batch_id, row_no)
    ).fetchone()
    return _row_view(item)


def _row_view(item: Any) -> dict[str, Any]:
    return {
        "row": item["row_no"],
        "action": item["action"],
        "record_id": item["record_id"],
        "cells": json.loads(item["cells"]),
        "changes": json.loads(item["changes"]) if item["changes"] else None,
        "reason": item["reason"],
        "message": item["message"],
        "decision": item["decision"],
    }


def expire(tx: Tx, folder: Path) -> int:
    """Просроченные пакеты: книга и строки плана удаляются, отчёт применённого остаётся."""
    now = text.now()
    old = [
        item["id"]
        for item in tx.conn.execute(
            "SELECT id FROM import_batches WHERE expires_at < ? AND"
            " (status <> 'applied' OR EXISTS (SELECT 1 FROM import_rows r"
            " WHERE r.batch_id = import_batches.id))",
            (now,),
        )
    ]
    for batch_id in old:
        (folder / f"{batch_id}.xlsx").unlink(missing_ok=True)
        tx.conn.execute("DELETE FROM import_rows WHERE batch_id = ?", (batch_id,))
    tx.conn.execute(
        "DELETE FROM import_batches WHERE expires_at < ? AND status <> 'applied'", (now,)
    )
    # Книги без пакета: запись пакета не удалась или файл остался от сбоя.
    limit = (datetime.now(UTC) - LIFETIME).timestamp()
    for path in folder.glob("*.xlsx*") if folder.is_dir() else []:
        if path.stat().st_mtime < limit:
            path.unlink(missing_ok=True)
    return len(old)


# Профили (IMP-08) -----------------------------------------------------------------


def signature(headers: list[str]) -> str:
    """Отпечаток вида книги: свёрнутые заголовки в порядке листа."""
    joined = "\x1f".join(text.fold(name).strip() for name in headers)
    return hashlib.sha256(joined.encode()).hexdigest()


def profiles(conn: Any, *, headers: list[str] | None = None) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM import_profiles", ()
    if headers is not None:
        sql, params = sql + " WHERE signature = ?", (signature(headers),)
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "settings": json.loads(item["settings"]),
            "created_by": item["created_by"],
            "updated_at": item["updated_at"],
        }
        for item in conn.execute(f"{sql} ORDER BY name_norm", params)
    ]


def save_profile(
    tx: Tx, access: Access, name: Any, headers: list[str], settings: dict[str, Any]
) -> dict[str, Any]:
    """Профиль с тем же названием заменяется: так профиль уточняют."""
    shown = text.clean_name(name, "Название профиля")
    kept = {key: settings[key] for key in settings if key not in ("sheet",)}
    existing = tx.conn.execute(
        "SELECT id FROM import_profiles WHERE name_norm = ?", (text.norm_name(shown),)
    ).fetchone()
    if existing:
        tx.conn.execute(
            "UPDATE import_profiles SET name = ?, signature = ?, settings = ?, updated_at = ?"
            " WHERE id = ?",
            (shown, signature(headers), dumps(kept), tx.now, existing["id"]),
        )
        profile_id = existing["id"]
    else:
        profile_id = text.new_id()
        tx.conn.execute(
            "INSERT INTO import_profiles (id, name, name_norm, signature, settings, created_by,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                profile_id,
                shown,
                text.norm_name(shown),
                signature(headers),
                dumps(kept),
                access.user_id,
                tx.now,
                tx.now,
            ),
        )
    tx.audit("import.profile", "import_profile", profile_id, details={"name": shown})
    return {"id": profile_id, "name": shown}


def delete_profile(tx: Tx, access: Access, profile_id: str) -> None:
    found = tx.conn.execute("SELECT * FROM import_profiles WHERE id = ?", (profile_id,)).fetchone()
    if found is None:
        raise not_found("import_profile", profile_id)
    if found["created_by"] != access.user_id and not access.has("admin"):
        raise EngineError(
            "forbidden", "Удалить профиль может его автор или администратор", status=FORBIDDEN
        )
    tx.conn.execute("DELETE FROM import_profiles WHERE id = ?", (profile_id,))
    tx.audit("import.profile_delete", "import_profile", profile_id, details={"name": found["name"]})
