"""Граф листа схемы: хранение и запросы (ТЗ 10, VIS-04…06, VIS-09).

Граф строит процесс схем (vision) и отдаёт его вместе с текстом страницы
(``POST /jobs/{id}/complete``, поле ``sheets``). Здесь он хранится и
отвечает на вопросы ИИ и экрана просмотра листа:

- что соединено с элементом — на глубину до ``MAX_DEPTH`` шагов;
- по какому пути соединены два элемента — кратчайший путь для подсветки;
- на каких листах встречается объект ККС.

Код ККС в надписи элемента сопоставляется с каталогом (VIS-05) с
уверенностью: 1 — код, 0,9 — псевдоним, 0,8 — код сигнала. Сопоставление —
предложение: привязкой документа к ККС оно становится после подтверждения
человеком. Если документ уже привязан к этому объекту, сопоставление
подтверждено сразу.

Права — как у документа: лист не виден тому, кто не видит документ.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import deque
from itertools import pairwise
from typing import Any

from engine import documents, kks_rules, structure, text
from engine.access import Access
from engine.database import Tx
from engine.errors import EngineError, not_found

MAX_SHEETS = 500
MAX_ELEMENTS = 20000
MAX_EDGES = 40000
MAX_DEPTH = 5
KINDS = ("label", "symbol", "terminal", "wire", "other")
_CODE = re.compile(r"(?<![\w-])(?=[\w-]*\d)(?=[\w-]*[^\W\d_])[\w][\w.-]{3,39}(?![\w-])")
_CONFIDENCE = {"code": 1.0, "alias": 0.9, "signal": 0.8}


# Запись результата разбора ---------------------------------------------------------


def store(tx: Tx, document_id: str, version: int, sheets: list[dict[str, Any]]) -> int:
    """Сохранить листы версии документа (повторный разбор заменяет прежний); число элементов."""
    row = tx.conn.execute(
        "SELECT seq FROM documents WHERE id = ? AND version = ?", (document_id, version)
    ).fetchone()
    if row is None:
        raise EngineError("document_version_gone", "Документ заменён новой версией")
    if len(sheets) > MAX_SHEETS:
        raise EngineError("sheets_too_many", f"Листов больше {MAX_SHEETS}", field="sheets")
    tx.conn.execute(
        "DELETE FROM sheets WHERE document_seq = ? AND version = ?", (row["seq"], version)
    )
    linked = {
        item["target_id"]
        for item in tx.conn.execute(
            "SELECT target_id FROM document_links WHERE document_seq = ? AND target_kind = 'kks'",
            (row["seq"],),
        )
    }
    sites = [item["id"] for item in structure.list_sites(tx.conn) if not item["archived_at"]]
    total = 0
    for sheet in sheets:
        total += _store_sheet(tx, row["seq"], version, sheet, sites, linked)
    return total


def _store_sheet(
    tx: Tx, document_seq: int, version: int, sheet: dict[str, Any], sites: list[str],
    linked: set[str],
) -> int:  # fmt: skip
    elements, edges = sheet.get("elements") or [], sheet.get("edges") or []
    if len(elements) > MAX_ELEMENTS or len(edges) > MAX_EDGES:
        raise EngineError("sheet_too_large", "Лист слишком велик", field="sheets")
    cursor = tx.conn.execute(
        "INSERT INTO sheets (document_seq, version, page, width, height, parser, source,"
        " created_at) VALUES (?,?,?,?,?,?,?,?)",
        (document_seq, version, str(sheet["page"]), float(sheet["width"]), float(sheet["height"]),
         str(sheet.get("parser") or "vision"), sheet.get("source") or "vector", tx.now),
    )  # fmt: skip
    sheet_seq = cursor.lastrowid
    known: set[str] = set()
    for item in elements:
        eid = str(item["id"])
        kind = item.get("kind") if item.get("kind") in KINDS else "other"
        x0, y0, x1, y1 = (float(value) for value in item["bbox"])
        kks_id, confidence = _match(tx.conn, item.get("label"), sites)
        state = None
        if kks_id:
            state = "confirmed" if kks_id in linked else "proposed"
        tx.conn.execute(
            "INSERT OR REPLACE INTO sheet_elements (sheet_seq, eid, kind, label, x0, y0, x1, y1,"
            " kks_id, confidence, kks_state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sheet_seq, eid, kind, (item.get("label") or None), min(x0, x1), min(y0, y1),
             max(x0, x1), max(y0, y1), kks_id, confidence, state),
        )  # fmt: skip
        known.add(eid)
    for edge in edges:
        a, b = sorted((str(edge["from"]), str(edge["to"])))
        if a == b or a not in known or b not in known:
            continue
        points = edge.get("points")
        tx.conn.execute(
            "INSERT OR IGNORE INTO sheet_edges (sheet_seq, a, b, kind, points) VALUES (?,?,?,?,?)",
            (sheet_seq, a, b, edge.get("kind"), json.dumps(points) if points else None),
        )
    return len(known)


def _match(conn: sqlite3.Connection, label: Any, sites: list[str]) -> tuple[str | None, Any]:
    """Лучшее совпадение надписи с каталогом: код важнее псевдонима, псевдоним — сигнала."""
    if not isinstance(label, str):
        return None, None
    best: tuple[str | None, Any] = (None, None)
    for code in _CODE.findall(label)[:5]:
        for site_id in sites:
            try:
                found = kks_rules.resolve(conn, site_id, code)
            except EngineError:
                continue
            if found is not None:
                confidence = _CONFIDENCE.get(found.get("via"), 0.8)
                if best[1] is None or confidence > best[1]:
                    best = (found["id"], confidence)
    return best


# Просмотр листа (VIS-09) -------------------------------------------------------------


def listing(conn: sqlite3.Connection, viewer: Access, document_id: str) -> dict[str, Any]:
    document = documents.get(conn, viewer, document_id)
    rows = conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM sheet_elements e WHERE e.sheet_seq = s.seq) AS elements,"
        " (SELECT COUNT(*) FROM sheet_edges e WHERE e.sheet_seq = s.seq) AS edges,"
        " (SELECT COUNT(*) FROM sheet_elements e WHERE e.sheet_seq = s.seq"
        "  AND e.kks_state = 'proposed') AS proposed"
        " FROM sheets s JOIN documents d ON d.seq = s.document_seq"
        " WHERE d.id = ? AND s.version = d.version ORDER BY s.seq",
        (document_id,),
    ).fetchall()
    return {
        "document_id": document_id,
        "version": document["version"],
        "items": [
            {
                "page": row["page"],
                "width": row["width"],
                "height": row["height"],
                "source": row["source"],
                "parser": row["parser"],
                "elements": row["elements"],
                "edges": row["edges"],
                "proposed": row["proposed"],
            }
            for row in rows
        ],
    }


def sheet(conn: sqlite3.Connection, viewer: Access, document_id: str, page: str) -> dict[str, Any]:
    found = _sheet(conn, viewer, document_id, page)
    elements = [_element(item) for item in _elements(conn, found["seq"])]
    edges = [_edge(item) for item in conn.execute(
        "SELECT * FROM sheet_edges WHERE sheet_seq = ?", (found["seq"],))]  # fmt: skip
    return _sheet_view(found, document_id) | {"elements": elements, "edges": edges}


# Запросы по графу (VIS-06) ----------------------------------------------------------


def connections(
    conn: sqlite3.Connection, viewer: Access, document_id: str, page: str, ref: Any, depth: int = 1
) -> dict[str, Any]:
    """Всё, что соединено с элементом, на глубину ``depth``; ``distance`` — число шагов."""
    if not isinstance(depth, int) or not 1 <= depth <= MAX_DEPTH:
        raise EngineError("sheet_depth", f"Глубина — от 1 до {MAX_DEPTH}", field="depth")
    found = _sheet(conn, viewer, document_id, page)
    graph, elements = _graph(conn, found["seq"])
    start = _find(elements, ref)
    distance = {start: 0}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if distance[current] == depth:
            continue
        for other in graph.get(current, ()):
            if other not in distance:
                distance[other] = distance[current] + 1
                queue.append(other)
    edges = [
        _edge(item)
        for item in conn.execute("SELECT * FROM sheet_edges WHERE sheet_seq = ?", (found["seq"],))
        if item["a"] in distance and item["b"] in distance
    ]
    return _sheet_view(found, document_id) | {
        "start": start,
        "elements": [
            _element(elements[eid]) | {"distance": steps}
            for eid, steps in sorted(distance.items(), key=lambda item: (item[1], item[0]))
        ],
        "edges": edges,
    }


def path(
    conn: sqlite3.Connection, viewer: Access, document_id: str, page: str, start: Any, end: Any
) -> dict[str, Any]:
    """Кратчайший путь между двумя элементами листа — для ответа и подсветки."""
    found = _sheet(conn, viewer, document_id, page)
    graph, elements = _graph(conn, found["seq"])
    a, b = _find(elements, start), _find(elements, end)
    previous: dict[str, str | None] = {a: None}
    queue = deque([a])
    while queue and b not in previous:
        current = queue.popleft()
        for other in graph.get(current, ()):
            if other not in previous:
                previous[other] = current
                queue.append(other)
    if b not in previous:
        return _sheet_view(found, document_id) | {"found": False, "elements": [], "edges": []}
    chain = [b]
    while previous[chain[-1]] is not None:
        chain.append(previous[chain[-1]])  # type: ignore[arg-type]
    chain.reverse()
    pairs = {tuple(sorted(pair)) for pair in pairwise(chain)}
    edges = [
        _edge(item)
        for item in conn.execute("SELECT * FROM sheet_edges WHERE sheet_seq = ?", (found["seq"],))
        if (item["a"], item["b"]) in pairs
    ]
    return _sheet_view(found, document_id) | {
        "found": True,
        "elements": [_element(elements[eid]) for eid in chain],
        "edges": edges,
    }


def occurrences(conn: sqlite3.Connection, viewer: Access, kks_id: str) -> dict[str, Any]:
    """На каких листах встречается объект ККС (действующие версии видимых документов)."""
    rows = conn.execute(
        "SELECT d.id, d.title, d.type_id, s.page, s.source, e.eid, e.kks_state, e.confidence"
        " FROM sheet_elements e JOIN sheets s ON s.seq = e.sheet_seq"
        " JOIN documents d ON d.seq = s.document_seq"
        " WHERE e.kks_id = ? AND s.version = d.version AND d.archived_at IS NULL"
        " AND e.kks_state <> 'rejected' ORDER BY d.title, s.seq",
        (kks_id,),
    ).fetchall()
    return {
        "items": [
            {"document_id": row["id"], "title": row["title"], "page": row["page"],
             "source": row["source"], "element": row["eid"], "state": row["kks_state"],
             "confidence": row["confidence"]}
            for row in rows
            if viewer.sees_documents(row["type_id"])
        ]
    }  # fmt: skip


def decide(
    tx: Tx, viewer: Access, document_id: str, page: str, eid: str, confirm: bool
) -> dict[str, Any]:
    """Решение по сопоставлению с ККС: подтверждение привязывает документ к объекту (VIS-05)."""
    found = _sheet(tx.conn, viewer, document_id, page)
    row = tx.conn.execute(
        "SELECT * FROM sheet_elements WHERE sheet_seq = ? AND eid = ?", (found["seq"], eid)
    ).fetchone()
    if row is None or row["kks_id"] is None:
        raise not_found("sheet_element", eid)
    if confirm:
        documents.link(tx, viewer, document_id, "kks", row["kks_id"])
    else:
        documents.check_link(tx.conn, viewer, document_id, "kks", row["kks_id"])  # то же право
    tx.conn.execute(
        "UPDATE sheet_elements SET kks_state = ? WHERE sheet_seq = ? AND eid = ?",
        ("confirmed" if confirm else "rejected", found["seq"], eid),
    )
    tx.audit("sheet.kks", "document", document_id,
             details={"page": page, "element": eid, "confirm": confirm})  # fmt: skip
    updated = tx.conn.execute(
        "SELECT e.*, k.code FROM sheet_elements e LEFT JOIN kks_objects k ON k.id = e.kks_id"
        " WHERE e.sheet_seq = ? AND e.eid = ?",
        (found["seq"], eid),
    ).fetchone()
    return _element(updated)


# Внутреннее -------------------------------------------------------------------------


def _sheet(conn: sqlite3.Connection, viewer: Access, document_id: str, page: str) -> sqlite3.Row:
    documents.get(conn, viewer, document_id)  # 404 без права на тип документа
    row = conn.execute(
        "SELECT s.*, d.title FROM sheets s JOIN documents d ON d.seq = s.document_seq"
        " WHERE d.id = ? AND s.version = d.version AND s.page = ?",
        (document_id, str(page)),
    ).fetchone()
    if row is None:
        raise not_found("sheet", f"{document_id}/{page}")
    return row


def _elements(conn: sqlite3.Connection, sheet_seq: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT e.*, k.code FROM sheet_elements e LEFT JOIN kks_objects k ON k.id = e.kks_id"
        " WHERE e.sheet_seq = ? ORDER BY e.eid",
        (sheet_seq,),
    ).fetchall()


def _graph(
    conn: sqlite3.Connection, sheet_seq: int
) -> tuple[dict[str, list[str]], dict[str, sqlite3.Row]]:
    elements = {row["eid"]: row for row in _elements(conn, sheet_seq)}
    graph: dict[str, list[str]] = {}
    for row in conn.execute("SELECT a, b FROM sheet_edges WHERE sheet_seq = ?", (sheet_seq,)):
        graph.setdefault(row["a"], []).append(row["b"])
        graph.setdefault(row["b"], []).append(row["a"])
    return graph, elements


def _find(elements: dict[str, sqlite3.Row], ref: Any) -> str:
    """Элемент по номеру, коду ККС или надписи (без учёта регистра)."""
    if not isinstance(ref, str) or not ref.strip():
        raise EngineError("sheet_element_required", "Укажите элемент листа", field="element")
    if ref in elements:
        return ref
    wanted = text.fold(ref.strip())
    for key in ("code", "label"):
        for eid, row in elements.items():
            if row[key] and text.fold(row[key]) == wanted:
                return eid
    for eid, row in elements.items():
        if row["label"] and wanted in text.fold(row["label"]):
            return eid
    raise not_found("sheet_element", ref)


def _sheet_view(row: sqlite3.Row, document_id: str) -> dict[str, Any]:
    return {"document_id": document_id, "title": row["title"], "page": row["page"],
            "width": row["width"],
            "height": row["height"], "source": row["source"], "parser": row["parser"]}  # fmt: skip


def _element(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["eid"],
        "kind": row["kind"],
        "label": row["label"],
        "bbox": [row["x0"], row["y0"], row["x1"], row["y1"]],
        "kks": (
            {"id": row["kks_id"], "code": row["code"], "state": row["kks_state"],
             "confidence": row["confidence"]}
            if row["kks_id"] else None
        ),
    }  # fmt: skip


def _edge(row: sqlite3.Row) -> dict[str, Any]:
    return {"from": row["a"], "to": row["b"], "kind": row["kind"],
            "points": json.loads(row["points"]) if row["points"] else None}  # fmt: skip
