"""Общий поиск по базе (ТЗ 5.1)."""

from __future__ import annotations

from typing import Any

from engine import EngineError, ai_memory
from engine import search as engine_search
from server import ai_link
from server.api import Call, Endpoint

QUERY_TIMEOUT = 5.0  # вектор строки поиска: модель векторов на процессоре — доли секунды


def search(call: Call) -> dict[str, Any]:
    """ККС, записи, документы, заметки и словарь по одной строке.

    ``mode=hybrid`` — документы ищутся и по смыслу (AI-05): вектор строки
    считает ИИ-служба. Служба или модель векторов недоступны — поиск идёт по
    словам, а ``meaning: false`` говорит, что смысл не участвовал.
    """
    mode = call.query.get("mode") or "text"
    if mode not in ("text", "hybrid"):
        raise EngineError("search_mode", "Режим поиска: text или hybrid", field="mode")
    limit = call.query_int("limit", engine_search.LIMIT)
    needle = call.query.get("q", "")
    meaning = _meaning(call, needle, limit) if mode == "hybrid" else None
    return engine_search.search(
        call.conn,
        call.access,
        needle,
        limit=limit,
        site_id=call.query.get("site_id") or None,
        meaning=meaning,
    )


def _meaning(call: Call, needle: str, limit: int) -> list[tuple[int, float]] | None:
    index = call.vectors
    if call.ai is None or index is None or not index.available or not needle.strip():
        return None
    config = ai_memory.settings(call.conn, secrets=True).get("embedding")
    try:
        found = call.ai.embed([needle.strip()[:2000]], config, timeout=QUERY_TIMEOUT)
    except ai_link.AiUnavailable:
        return None
    if not found.get("model") or not found.get("vectors"):
        return None
    return index.search(call.conn, found["model"], found["vectors"][0], limit * 5)


ENDPOINTS = [
    Endpoint(
        "GET", "/search", search, "session", query=("q", "limit", "site_id", "mode"), tag="Поиск"
    ),
]
