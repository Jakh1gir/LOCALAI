"""Общий поиск по базе для строки Ctrl+K (ТЗ 5.1, `/api/v1/search`; AI-16, PERF-07).

Одна строка — несколько групп результатов:

- **ККС** — точное совпадение кода, псевдонима или кода сигнала (по правилу
  станции DATA-16) первым, затем коды, содержащие строку;
- **записи** — во всех реестрах, которые пользователь видит, по
  полнотекстовому индексу записей; записи, где строка совпала с ключом
  (уникальным полем) записи, — первыми.

- **документы** — фрагменты прочитанных документов видимых типов: по одному
  на страницу, с отрывком текста вокруг найденного.

- **заметки** — подтверждённые заметки знаний, которые пользователь видит;
- **словарь** — термины станции в строке запроса (KNOW-10): местное название
  добавляет объекты ККС по официальному коду (``via: "glossary"``) и записи
  с этими кодами.

Права (ROLE-03): реестр без права «видеть» не участвует. В реестре, где у
роли есть скрытые поля, поиск идёт перебором видимых значений, а не по
индексу: индекс содержит и скрытые поля, и совпадение по ним выдало бы их.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from engine import documents, fulltext, kks, kks_rules, knowledge, links, query, structure, text
from engine.access import Access
from engine.errors import EngineError

LIMIT = 20
MAX_LIMIT = 50
MAX_QUERY = 200
_SCAN_DATASETS = 20  # реестров со скрытыми полями, перебираемых за один запрос


def search(
    conn: sqlite3.Connection,
    viewer: Access,
    raw: Any,
    *,
    limit: int = LIMIT,
    site_id: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    if not isinstance(raw, str) or not raw.strip():
        raise EngineError("search_empty", "Введите, что искать", field="q")
    needle = raw.strip()
    if len(needle) > MAX_QUERY:
        raise EngineError("search_too_long", f"Строка поиска длиннее {MAX_QUERY} знаков", field="q")
    if not 1 <= limit <= MAX_LIMIT:
        raise EngineError("limit_invalid", f"Размер списка — от 1 до {MAX_LIMIT}", field="limit")
    sites = _sites(conn, site_id)
    found_kks = _kks(conn, sites, needle, limit)
    records, more = _records(conn, viewer, sites, needle, limit)
    glossary = knowledge.expand(conn, needle)
    _expand(conn, viewer, sites, glossary, found_kks, records, limit)
    tokens = [token for token in text.fold(needle).split() if len(token) >= fulltext.MIN_TOKEN]
    return {
        "query": needle,
        "kks": found_kks,
        "records": records,
        "more": {"records": more},
        "documents": documents.search(conn, viewer, tokens, limit),
        "notes": knowledge.search(conn, viewer, tokens, limit),
        "glossary": glossary,
        "took_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _expand(
    conn: sqlite3.Connection,
    viewer: Access,
    sites: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    found_kks: list[dict[str, Any]],
    records: list[dict[str, Any]],
    limit: int,
) -> None:
    """Словарь станции (KNOW-10): местное название ведёт к объектам по официальному коду."""
    allowed = {site["id"]: site for site in sites}
    seen_kks = {item["id"] for item in found_kks}
    seen_records = {item["id"] for item in records}
    for term in glossary:
        for item in term["kks"]:
            if item["site_id"] not in allowed or item["id"] in seen_kks:
                continue
            row = kks.get(conn, item["id"])
            found_kks.insert(
                0,
                {
                    **_kks_item(row, allowed[item["site_id"]]),
                    "exact": True,
                    "via": "glossary",
                    "term": term["term"],
                },
            )
            seen_kks.add(item["id"])
            extra, _ = _records(conn, viewer, sites, item["code"], limit)
            for record in extra:
                if record["id"] not in seen_records and len(records) < limit:
                    records.append({**record, "via": "glossary"})
                    seen_records.add(record["id"])
    del found_kks[limit:]


def _sites(conn: sqlite3.Connection, site_id: str | None) -> list[dict[str, Any]]:
    if site_id:
        return [structure.get_site(conn, site_id)]
    return [item for item in structure.list_sites(conn) if not item["archived_at"]]


# ККС ---------------------------------------------------------------------------


def _kks(
    conn: sqlite3.Connection, sites: list[dict[str, Any]], needle: str, limit: int
) -> list[dict[str, Any]]:
    """Точное совпадение (код, псевдоним, код сигнала) — первым, затем части кода."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for site in sites:
        try:
            exact = kks_rules.resolve(conn, site["id"], needle)
        except EngineError:
            exact = None
        if exact is not None:
            found.append({**_kks_item(exact, site), "exact": True})
            seen.add(exact["id"])
    for site in sites:
        for item in kks.search(conn, site["id"], needle, limit=limit):
            if item["id"] not in seen and len(found) < limit:
                found.append({**_kks_item(item, site), "exact": False})
                seen.add(item["id"])
    return found[:limit]


def _kks_item(item: dict[str, Any], site: dict[str, Any]) -> dict[str, Any]:
    shown = {
        "id": item["id"],
        "code": item["code"],
        "name": item.get("name"),
        "site_id": site["id"],
        "site": site["name"],
        "archived": item.get("archived_at") is not None,
    }
    if item.get("via") in ("alias", "signal"):
        shown["via"] = item["via"]
    if item.get("via") == "signal":
        shown["signal"] = item["signal"]
    return shown


# Записи ------------------------------------------------------------------------


def _records(
    conn: sqlite3.Connection,
    viewer: Access,
    sites: list[dict[str, Any]],
    needle: str,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    tokens = [token for token in text.fold(needle).split() if len(token) >= fulltext.MIN_TOKEN]
    if not tokens:
        return [], False  # одно-два знака: только подсказки ККС
    plain, scanned = _datasets(conn, viewer, sites)
    rows: list[sqlite3.Row] = []
    if plain:
        rows += _indexed(conn, plain, tokens, limit + 1)
    for dataset, hidden in scanned[:_SCAN_DATASETS]:
        page = query.list_records(
            conn, dataset["id"], {"search": needle, "limit": limit + 1}, hidden=hidden
        )
        for item in page["items"]:
            rows.append(
                conn.execute(
                    "SELECT r.seq, r.id, r.doc, r.version, d.id AS dataset_id FROM records r"
                    " JOIN datasets d ON d.seq = r.dataset_seq WHERE r.id = ?",
                    (item["id"],),
                ).fetchone()
            )
    by_id = {item["id"]: item for item in plain.values()}
    by_id.update({item["id"]: item for item, _ in scanned})
    hidden_by = {item["id"]: hidden for item, hidden in scanned}
    items = [
        _record_item(
            conn,
            row,
            by_id[row["dataset_id"]],
            hidden_by.get(row["dataset_id"], frozenset()),
            tokens,
        )
        for row in rows
    ]
    items.sort(key=lambda item: (not item["key_match"], -item["seq"]))
    return [_public(item) for item in items[:limit]], len(items) > limit


def _datasets(
    conn: sqlite3.Connection, viewer: Access, sites: list[dict[str, Any]]
) -> tuple[dict[int, dict[str, Any]], list[tuple[dict[str, Any], frozenset[str]]]]:
    """Видимые реестры: без скрытых полей — по индексу, со скрытыми — перебором."""
    plain: dict[int, dict[str, Any]] = {}
    scanned: list[tuple[dict[str, Any], frozenset[str]]] = []
    schemas: dict[str, structure.TypeSchema] = {}
    for site in sites:
        for item in structure.list_datasets(conn, site_id=site["id"]):
            if item["archived_at"] or not viewer.can(item["id"], "view"):
                continue
            if item["type_id"] not in schemas:
                schemas[item["type_id"]] = structure.load_schema(conn, item["type_id"])
            hidden = viewer.hidden_keys(schemas[item["type_id"]])
            dataset = {**item, "seq": structure.load_dataset(conn, item["id"])["seq"]}
            dataset["schema"] = schemas[item["type_id"]]
            if hidden:
                scanned.append((dataset, hidden))
            else:
                plain[dataset["seq"]] = dataset
    return plain, scanned


def _indexed(
    conn: sqlite3.Connection, plain: dict[int, dict[str, Any]], tokens: list[str], wanted: int
) -> list[sqlite3.Row]:
    """Новые сначала: индекс даёт кандидатов, реестр и архив отсекаются в том же запросе."""
    marks = ",".join("?" * len(plain))
    return conn.execute(
        "SELECT r.seq, r.id, r.doc, r.version, d.id AS dataset_id FROM records r"
        " JOIN datasets d ON d.seq = r.dataset_seq"
        f" WHERE r.seq IN ({fulltext.MATCH_SQL}) AND r.dataset_seq IN ({marks})"
        " AND r.archived_at IS NULL ORDER BY r.seq DESC LIMIT ?",
        (" AND ".join(fulltext.phrase(token) for token in tokens), *plain, wanted),
    ).fetchall()


def _record_item(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    dataset: dict[str, Any],
    hidden: frozenset[str],
    tokens: list[str],
) -> dict[str, Any]:
    schema: structure.TypeSchema = dataset["schema"]
    doc = {key: value for key, value in json.loads(row["doc"]).items() if key not in hidden}
    shown = fulltext.Labels(conn)
    match = None
    partial = None  # слова запроса нашлись в разных полях: показываем первое
    key_match = False
    for field in schema.active():
        if field.key not in doc or field.key in hidden:
            continue
        values = doc[field.key] if isinstance(doc[field.key], list) else [doc[field.key]]
        texts = [_text(field, value, shown) for value in values]
        joined = ", ".join(item for item in texts if item)
        folded = text.fold(joined)
        if joined and all(token in folded for token in tokens):
            if match is None:
                match = {"field": field.name, "text": joined[:200]}
            if field.unique_values:
                key_match = True
        elif partial is None and joined and any(token in folded for token in tokens):
            partial = {"field": field.name, "text": joined[:200]}
    return {
        "id": row["id"],
        "seq": row["seq"],
        "dataset_id": dataset["id"],
        "dataset": dataset["name"],
        "label": links.label(schema, doc, row["id"]),
        "version": row["version"],
        "match": match or partial,
        "key_match": key_match,
    }


def _text(field: structure.Field, value: Any, shown: fulltext.Labels) -> str:
    if field.kind == "choice":
        return shown.choice(value)
    if field.kind == "kks":
        return shown.kks(value)
    return "" if value is None else str(value)


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in ("seq", "key_match")}
