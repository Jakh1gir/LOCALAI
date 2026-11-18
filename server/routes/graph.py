"""Граф объектов: окрестность узла и путь между узлами (DATA-15)."""

from __future__ import annotations

from typing import Any

from engine import graph, structure
from server.api import Call, Endpoint


def _visible(call: Call):  # type: ignore[no-untyped-def]
    """Скрытые поля реестра или ``None``, если реестр роли не виден."""
    cache: dict[str, frozenset[str] | None] = {}

    def visible(dataset_id: str) -> frozenset[str] | None:
        if dataset_id not in cache:
            if not call.access.can(dataset_id, "view"):
                cache[dataset_id] = None
            else:
                dataset = structure.load_dataset(call.conn, dataset_id)
                schema = structure.load_schema(call.conn, dataset["type_id"])
                cache[dataset_id] = call.access.hidden_keys(schema)
        return cache[dataset_id]

    return visible


def neighbourhood(call: Call) -> dict[str, Any]:
    """Всё, что связано с объектом ККС или записью, на заданную глубину."""
    depth = call.query_int("depth", 2)
    return graph.neighbourhood(call.conn, call.query.get("node"), _visible(call), depth)


def path(call: Call) -> dict[str, Any]:
    """Кратчайший путь между двумя узлами графа."""
    return graph.path(call.conn, call.query.get("from"), call.query.get("to"), _visible(call))


ENDPOINTS = [
    Endpoint(
        "GET", "/graph/neighbourhood", neighbourhood, "session", query=("node", "depth"), tag="Граф"
    ),
    Endpoint("GET", "/graph/path", path, "session", query=("from", "to"), tag="Граф"),
]
