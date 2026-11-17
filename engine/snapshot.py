"""Проверенная копия базы: перед миграцией, перед импортом, перед восстановлением."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

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


def copy_untouched(
    path: Path,
    target: Path,
    prepare: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    """Согласованная копия чужой базы, после которой рядом с ней не появляется файлов.

    - Журнала WAL нет: база закрыта и открывается как неизменяемая.
    - Журнал есть, файла общей памяти нет: база закрыта аварийно; база и
      журнал копируются как файлы и читаются в копии.
    - Есть оба файла: база открыта другой программой; чтение на месте, как у
      любого читателя, без новых файлов.

    Путь передаётся в SQLite через URI с экранированием, поэтому символы
    вроде «#» и «?» в имени файла не обрезают его. ``prepare`` регистрирует
    функции, без которых схема источника не читается.
    """
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    if not wal.exists():
        options = "mode=ro&immutable=1"
    elif not shm.exists():
        staged = target.parent / f"{target.stem}.staged"
        staged.mkdir()
        shutil.copy2(path, staged / path.name)
        shutil.copy2(wal, staged / wal.name)
        path, options = staged / path.name, "mode=rw"
    else:
        options = "mode=ro"
    source = sqlite3.connect(f"file:{quote(str(path.resolve()))}?{options}", uri=True)
    try:
        if prepare is not None:
            prepare(source)
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
