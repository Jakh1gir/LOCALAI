"""Нумерованные миграции схемы с проверенной копией перед изменением (DATA-11)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from engine import text
from engine.database import Database
from engine.errors import EngineError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


def available() -> list[tuple[int, str, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _NAME_RE.match(path.name)
        if not match:
            raise EngineError("migration_name", f"Неверное имя миграции: {path.name}")
        found.append((int(match.group(1)), match.group(2), path))
    numbers = [number for number, _, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise EngineError("migration_gap", "Номера миграций идут не подряд")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def migrate(db: Database, *, backup_dir: str | Path | None = None) -> list[int]:
    """Применяет недостающие миграции. Возвращает номера применённых.

    Перед изменением существующей базы создаётся её копия и проверяется
    ``PRAGMA integrity_check``; без исправной копии миграция не начинается.
    """
    conn = db.connect()
    version = current_version(conn)
    migrations = available()
    latest = migrations[-1][0] if migrations else 0
    if version > latest:
        raise EngineError(
            "schema_newer",
            f"База создана более новой версией программы (схема {version}, "
            f"программа знает {latest}). Обновите программу.",
        )
    pending = [item for item in migrations if item[0] > version]
    if not pending:
        return []
    if version > 0:
        _backup(conn, db.path, version, backup_dir)
    applied = []
    for number, name, path in pending:
        _apply(conn, number, name, path.read_text(encoding="utf-8"))
        applied.append(number)
    conn.execute(
        "INSERT OR IGNORE INTO app_meta (key, value) VALUES ('database_id', ?)",
        (text.new_id(),),
    )
    return applied


def _apply(conn: sqlite3.Connection, number: int, name: str, sql: str) -> None:
    script = (
        "BEGIN IMMEDIATE;\n"
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);\n"
        f"{sql}\n"
        f"INSERT INTO schema_migrations VALUES ({number}, '{name}', '{text.now()}');\n"
        "COMMIT;"
    )
    try:
        conn.executescript(script)
    except sqlite3.Error as error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise EngineError(
            "migration_failed", f"Миграция {number:04d}_{name} не применена: {error}"
        ) from error


def _backup(
    conn: sqlite3.Connection, db_path: str, version: int, backup_dir: str | Path | None
) -> Path:
    source = Path(db_path)
    folder = Path(backup_dir) if backup_dir else source.parent
    folder.mkdir(parents=True, exist_ok=True)
    stamp = text.now().replace(":", "").replace("-", "").replace(".", "")
    target = folder / f"{source.stem}.schema{version:04d}.{stamp}.sqlite3"
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
        verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copy.close()
    if verdict != "ok":
        target.unlink(missing_ok=True)
        raise EngineError(
            "backup_failed", f"Копия перед миграцией не прошла проверку: {verdict}"
        )
    return target
