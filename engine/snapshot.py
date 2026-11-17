"""Проверенная копия базы: перед миграцией, перед импортом, перед восстановлением."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from engine import text
from engine.errors import EngineError


def take(conn: sqlite3.Connection, db_path: str, label: str, folder: str | Path | None) -> Path:
    """Копирует базу через API резервного копирования SQLite и проверяет копию.

    Копия согласована даже при одновременной записи. Если ``PRAGMA
    integrity_check`` копии не «ok», копия удаляется и действие не начинается.
    """
    source = Path(db_path)
    target_dir = Path(folder) if folder else source.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = text.now().replace(":", "").replace("-", "").replace(".", "")
    target = target_dir / f"{source.stem}.{label}.{stamp}.sqlite3"
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
        verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copy.close()
    if verdict != "ok":
        target.unlink(missing_ok=True)
        raise EngineError("backup_failed", f"Копия базы не прошла проверку: {verdict}")
    return target
