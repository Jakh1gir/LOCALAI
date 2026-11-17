"""Полнотекстовый индекс записей: поиск подстроки по всем полям (PERF-07).

В индекс попадает то, что видит человек: текст, числа, даты, названия
значений справочников и коды ККС. Текст сворачивается к нижнему регистру
до записи в индекс, поэтому поиск не зависит от регистра и от «ё».
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from engine import structure, text

MIN_TOKEN = 3  # триграммный индекс не находит слова короче трёх символов
MATCH_SQL = "SELECT rowid FROM record_fts WHERE record_fts MATCH ?"
_TEXT_KINDS = ("text", "integer", "number", "date")


class Labels:
    """Названия значений справочников и коды ККС по ID, с кэшем на операцию."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._choice: dict[str, str] = {}
        self._kks: dict[str, str] = {}

    def choice(self, value_id: str) -> str:
        if value_id not in self._choice:
            row = self.conn.execute(
                "SELECT value FROM dictionary_values WHERE id = ?", (value_id,)
            ).fetchone()
            self._choice[value_id] = row["value"] if row else ""
        return self._choice[value_id]

    def kks(self, kks_id: str) -> str:
        if kks_id not in self._kks:
            row = self.conn.execute(
                "SELECT code FROM kks_objects WHERE id = ?", (kks_id,)
            ).fetchone()
            self._kks[kks_id] = row["code"] if row else ""
        return self._kks[kks_id]


def record_text(doc: dict[str, Any], schema: structure.TypeSchema, labels: Labels) -> str:
    parts: list[str] = []
    for field in schema.active():
        value = doc.get(field.key)
        if value is None or not field.searchable:
            continue
        for item in value if isinstance(value, list) else [value]:
            parts.extend(_shown(field.kind, item, labels))
    return text.fold("\n".join(part for part in parts if part))


def _shown(kind: str, item: Any, labels: Labels) -> list[str]:
    if kind == "choice":
        return [labels.choice(item)]
    if kind == "kks":
        return [labels.kks(item)]
    if kind == "date":
        year, month, day = str(item).split("-")
        return [str(item), f"{day}.{month}.{year}"]
    if kind == "number" and isinstance(item, float):
        shown = repr(item)
        return [shown, shown.replace(".", ",")]
    if kind == "boolean":
        return []
    return [str(item)]


def write(conn: sqlite3.Connection, seq: int, body: str) -> None:
    conn.execute("INSERT OR REPLACE INTO record_fts (rowid, text) VALUES (?, ?)", (seq, body))


def reindex_records(conn: sqlite3.Connection, seqs: Iterable[int]) -> int:
    """Перестраивает индекс для указанных записей. Возвращает их число."""
    labels = Labels(conn)
    schemas: dict[str, structure.TypeSchema] = {}
    count = 0
    pending = list(seqs)
    for start in range(0, len(pending), 500):
        chunk = pending[start : start + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT r.seq, r.doc, d.type_id FROM records r JOIN datasets d"
            f" ON d.seq = r.dataset_seq WHERE r.seq IN ({marks})",
            chunk,
        ).fetchall()
        for row in rows:
            if row["type_id"] not in schemas:
                schemas[row["type_id"]] = structure.load_schema(conn, row["type_id"])
            body = record_text(json.loads(row["doc"]), schemas[row["type_id"]], labels)
            write(conn, row["seq"], body)
            count += 1
    return count


def reindex_type(conn: sqlite3.Connection, type_id: str) -> int:
    rows = conn.execute(
        "SELECT r.seq FROM records r JOIN datasets d ON d.seq = r.dataset_seq WHERE d.type_id = ?",
        (type_id,),
    ).fetchall()
    return reindex_records(conn, [row[0] for row in rows])


def reindex_choice(conn: sqlite3.Connection, dictionary_id: str, value_id: str) -> int:
    """После переименования значения справочника обновляет записи, где оно стоит."""
    fields = conn.execute(
        "SELECT type_id, key FROM fields WHERE dictionary_id = ?", (dictionary_id,)
    ).fetchall()
    seqs: set[int] = set()
    for field in fields:
        rows = conn.execute(
            "SELECT r.seq FROM records r JOIN datasets d ON d.seq = r.dataset_seq"
            " WHERE d.type_id = ? AND EXISTS (SELECT 1 FROM json_tree(r.doc, ?) AS jt"
            " WHERE jt.atom = ?)",
            (field["type_id"], f"$.{field['key']}", value_id),
        ).fetchall()
        seqs.update(row[0] for row in rows)
    return reindex_records(conn, sorted(seqs))


def reindex_kks(conn: sqlite3.Connection, kks_id: str) -> int:
    rows = conn.execute(
        "SELECT DISTINCT record_seq FROM record_kks_links WHERE kks_id = ?", (kks_id,)
    ).fetchall()
    return reindex_records(conn, [row[0] for row in rows])


def search_sql(
    schema: structure.TypeSchema, site_id: str, query: str
) -> tuple[str, list[Any]] | None:
    """Условие SQL для строки поиска: каждое слово должно встретиться в записи."""
    tokens = text.fold(query).split()
    if not tokens:
        return None
    clauses: list[str] = []
    params: list[Any] = []
    long = [token for token in tokens if len(token) >= MIN_TOKEN]
    if long:
        clauses.append(f"seq IN ({MATCH_SQL})")
        params.append(" AND ".join(phrase(token) for token in long))
    for token in tokens:
        if len(token) < MIN_TOKEN:
            sql, extra = _short_token_sql(schema, site_id, token)
            clauses.append(sql)
            params.extend(extra)
    return " AND ".join(clauses), params


def phrase(folded: str) -> str:
    """Свёрнутый текст как одна фраза запроса FTS5."""
    return '"' + folded.replace('"', '""') + '"'


def _short_token_sql(
    schema: structure.TypeSchema, site_id: str, token: str
) -> tuple[str, list[Any]]:
    """Слово из одного-двух символов ищется перебором значений записи.

    Это медленнее индекса, но остаётся в пределах одного реестра.
    """
    fields = [field for field in schema.active() if field.searchable]
    options: list[str] = []
    params: list[Any] = []
    plain = [field.key for field in fields if field.kind in _TEXT_KINDS]
    if plain:
        keys = ",".join(f"'{key}'" for key in plain)
        paths = ",".join(f"'$.{key}'" for key in plain)
        options.append(
            "EXISTS (SELECT 1 FROM json_tree(doc) AS jt WHERE jt.atom IS NOT NULL"
            f" AND ((jt.path = '$' AND jt.key IN ({keys})) OR jt.path IN ({paths}))"
            " AND instr(lai_fold(jt.atom), ?) > 0)"
        )
        params.append(token)
    dictionaries = sorted({f.dictionary_id for f in fields if f.kind == "choice"})
    if dictionaries:
        marks = ",".join("?" * len(dictionaries))
        options.append(
            "EXISTS (SELECT 1 FROM json_tree(doc) AS jt JOIN dictionary_values dv"
            f" ON dv.id = jt.atom WHERE dv.dictionary_id IN ({marks})"
            " AND instr(lai_fold(dv.value), ?) > 0)"
        )
        params.extend([*dictionaries, token])
    kks_fields = [field.key for field in fields if field.kind == "kks"]
    if kks_fields:
        marks = ",".join("?" * len(kks_fields))
        options.append(
            "seq IN (SELECT l.record_seq FROM record_kks_links l JOIN kks_objects k"
            f" ON k.id = l.kks_id WHERE l.field_key IN ({marks}) AND k.site_id = ?"
            " AND instr(k.code_norm, ?) > 0)"
        )
        params.extend([*kks_fields, site_id, token.upper()])
    if not options:
        return "0", []
    return "(" + " OR ".join(options) + ")", params
