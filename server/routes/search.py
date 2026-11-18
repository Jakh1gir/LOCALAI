"""Общий поиск по базе (ТЗ 5.1)."""

from __future__ import annotations

from typing import Any

from engine import search as engine_search
from server.api import Call, Endpoint


def search(call: Call) -> dict[str, Any]:
    """ККС и записи всех видимых реестров по одной строке; документы и заметки — с этапа 4."""
    return engine_search.search(
        call.conn,
        call.access,
        call.query.get("q", ""),
        limit=call.query_int("limit", engine_search.LIMIT),
        site_id=call.query.get("site_id") or None,
    )


ENDPOINTS = [
    Endpoint("GET", "/search", search, "session", query=("q", "limit", "site_id"), tag="Поиск"),
]
