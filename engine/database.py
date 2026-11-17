"""Подключение к базе и транзакции (DATA-10, принцип «одно действие — одна транзакция»)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine import text
from engine.errors import EngineError

MIN_SQLITE = (3, 45, 0)
BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class Actor:
    """Кто выполняет действие. ``id`` — учётная запись, ``label`` — имя для журнала."""

    id: str | None
    label: str
    origin: str = "user"


SYSTEM = Actor(None, "Система", "system")


class Tx:
    """Открытая транзакция записи: соединение, автор и единое время действия."""

    def __init__(self, conn: sqlite3.Connection, actor: Actor) -> None:
        self.conn = conn
        self.actor = actor
        self.now = text.now()

    def audit(
        self,
        action: str,
        entity_kind: str,
        entity_id: str | None,
        *,
        dataset_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Строка журнала в той же транзакции, что и само изменение."""
        self.conn.execute(
            "INSERT INTO audit_log (at, actor_id, actor_label, origin, action,"
            " entity_kind, entity_id, dataset_id, details) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.now,
                self.actor.id,
                self.actor.label,
                self.actor.origin,
                action,
                entity_kind,
                entity_id,
                dataset_id,
                dumps(details or {}),
            ),
        )


def dumps(value: Any) -> str:
    """Компактный JSON с кириллицей как есть и устойчивым порядком ключей."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Database:
    """Файл SQLite. У каждого потока своё соединение; пишет один поток за раз."""

    def __init__(self, path: str | Path) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            need = ".".join(map(str, MIN_SQLITE))
            raise EngineError(
                "sqlite_too_old",
                f"Нужен SQLite {need} или новее, найден {sqlite3.sqlite_version}",
            )
        self.path = str(path)
        self._local = threading.local()
        # Потоки одного процесса ждут здесь, а не в цикле занятости SQLite.
        self._write_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _open(self.path)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Чтение из одного согласованного снимка базы."""
        conn = self.connect()
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("ROLLBACK")

    @contextmanager
    def write(self, actor: Actor) -> Iterator[Tx]:
        """Транзакция записи: либо всё вместе с аудитом, либо ничего."""
        conn = self.connect()
        if conn.in_transaction:
            raise EngineError("nested_write", "Транзакция записи уже открыта")
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield Tx(conn, actor)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -32000")
    conn.create_function("lai_fold", 1, _sql_fold, deterministic=True)
    return conn


def _sql_fold(value: object) -> str | None:
    return None if value is None else text.fold(value)
