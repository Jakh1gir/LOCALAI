"""Подсчёт записей по значениям поля и итоги числового поля.

Значения поля — список значений в фильтре столбца. Итоги (``field_stats``) —
минимум, максимум, сумма и среднее числового поля и самая ранняя и поздняя
дата: для инструмента ИИ ``aggregate`` (ТЗ 9.1, AI-04) и строки итогов.

Условия те же, что у списка и подсчёта записей (API-04): ``where``, ``search``,
``kks``, ``archived``. Группировка идёт в SQL (API-03). У многозначного поля
запись считается в каждом своём значении один раз; запись без значения
попадает в группу ``null``.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from engine import structure
from engine.query import _bad, _compile, _expr, _refs

MAX_VALUES = 200
_FILTERS = {"where", "search", "kks", "archived"}


def value_counts(
    conn: sqlite3.Connection,
    dataset_id: str,
    field_key: Any,
    query: dict[str, Any] | None = None,
    *,
    limit: Any = 50,
    hidden: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Самые частые значения поля и число записей с каждым, от больших групп к меньшим."""
    dataset = structure.load_dataset(conn, dataset_id)
    schema = structure.load_schema(conn, dataset["type_id"]).without(hidden)
    field = schema.by_key(field_key) if isinstance(field_key, str) else None
    if field is None:
        raise _bad(f"В реестре нет поля «{field_key}»", "field")
    if type(limit) is not int or not 1 <= limit <= MAX_VALUES:
        raise _bad(f"Число значений: от 1 до {MAX_VALUES}", "limit")
    filters = dict(query or {})
    unknown = set(filters) - _FILTERS
    if unknown:
        raise _bad(f"Неизвестный параметр запроса: {min(unknown)}")
    compiled = _compile(conn, dataset, schema, filters, hidden=bool(hidden))
    where = " AND ".join(compiled.where)
    expr = _expr(field.key)
    if field.multiple:
        # Подзапрос отделяет столбцы записи от столбцов json_each (у обоих есть id).
        grouped = (
            "SELECT value, COUNT(DISTINCT seq) AS n FROM ("
            f" SELECT je.value AS value, r.seq AS seq FROM (SELECT seq, doc FROM records"
            f" WHERE {where}) AS r, json_each(r.doc, '$.{field.key}') AS je"
            f" UNION ALL SELECT NULL, seq FROM records WHERE {where} AND {expr} IS NULL"
            ") GROUP BY value"
        )
        params = [*compiled.params, *compiled.params]
    else:
        grouped = f"SELECT {expr} AS value, COUNT(*) AS n FROM records WHERE {where} GROUP BY value"
        params = list(compiled.params)
    rows = conn.execute(
        f"SELECT value, n FROM ({grouped}) ORDER BY n DESC, value IS NULL, value LIMIT ?",
        (*params, limit),
    ).fetchall()
    distinct = conn.execute(f"SELECT COUNT(*) FROM ({grouped})", params).fetchone()[0]
    items = [{"value": _shown(field, row[0]), "count": row[1]} for row in rows]
    values = [item["value"] for item in items if item["value"] is not None]
    return {
        "field": field.key,
        "items": items,
        "distinct": distinct,
        "refs": _refs(conn, schema, [{"doc": {field.key: values}}]),
    }


def _shown(field: structure.Field, value: Any) -> Any:
    if field.kind == "boolean" and value is not None:
        return bool(value)
    return value


STAT_KINDS = ("integer", "number", "date")


def field_stats(
    conn: sqlite3.Connection,
    dataset_id: str,
    field_key: Any,
    query: dict[str, Any] | None = None,
    *,
    hidden: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Итоги поля по записям с условиями: ``count`` — записей со значением.

    Число — ``min``, ``max``, ``sum``, ``avg``; дата — ``min`` и ``max``.
    ``min_id`` и ``max_id`` — записи с крайними значениями (первая из равных
    по порядку добавления): на них ссылается ответ.
    """
    dataset = structure.load_dataset(conn, dataset_id)
    schema = structure.load_schema(conn, dataset["type_id"]).without(hidden)
    field = schema.by_key(field_key) if isinstance(field_key, str) else None
    if field is None:
        raise _bad(f"В реестре нет поля «{field_key}»", "field")
    if field.kind not in STAT_KINDS:
        raise _bad(f"Итоги считаются по числу и дате, а «{field.name}» — {field.kind}", "field")
    filters = dict(query or {})
    unknown = set(filters) - _FILTERS
    if unknown:
        raise _bad(f"Неизвестный параметр запроса: {min(unknown)}")
    compiled = _compile(conn, dataset, schema, filters, hidden=bool(hidden))
    where = " AND ".join(compiled.where)
    if field.multiple:
        values = (
            f"SELECT je.value AS value, r.seq AS seq, r.id AS id FROM (SELECT seq, id, doc"
            f" FROM records WHERE {where}) AS r, json_each(r.doc, '$.{field.key}') AS je"
        )
    else:
        values = f"SELECT {_expr(field.key)} AS value, seq, id FROM records WHERE {where}"
    filled = f"SELECT value, seq, id FROM ({values}) WHERE value IS NOT NULL"
    params = list(compiled.params)
    numeric = field.kind != "date"
    totals = conn.execute(
        f"SELECT COUNT(DISTINCT seq), MIN(value), MAX(value)"
        f"{', SUM(value), AVG(value)' if numeric else ''} FROM ({filled})",
        params,
    ).fetchone()
    shown: dict[str, Any] = {"field": field.key, "count": totals[0], "min": totals[1],
                             "max": totals[2]}  # fmt: skip
    if numeric:
        shown["sum"] = totals[3]
        shown["avg"] = None if totals[4] is None else round(totals[4], 6)
    for edge, order in (("min", "ASC"), ("max", "DESC")):
        row = conn.execute(
            f"SELECT id FROM ({filled}) ORDER BY value {order}, seq LIMIT 1", params
        ).fetchone()
        shown[f"{edge}_id"] = row[0] if row else None
    return shown
