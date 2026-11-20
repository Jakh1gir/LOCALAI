"""Листы схем: граф элементов и соединений, пути, сопоставление с ККС (ТЗ 10)."""

from __future__ import annotations

from typing import Any

from engine import sheets
from server import schemas
from server.api import Call, Endpoint

PAGE = "/documents/{id}/sheets/{page}"


def listing(call: Call) -> dict[str, Any]:
    """Листы действующей версии документа: размер, источник, число элементов и соединений."""
    return sheets.listing(call.conn, call.access, call.params["id"])


def sheet(call: Call) -> dict[str, Any]:
    """Лист целиком для просмотра (VIS-09): элементы с рамками, ККС и соединения."""
    return sheets.sheet(call.conn, call.access, call.params["id"], call.params["page"])


def connections(call: Call) -> dict[str, Any]:
    """Что соединено с элементом листа — на глубину до 5 шагов (VIS-06)."""
    return sheets.connections(
        call.conn, call.access, call.params["id"], call.params["page"],
        call.query.get("element"), call.query_int("depth", 1),
    )  # fmt: skip


def path(call: Call) -> dict[str, Any]:
    """Кратчайший путь между двумя элементами листа — для ответа и подсветки."""
    return sheets.path(
        call.conn, call.access, call.params["id"], call.params["page"],
        call.query.get("from"), call.query.get("to"),
    )  # fmt: skip


def decide(call: Call) -> dict[str, Any]:
    """Подтвердить или отклонить сопоставление элемента с ККС (VIS-05)."""
    return sheets.decide(
        call.tx, call.access, call.params["id"], call.params["page"], call.params["eid"],
        call.body.decision == "confirm",
    )  # fmt: skip


def occurrences(call: Call) -> dict[str, Any]:
    """Листы, на которых встречается объект ККС."""
    return sheets.occurrences(call.conn, call.access, call.params["id"])


def _e(method: str, path_: str, handler: Any, permission: str, **extra: Any) -> Endpoint:
    return Endpoint(method, path_, handler, permission, tag="Схемы", **extra)


ENDPOINTS = [
    _e("GET", "/documents/{id}/sheets", listing, "session"),
    _e("GET", PAGE, sheet, "session"),
    _e("GET", PAGE + "/connections", connections, "session", query=("element", "depth")),
    _e("GET", PAGE + "/path", path, "session", query=("from", "to")),
    _e(
        "POST",
        PAGE + "/elements/{eid}/decision",
        decide,
        "section.documents",
        body=schemas.SheetDecision,
    ),
    _e("GET", "/kks/{id}/sheets", occurrences, "session"),
]
