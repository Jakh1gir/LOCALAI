"""Журнал и состояние сервера."""

from __future__ import annotations

from typing import Any

import engine
from engine import audit, migrator
from server.api import Call, Endpoint


def list_audit(call: Call) -> dict[str, Any]:
    """События журнала от новых к старым."""
    before = call.query_int("before", 0) or None
    return {"items": audit.list_events(
        call.conn, entity_id=call.query.get("entity_id"), dataset_id=call.query.get("dataset_id"),
        before_seq=before, limit=call.query_int("limit", 50))}  # fmt: skip


def health(call: Call) -> dict[str, Any]:
    """Состояние сервера и базы."""
    row = call.conn.execute("SELECT value FROM app_meta WHERE key = 'database_id'").fetchone()
    return {
        "status": "ok",
        "version": engine.__version__,
        "schema_version": migrator.current_version(call.conn),
        "database_id": row[0] if row else None,
    }


ENDPOINTS = [
    Endpoint("GET", "/audit", list_audit, "audit.read", query=("entity_id", "dataset_id", "before",
             "limit"), tag="Журнал"),  # fmt: skip
    Endpoint("GET", "/system/health", health, "public", tag="Система"),
]
