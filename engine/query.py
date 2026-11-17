"""Слой запросов: единый язык условий для таблицы, экспорта и ИИ (API-03…API-05).

Список, поиск, фильтр, сортировка и подсчёт выполняются в SQL; записи
реестра в память не читаются. Страницы выдаются по курсору. Описание
языка: docs/contract/query-language.md
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from engine import fulltext, kks, structure, text
from engine.errors import EngineError
from engine.validation import coerce_scalar, load_dictionary

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_IN = 500
MAX_CONDITIONS = 30

_COMPARE = {"eq": "=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
_TEXT_OPS = ("contains", "starts")
_OPS = (*_COMPARE, "ne", "in", "empty", "not_empty", *_TEXT_OPS)
_SYSTEM = {
    "$id": ("id", "text"),
    "$version": ("version", "integer"),
    "$created_at": ("created_at", "text"),
    "$updated_at": ("updated_at", "text"),
}
_COLUMNS = "seq, id, version, doc, archived_at, created_at, updated_at"


@dataclass
class _Compiled:
    where: list[str] = field(default_factory=list)
    params: list[Any] = field(default_factory=list)
    sort_sql: str = "seq"
    descending: bool = False
    sort_tag: str = "seq"


def list_records(
    conn: sqlite3.Connection, dataset_id: str, query: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Страница записей реестра: ``items``, ``next_cursor`` и ``refs`` для показа ID."""
    query = query or {}
    dataset = structure.load_dataset(conn, dataset_id)
    schema = structure.load_schema(conn, dataset["type_id"])
    compiled = _compile(conn, dataset, schema, query)
    limit = _limit(query.get("limit"))
    segments: list[tuple[str | None, list[Any]]] = [(None, [])]
    if query.get("cursor"):
        segments = _after_cursor(compiled, query["cursor"])
    way = "DESC" if compiled.descending else "ASC"
    rows: list[sqlite3.Row] = []
    for condition, extra in segments:
        wanted = limit + 1 - len(rows)
        if wanted <= 0:
            break
        where = [*compiled.where, *([condition] if condition else [])]
        rows += conn.execute(
            f"SELECT {_COLUMNS}, {compiled.sort_sql} AS sort_value FROM records"
            f" WHERE {' AND '.join(where)}"
            f" ORDER BY sort_value {way}, seq {way} LIMIT ?",
            (*compiled.params, *extra, wanted),
        ).fetchall()
    page = rows[:limit]
    items = [_item(row, dataset_id) for row in page]
    cursor = None
    if len(rows) > limit:
        last = page[-1]
        cursor = _encode_cursor(compiled.sort_tag, last["sort_value"], last["seq"])
    return {"items": items, "next_cursor": cursor, "refs": _refs(conn, schema, items)}


def count_records(
    conn: sqlite3.Connection, dataset_id: str, query: dict[str, Any] | None = None
) -> int:
    """Число записей по тем же условиям, что и список."""
    dataset = structure.load_dataset(conn, dataset_id)
    schema = structure.load_schema(conn, dataset["type_id"])
    compiled = _compile(conn, dataset, schema, query or {})
    row = conn.execute(
        f"SELECT COUNT(*) FROM records WHERE {' AND '.join(compiled.where)}", compiled.params
    ).fetchone()
    return row[0]


# Сборка запроса ---------------------------------------------------------------


def _compile(
    conn: sqlite3.Connection,
    dataset: dict[str, Any],
    schema: structure.TypeSchema,
    query: dict[str, Any],
) -> _Compiled:
    unknown = set(query) - {"where", "search", "sort", "limit", "cursor", "archived", "kks"}
    if unknown:
        raise _bad(f"Неизвестный параметр запроса: {min(unknown)}")
    out = _Compiled(where=["dataset_seq = ?"], params=[dataset["seq"]])
    archived = query.get("archived", False)
    if archived is False:
        out.where.append("archived_at IS NULL")
    elif archived is True:
        out.where.append("archived_at IS NOT NULL")
    elif archived != "any":
        raise _bad("Параметр archived: true, false или «any»")
    builder = _Conditions(conn, dataset, schema, out.params)
    conditions = query.get("where") or []
    if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS:
        raise _bad(f"Условия: список длиной до {MAX_CONDITIONS}")
    for condition in conditions:
        out.where.append(builder.build(condition))
    if query.get("kks"):
        out.where.append("seq IN (SELECT record_seq FROM record_kks_links WHERE kks_id = ?)")
        out.params.append(query["kks"])
    if query.get("search"):
        found = fulltext.search_sql(schema, dataset["site_id"], str(query["search"]))
        if found:
            out.where.append(found[0])
            out.params.extend(found[1])
    _sort(schema, query.get("sort"), out)
    return out


class _Conditions:
    """Переводит условия «поле, операция, значение» в SQL с параметрами."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        dataset: dict[str, Any],
        schema: structure.TypeSchema,
        params: list[Any],
    ) -> None:
        self.conn = conn
        self.site_id = dataset["site_id"]
        self.schema = schema
        self.params = params

    def build(self, condition: Any) -> str:
        if not isinstance(condition, dict):
            raise _bad("Условие: ожидается объект")
        if "any" in condition:
            parts = condition["any"]
            if not isinstance(parts, list) or not parts or len(condition) != 1:
                raise _bad("Группа «any»: непустой список условий")
            return "(" + " OR ".join(self._simple(part) for part in parts) + ")"
        return self._simple(condition)

    def _simple(self, condition: Any) -> str:
        if not isinstance(condition, dict) or set(condition) - {"field", "op", "value"}:
            raise _bad("Условие: поля field, op, value")
        name, op, value = condition.get("field"), condition.get("op"), condition.get("value")
        if not isinstance(name, str):
            raise _bad("Условие: в field нужен ключ поля", "field")
        if not isinstance(op, str) or op not in _OPS:
            raise _bad(f"Неизвестная операция: {op}", name)
        if name in _SYSTEM:
            column, kind = _SYSTEM[name]
            return self._scalar(column, kind, op, value, name)
        found = self.schema.by_key(name) if isinstance(name, str) else None
        if found is None or found.archived_at is not None:
            raise _bad(f"В типе нет поля «{name}»", name if isinstance(name, str) else None)
        expr = _expr(found.key)
        if op == "empty":
            return f"{expr} IS NULL"
        if op == "not_empty":
            return f"{expr} IS NOT NULL"
        if found.kind == "kks":
            return self._kks(found, op, value)
        if found.multiple:
            return self._multiple(found, op, value)
        return self._field(found, expr, op, value)

    def _multiple(self, found: structure.Field, op: str, value: Any) -> str:
        inner = self._field(found, "je.value", "eq" if op == "ne" else op, value)
        exists = f"EXISTS (SELECT 1 FROM json_each(doc, '$.{found.key}') AS je WHERE {inner})"
        return f"NOT {exists}" if op == "ne" else exists

    def _field(self, found: structure.Field, expr: str, op: str, value: Any) -> str:
        if found.kind == "choice":
            return self._choice(found, expr, op, value)
        sql = self._scalar(expr, found.kind, op, value, found.key)
        if found.kind == "text" and op in _TEXT_OPS and found.searchable:
            # Индекс поиска сначала отбирает записи, где текст есть хоть в каком-то
            # поле; точная проверка нужного поля идёт уже по ним.
            needle = text.fold(value)
            if len(needle) >= fulltext.MIN_TOKEN and "\n" not in needle:
                self.params.insert(len(self.params) - 1, fulltext.phrase(needle))
                return f"(seq IN ({fulltext.MATCH_SQL}) AND {sql})"
        return sql

    def _scalar(self, expr: str, kind: str, op: str, value: Any, name: str) -> str:
        if op == "empty":
            return f"{expr} IS NULL"
        if op == "not_empty":
            return f"{expr} IS NOT NULL"
        if kind == "boolean" and op not in ("eq", "ne"):
            raise _bad("Для поля «да/нет» доступны только eq и ne", name)
        if op in _TEXT_OPS:
            needle = text.fold(_required(value, name))
            self.params.append(needle)
            test = "> 0" if op == "contains" else "= 1"
            return f"instr(lai_fold({expr}), ?) {test}"
        if op == "in":
            values = [self._coerce(kind, item, name) for item in _list(value, name)]
            self.params.extend(values)
            return f"{expr} IN ({','.join('?' * len(values))})"
        self.params.append(self._coerce(kind, _required(value, name), name))
        if op == "ne":
            return f"({expr} IS NULL OR {expr} != ?)"
        return f"{expr} {_COMPARE[op]} ?"

    def _coerce(self, kind: str, value: Any, name: str) -> Any:
        try:
            coerced = coerce_scalar(kind, value, name)
        except EngineError as error:
            raise _bad(error.message, name) from None
        if coerced is None:
            raise _bad("Значение условия не может быть пустым", name)
        return int(coerced) if isinstance(coerced, bool) else coerced

    def _choice(self, found: structure.Field, expr: str, op: str, value: Any) -> str:
        if op in _TEXT_OPS:
            self.params.extend([found.dictionary_id, text.fold(_required(value, found.key))])
            test = "> 0" if op == "contains" else "= 1"
            return (
                f"{expr} IN (SELECT id FROM dictionary_values WHERE dictionary_id = ?"
                f" AND instr(lai_fold(value), ?) {test})"
            )
        if op not in ("eq", "ne", "in"):
            raise _bad("Для списка доступны eq, ne, in, contains, starts", found.key)
        known = load_dictionary(self.conn, found.dictionary_id or "")
        wanted = _list(value, found.key) if op == "in" else [_required(value, found.key)]
        ids = [_choice_id(known, item) for item in wanted]
        return self._ids(expr, op, [item for item in ids if item])

    def _kks(self, found: structure.Field, op: str, value: Any) -> str:
        """Условие по полю ККС идёт через таблицу связей и её индекс по объекту."""
        links = "SELECT l.record_seq FROM record_kks_links l"
        if op in _TEXT_OPS:
            needle = text.kks_needle(_required(value, found.key))
            self.params.extend([found.key, self.site_id, needle])
            test = "> 0" if op == "contains" else "= 1"
            return (
                f"seq IN ({links} JOIN kks_objects k ON k.id = l.kks_id"
                f" WHERE l.field_key = ? AND k.site_id = ? AND instr(k.code_norm, ?) {test})"
            )
        if op not in ("eq", "ne", "in"):
            raise _bad("Для ККС доступны eq, ne, in, contains, starts", found.key)
        wanted = _list(value, found.key) if op == "in" else [_required(value, found.key)]
        ids = []
        for item in wanted:
            if text.is_uuid(item):
                ids.append(item)
                continue
            resolved = kks.resolve(self.conn, self.site_id, str(item))
            if resolved:
                ids.append(resolved["id"])
        if not ids:
            return "1" if op == "ne" else "0"
        self.params.extend([*ids, found.key])
        marks = ",".join("?" * len(ids))
        inside = f"seq {'NOT IN' if op == 'ne' else 'IN'}"
        return f"{inside} ({links} WHERE l.kks_id IN ({marks}) AND l.field_key = ?)"

    def _ids(self, expr: str, op: str, ids: list[str]) -> str:
        if not ids:
            return "1" if op == "ne" else "0"
        self.params.extend(ids)
        marks = ",".join("?" * len(ids))
        if op == "ne":
            return f"({expr} IS NULL OR {expr} NOT IN ({marks}))"
        return f"{expr} IN ({marks})"


def _sort(schema: structure.TypeSchema, sort: Any, out: _Compiled) -> None:
    if not sort:
        return
    if isinstance(sort, dict):
        sort = [sort]
    if not isinstance(sort, list) or len(sort) != 1 or not isinstance(sort[0], dict):
        raise _bad("Сортировка: одно поле вида {field, dir}")
    name, way = sort[0].get("field"), sort[0].get("dir", "asc")
    if not isinstance(name, str):
        raise _bad("Сортировка: в field нужен ключ поля", "sort")
    if way not in ("asc", "desc"):
        raise _bad("Направление сортировки: asc или desc", name)
    out.descending = way == "desc"
    out.sort_tag = f"{name}:{way}"
    if name in _SYSTEM:
        out.sort_sql = _SYSTEM[name][0]
        return
    found = schema.by_key(name) if isinstance(name, str) else None
    if found is None or found.archived_at is not None:
        raise _bad(f"В типе нет поля «{name}»", name if isinstance(name, str) else None)
    if found.multiple:
        raise _bad("По полю с несколькими значениями сортировать нельзя", name)
    expr = _expr(found.key)
    if found.kind == "choice":
        out.sort_sql = f"(SELECT value_norm FROM dictionary_values WHERE id = {expr})"
    elif found.kind == "kks":
        out.sort_sql = f"(SELECT code_norm FROM kks_objects WHERE id = {expr})"
    else:
        out.sort_sql = expr


# Курсор -----------------------------------------------------------------------


def _encode_cursor(tag: str, value: Any, seq: int) -> str:
    raw = json.dumps([_tag_hash(tag), value, seq], ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _after_cursor(compiled: _Compiled, cursor: Any) -> list[tuple[str | None, list[Any]]]:
    """Условия для строк после курсора — по частям, в порядке выдачи.

    Пустые значения идут первыми при ``asc`` и последними при ``desc``.
    Строки с пустым и непустым значением запрашиваются отдельно: так каждая
    часть сразу встаёт по индексу на место курсора, а не перебирает
    уже пройденные строки (API-05).
    """
    try:
        padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
        tag, value, seq = json.loads(base64.urlsafe_b64decode(padded.encode()))
        seq = int(seq)
    except (ValueError, TypeError):
        raise _bad("Курсор повреждён") from None
    if tag != _tag_hash(compiled.sort_tag):
        raise _bad("Курсор получен для другой сортировки; начните с первой страницы")
    expr = compiled.sort_sql
    if expr == "seq":
        return [("seq < ?" if compiled.descending else "seq > ?", [seq])]
    if compiled.descending:
        if value is None:
            return [(f"{expr} IS NULL AND seq < ?", [seq])]
        return [
            (f"{expr} <= ? AND ({expr} < ? OR seq < ?)", [value, value, seq]),
            (f"{expr} IS NULL", []),
        ]
    if value is None:
        return [(f"{expr} IS NULL AND seq > ?", [seq]), (f"{expr} IS NOT NULL", [])]
    return [(f"{expr} >= ? AND ({expr} > ? OR seq > ?)", [value, value, seq])]


def _tag_hash(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()[:8]


# Мелочи -----------------------------------------------------------------------


def _expr(key: str) -> str:
    """Выражение значения поля; совпадает с выражением индекса (DATA-03)."""
    if not structure.KEY_RE.match(key):
        raise _bad(f"Недопустимый ключ поля: {key}")
    return f"json_extract(doc, '$.{key}')"


def _limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise _bad(f"Размер страницы: от 1 до {MAX_LIMIT}", "limit")
    return value


def _required(value: Any, name: Any) -> Any:
    if value is None or value == "" or isinstance(value, (list, dict)):
        raise _bad("Для этой операции нужно одно значение", name)
    return value


def _list(value: Any, name: Any) -> list[Any]:
    if not isinstance(value, list) or not value or len(value) > MAX_IN:
        raise _bad(f"Для операции in нужен список от 1 до {MAX_IN} значений", name)
    return value


def _choice_id(known: dict[str, Any], item: Any) -> str | None:
    if isinstance(item, str) and item in known["by_id"]:
        return item
    return known["by_name"].get(text.norm_name(str(item)))


def _bad(message: str, name: Any = None) -> EngineError:
    return EngineError("query_invalid", message, field=name if isinstance(name, str) else None)


def _item(row: sqlite3.Row, dataset_id: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "dataset_id": dataset_id,
        "version": row["version"],
        "doc": json.loads(row["doc"]),
        "archived_at": row["archived_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def refs(
    conn: sqlite3.Connection, dataset_id: str, docs: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """Названия значений справочников и коды ККС для документов записей реестра."""
    dataset = structure.load_dataset(conn, dataset_id)
    schema = structure.load_schema(conn, dataset["type_id"])
    return _refs(conn, schema, [{"doc": doc} for doc in docs])


def _refs(
    conn: sqlite3.Connection, schema: structure.TypeSchema, items: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """Названия для ID на странице: значения справочников и коды ККС."""
    wanted: dict[str, set[str]] = {"choice": set(), "kks": set()}
    for found in schema.fields:
        if found.kind not in wanted:
            continue
        for item in items:
            value = item["doc"].get(found.key)
            if value is not None:
                wanted[found.kind].update(value if isinstance(value, list) else [value])
    return {
        "choices": _names(conn, "dictionary_values", "value", wanted["choice"]),
        "kks": _names(conn, "kks_objects", "code", wanted["kks"]),
    }


def _names(conn: sqlite3.Connection, table: str, column: str, ids: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    pending = sorted(ids)
    for start in range(0, len(pending), 500):
        chunk = pending[start : start + 500]
        rows = conn.execute(
            f"SELECT id, {column} FROM {table} WHERE id IN ({','.join('?' * len(chunk))})",
            chunk,
        ).fetchall()
        found.update({row[0]: row[1] for row in rows})
    return found
