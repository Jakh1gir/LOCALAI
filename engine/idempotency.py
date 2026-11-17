"""Ключи идемпотентности: повтор запроса возвращает прежний результат (API-06)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from engine.database import Tx, dumps
from engine.errors import CONFLICT, EngineError

MAX_KEY = 128


def run(tx: Tx, scope: str, key: str | None, request: Any, action: Callable[[], Any]) -> Any:
    """Выполняет ``action`` один раз на пару (область, ключ).

    Результат сохраняется в той же транзакции, что и само изменение. Тот же
    ключ с другим содержимым запроса — ошибка, а не тихая подмена.
    """
    if key is None:
        return action()
    if not isinstance(key, str) or not 1 <= len(key) <= MAX_KEY:
        raise EngineError("idempotency_key", f"Ключ идемпотентности: до {MAX_KEY} символов")
    digest = hashlib.sha256(dumps(request).encode()).hexdigest()
    row = tx.conn.execute(
        "SELECT request_hash, response FROM idempotency_keys WHERE scope = ? AND key = ?",
        (scope, key),
    ).fetchone()
    if row is not None:
        if row["request_hash"] != digest:
            raise EngineError(
                "idempotency_mismatch",
                "Этот ключ идемпотентности уже использован для другого запроса",
                status=CONFLICT,
            )
        return json.loads(row["response"])
    result = action()
    tx.conn.execute(
        "INSERT INTO idempotency_keys (scope, key, request_hash, response, created_at)"
        " VALUES (?,?,?,?,?)",
        (scope, key, digest, dumps(result), tx.now),
    )
    return result


def purge(tx: Tx, older_than: str) -> int:
    """Удаляет ключи старше указанного времени; вызывается обслуживанием сервера."""
    cursor = tx.conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?", (older_than,))
    return cursor.rowcount
