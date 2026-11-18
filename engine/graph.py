"""Граф объектов: объекты ККС и записи — узлы, связи ККС и ссылки — рёбра (DATA-15).

Узел ``kks:<UUID>`` — объект каталога, ``record:<UUID>`` — запись. Ребро идёт
от записи к объекту ККС (роль и поле связи) или от записи к записи (поле
ссылки). Контакт ребра — значение текстового поля с ``contact_for``.

Права приходят снаружи функцией ``visible(dataset_id)``: она возвращает
ключи скрытых полей реестра или ``None``, если реестр не виден. Записи
невидимых реестров и рёбра скрытых полей в граф не попадают.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Callable
from typing import Any

from engine import links, structure
from engine.errors import EngineError

MAX_DEPTH = 4
MAX_PATH = 8
MAX_NODES = 300
Visible = Callable[[str], frozenset[str] | None]


class _Graph:
    def __init__(self, conn: sqlite3.Connection, visible: Visible) -> None:
        self.conn = conn
        self.visible = visible
        self.nodes: dict[str, dict[str, Any]] = {}
        self._schemas: dict[str, structure.TypeSchema] = {}
        self._records: dict[int, sqlite3.Row | None] = {}

    def start(self, node: str) -> str:
        kind, _, entity = node.partition(":")
        if kind == "kks":
            row = self.conn.execute("SELECT * FROM kks_objects WHERE id = ?", (entity,)).fetchone()
            if row is not None:
                self._kks_node(row)
                return node
        if kind == "record":
            row = self.conn.execute("SELECT seq FROM records WHERE id = ?", (entity,)).fetchone()
            if row is not None and self._record(row[0]) is not None:
                return node
        raise EngineError("graph_node", f"Узел {node} не найден или не виден", field="node")

    def neighbours(self, node: str) -> list[dict[str, Any]]:
        """Рёбра узла в обе стороны."""
        kind, _, entity = node.partition(":")
        if kind == "kks":
            rows = self.conn.execute(
                "SELECT record_seq, field_key, ordinal, role FROM record_kks_links WHERE kks_id = ?",
                (entity,),
            ).fetchall()
            return [
                edge
                for row in rows
                if (edge := self._kks_edge(row["record_seq"], entity, row)) is not None
            ]
        seq = self.conn.execute("SELECT seq FROM records WHERE id = ?", (entity,)).fetchone()[0]
        edges = []
        for row in self.conn.execute(
            "SELECT record_seq, field_key, ordinal, role, kks_id FROM record_kks_links"
            " WHERE record_seq = ?",
            (seq,),
        ).fetchall():
            if (edge := self._kks_edge(seq, row["kks_id"], row)) is not None:
                edges.append(edge)
        for row in self.conn.execute(
            "SELECT record_seq, field_key, ordinal, target_seq FROM record_links"
            " WHERE record_seq = ? OR target_seq = ?",
            (seq, seq),
        ).fetchall():
            if (edge := self._record_edge(row)) is not None:
                edges.append(edge)
        return edges

    def _kks_edge(self, seq: int, kks_id: str, row: sqlite3.Row) -> dict[str, Any] | None:
        source = self._record(seq)
        if source is None or row["field_key"] in self._hidden(source):
            return None
        if f"kks:{kks_id}" not in self.nodes:
            found = self.conn.execute("SELECT * FROM kks_objects WHERE id = ?", (kks_id,))
            self._kks_node(found.fetchone())
        return {
            "from": f"record:{source['id']}",
            "to": f"kks:{kks_id}",
            "field": row["field_key"],
            "role": row["role"],
            "contact": self._contact(source, row["field_key"], row["ordinal"]),
        }

    def _record_edge(self, row: sqlite3.Row) -> dict[str, Any] | None:
        source, target = self._record(row["record_seq"]), self._record(row["target_seq"])
        if source is None or target is None or row["field_key"] in self._hidden(source):
            return None
        return {
            "from": f"record:{source['id']}",
            "to": f"record:{target['id']}",
            "field": row["field_key"],
            "role": "link",
            "contact": self._contact(source, row["field_key"], row["ordinal"]),
        }

    def _record(self, seq: int) -> sqlite3.Row | None:
        """Видимая действующая запись; заодно добавляет её узел."""
        if seq not in self._records:
            row = self.conn.execute(
                "SELECT r.id, r.doc, r.archived_at, d.id AS dataset_id, d.type_id"
                " FROM records r JOIN datasets d ON d.seq = r.dataset_seq WHERE r.seq = ?",
                (seq,),
            ).fetchone()
            if row is None or row["archived_at"] or self.visible(row["dataset_id"]) is None:
                row = None
            self._records[seq] = row
            if row is not None:
                doc = json.loads(row["doc"])
                self.nodes[f"record:{row['id']}"] = {
                    "id": f"record:{row['id']}",
                    "kind": "record",
                    "record_id": row["id"],
                    "dataset_id": row["dataset_id"],
                    "label": links.label(self._schema(row["type_id"]), doc, row["id"]),
                }
        return self._records[seq]

    def _kks_node(self, row: sqlite3.Row) -> None:
        self.nodes[f"kks:{row['id']}"] = {
            "id": f"kks:{row['id']}",
            "kind": "kks",
            "kks_id": row["id"],
            "label": row["code"],
        }

    def _hidden(self, record: sqlite3.Row) -> frozenset[str]:
        return self.visible(record["dataset_id"]) or frozenset()

    def _contact(self, record: sqlite3.Row, key: str, ordinal: int) -> str | None:
        schema = self._schema(record["type_id"])
        hidden = self._hidden(record)
        for field in schema.active():
            if field.contact_for == key and field.key not in hidden:
                value = json.loads(record["doc"]).get(field.key)
                if isinstance(value, list):
                    return value[ordinal] if ordinal < len(value) else None
                return value
        return None

    def _schema(self, type_id: str) -> structure.TypeSchema:
        if type_id not in self._schemas:
            self._schemas[type_id] = structure.load_schema(self.conn, type_id)
        return self._schemas[type_id]


def neighbourhood(
    conn: sqlite3.Connection, node: Any, visible: Visible, depth: Any = 2
) -> dict[str, Any]:
    """Всё, что связано с узлом, на ``depth`` шагов; не больше ``MAX_NODES`` узлов."""
    if type(depth) is not int or not 1 <= depth <= MAX_DEPTH:
        raise EngineError("graph_depth", f"Глубина: от 1 до {MAX_DEPTH}", field="depth")
    graph = _Graph(conn, visible)
    start = graph.start(_node(node))
    edges: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    queue, seen, truncated = deque([(start, 0)]), {start}, False
    while queue:
        current, level = queue.popleft()
        if level == depth:
            continue
        for edge in graph.neighbours(current):
            edges[(edge["from"], edge["to"], edge["field"], edge["contact"])] = edge
            other = edge["to"] if edge["from"] == current else edge["from"]
            if other in seen:
                continue
            if len(seen) >= MAX_NODES:
                truncated = True
                continue
            seen.add(other)
            queue.append((other, level + 1))
    return {
        "start": start,
        "nodes": [graph.nodes[item] for item in sorted(seen)],
        "edges": [edge for edge in edges.values() if edge["from"] in seen and edge["to"] in seen],
        "truncated": truncated,
    }


def path(conn: sqlite3.Connection, start: Any, finish: Any, visible: Visible) -> dict[str, Any]:
    """Кратчайший путь между двумя узлами не длиннее ``MAX_PATH`` рёбер."""
    graph = _Graph(conn, visible)
    first, last = graph.start(_node(start)), graph.start(_node(finish))
    came: dict[str, tuple[str, dict[str, Any]] | None] = {first: None}
    queue = deque([(first, 0)])
    while queue and last not in came:
        current, level = queue.popleft()
        if level == MAX_PATH:
            continue
        for edge in graph.neighbours(current):
            other = edge["to"] if edge["from"] == current else edge["from"]
            if other not in came:
                came[other] = (current, edge)
                queue.append((other, level + 1))
    if last not in came:
        return {"nodes": [], "edges": [], "found": False}
    nodes, edges, cursor = [last], [], last
    while (step := came[cursor]) is not None:
        cursor, edge = step
        nodes.append(cursor)
        edges.append(edge)
    return {
        "nodes": [graph.nodes[item] for item in reversed(nodes)],
        "edges": list(reversed(edges)),
        "found": True,
    }


def _node(value: Any) -> str:
    if not isinstance(value, str) or value.partition(":")[0] not in ("kks", "record"):
        raise EngineError("graph_node", "Узел: kks:<UUID> или record:<UUID>", field="node")
    return value
