"""Очередь фоновых задач (ТЗ 4.1 `jobs`, VIS-01, VIS-02, KNOW-01).

Задачу берёт исполнитель своего вида: ``server`` — сам сервер (простые
форматы), ``vision`` — процесс схем (PDF, чертежи, сканы). Захват — аренда
на ``LEASE``: если исполнитель пропал, задача по истечении аренды
возвращается в очередь. Попыток — не больше ``max_attempts``; после
последней неудачи задача остаётся с ошибкой, и её видно пользователю.

Задача относится к версии цели (``target_version``): если документ заменили,
задачи прежней версии отменяются, а результат устаревшей задачи не
принимается.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from engine import text
from engine.database import Tx
from engine.errors import CONFLICT, EngineError, not_found

LEASE = timedelta(minutes=10)
READERS = ("server", "vision")
STATES = ("queued", "running", "done", "error", "cancelled")


def enqueue(tx: Tx, kind: str, reader: str, target_id: str, target_version: int) -> dict[str, Any]:
    """Новая задача; незавершённые задачи той же цели отменяются."""
    cancel_for(tx, target_id)
    job_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO jobs (id, kind, reader, target_id, target_version, state, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'queued', ?)",
        (job_id, kind, reader, target_id, target_version, tx.now),
    )
    return get(tx.conn, job_id)


def cancel_for(tx: Tx, target_id: str) -> int:
    return tx.conn.execute(
        "UPDATE jobs SET state = 'cancelled', finished_at = ?, lease_until = NULL"
        " WHERE target_id = ? AND state IN ('queued', 'running')",
        (tx.now, target_id),
    ).rowcount


def claim(
    tx: Tx, reader: str, worker: str, kinds: list[str] | None = None
) -> dict[str, Any] | None:
    """Взять старейшую задачу: из очереди или с истёкшей арендой."""
    if reader not in READERS:
        raise EngineError(
            "job_reader", f"Исполнитель — один из: {', '.join(READERS)}", field="reader"
        )
    now = tx.now
    sql = (
        "SELECT id FROM jobs WHERE reader = ? AND attempts < max_attempts AND"
        " (state = 'queued' OR (state = 'running' AND lease_until < ?))"
    )
    params: list[Any] = [reader, now]
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    row = tx.conn.execute(sql + " ORDER BY created_at LIMIT 1", params).fetchone()
    if row is None:
        return None
    lease = (datetime.now(UTC) + LEASE).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    tx.conn.execute(
        "UPDATE jobs SET state = 'running', attempts = attempts + 1, worker = ?, lease_until = ?,"
        " started_at = ?, error = NULL WHERE id = ?",
        (worker, lease, now, row["id"]),
    )
    return get(tx.conn, row["id"])


def finish(tx: Tx, job_id: str, worker: str) -> dict[str, Any]:
    job = held(tx.conn, job_id, worker)
    tx.conn.execute(
        "UPDATE jobs SET state = 'done', finished_at = ?, lease_until = NULL WHERE id = ?",
        (tx.now, job["id"]),
    )
    return get(tx.conn, job_id)


def fail(tx: Tx, job_id: str, worker: str, error: str, *, retry: bool) -> dict[str, Any]:
    """Неудача: в очередь снова, если попытки остались и ошибка не окончательная."""
    job = held(tx.conn, job_id, worker)
    again = retry and job["attempts"] < job["max_attempts"]
    tx.conn.execute(
        "UPDATE jobs SET state = ?, error = ?, lease_until = NULL, worker = NULL,"
        " finished_at = ? WHERE id = ?",
        ("queued" if again else "error", error[:2000], None if again else tx.now, job["id"]),
    )
    return get(tx.conn, job_id)


def get(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise not_found("job", job_id)
    return dict(row)


def active_for(conn: sqlite3.Connection, target_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM jobs WHERE target_id = ? AND state IN ('queued', 'running')"
        " ORDER BY created_at DESC LIMIT 1",
        (target_id,),
    ).fetchone()
    return dict(row) if row else None


def listing(
    conn: sqlite3.Connection, *, state: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM jobs", []
    if state:
        if state not in STATES:
            raise EngineError(
                "job_state", f"Состояние — одно из: {', '.join(STATES)}", field="state"
            )
        sql += " WHERE state = ?"
        params.append(state)
    return [
        dict(row)
        for row in conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*params, limit))
    ]


def held(conn: sqlite3.Connection, job_id: str, worker: str) -> dict[str, Any]:
    job = get(conn, job_id)
    if job["state"] != "running" or job["worker"] != worker:
        raise EngineError(
            "job_not_held",
            "Задача не у этого исполнителя: аренда истекла или задача отменена",
            status=CONFLICT,
        )
    return job
