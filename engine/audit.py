"""Чтение журнала аудита. Запись в журнал делает ``Tx.audit`` внутри транзакции."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

MAX_LIMIT = 200


def list_events(
    conn: sqlite3.Connection,
    *,
    entity_id: str | None = None,
    dataset_id: str | None = None,
    before_seq: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """События от новых к старым; ``before_seq`` — курсор следующей страницы."""
    where, params = ["1 = 1"], []
    if entity_id:
        where.append("entity_id = ?")
        params.append(entity_id)
    if dataset_id:
        where.append("dataset_id = ?")
        params.append(dataset_id)
    if before_seq:
        where.append("seq < ?")
        params.append(before_seq)
    rows = conn.execute(
        f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
        (*params, max(1, min(limit, MAX_LIMIT))),
    ).fetchall()
    return [{**dict(row), "details": json.loads(row["details"])} for row in rows]
