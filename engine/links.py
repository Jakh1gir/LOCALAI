"""Уникальные поля и ссылки записей на записи (DATA-12, DATA-13).

Значение уникального поля действующей записи хранится в ``record_unique``
в нормализованном виде; повтор в том же реестре упирается в первичный ключ.
Ссылка на запись хранится в документе как UUID, а в ``record_links`` — как
пара внутренних номеров: по ней строятся обратные ссылки и граф объектов.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from engine import structure, text
from engine.errors import CONFLICT, EngineError

MAX_BACKLINKS = 200


def unique_value(field: structure.Field, value: Any) -> str:
    """Нормализованный вид значения: так сравниваются повторы."""
    if field.kind == "text":
        return text.fold(str(value)).strip()
    if field.kind in ("integer", "number"):
        return json.dumps(value)
    return str(value)


def sync_unique(
    conn: sqlite3.Connection,
    schema: structure.TypeSchema,
    dataset_seq: int,
    record_seq: int,
    doc: dict[str, Any],
    *,
    active: bool,
) -> None:
    """Строки уникальных значений записи; у записи в архиве их нет."""
    conn.execute("DELETE FROM record_unique WHERE record_seq = ?", (record_seq,))
    if not active:
        return
    for field in schema.active():
        if not field.unique_values or doc.get(field.key) is None:
            continue
        value = unique_value(field, doc[field.key])
        try:
            conn.execute(
                "INSERT INTO record_unique (dataset_seq, field_key, value, record_seq)"
                " VALUES (?,?,?,?)",
                (dataset_seq, field.key, value, record_seq),
            )
        except sqlite3.IntegrityError:
            raise _taken(conn, field, dataset_seq, value, doc[field.key]) from None


def rebuild_unique(conn: sqlite3.Connection, field: structure.Field) -> None:
    """Включение уникальности у поля: строки для всех действующих записей типа."""
    conn.execute(
        "DELETE FROM record_unique WHERE field_key = ? AND dataset_seq IN"
        " (SELECT seq FROM datasets WHERE type_id = ?)",
        (field.key, field.type_id),
    )
    if not field.unique_values:
        return
    rows = conn.execute(
        "SELECT r.seq, r.dataset_seq, json_extract(r.doc, ?) AS value FROM records r"
        " JOIN datasets d ON d.seq = r.dataset_seq"
        " WHERE d.type_id = ? AND r.archived_at IS NULL AND json_extract(r.doc, ?) IS NOT NULL",
        (f"$.{field.key}", field.type_id, f"$.{field.key}"),
    ).fetchall()
    for row in rows:
        value = unique_value(field, row["value"])
        try:
            conn.execute(
                "INSERT INTO record_unique (dataset_seq, field_key, value, record_seq)"
                " VALUES (?,?,?,?)",
                (row["dataset_seq"], field.key, value, row["seq"]),
            )
        except sqlite3.IntegrityError:
            error = _taken(conn, field, row["dataset_seq"], value, row["value"])
            error.code = "unique_has_duplicates"
            error.message = (
                f"Поле «{field.name}» нельзя сделать уникальным: значение «{row['value']}»"
                " уже повторяется. Исправьте повторы и повторите"
            )
            raise error from None


def sync_links(
    conn: sqlite3.Connection, schema: structure.TypeSchema, record_seq: int, doc: dict[str, Any]
) -> None:
    conn.execute("DELETE FROM record_links WHERE record_seq = ?", (record_seq,))
    rows = []
    for field in schema.fields:
        if field.kind != "record" or doc.get(field.key) is None:
            continue
        value = doc[field.key]
        for ordinal, target_id in enumerate(value if isinstance(value, list) else [value]):
            target = conn.execute("SELECT seq FROM records WHERE id = ?", (target_id,)).fetchone()
            if target is None:
                raise EngineError(
                    "record_link_missing",
                    f"Поле «{field.name}»: запись {target_id} не найдена",
                    field=field.key,
                )
            rows.append((record_seq, field.key, ordinal, target[0]))
    conn.executemany(
        "INSERT INTO record_links (record_seq, field_key, ordinal, target_seq) VALUES (?,?,?,?)",
        rows,
    )


def resolve(conn: sqlite3.Connection, field: structure.Field, site_id: str, raw: Any) -> str:
    """UUID записи для поля-ссылки: из ``{"id": …}``, из UUID или по уникальному значению.

    Текст ищется среди уникальных полей типа-цели в реестрах той же площадки:
    так строка сигнала из Excel находит кабель по его номеру.
    """
    if isinstance(raw, dict) and set(raw) == {"id"}:
        raw = raw["id"]
    if not isinstance(raw, str | int) or isinstance(raw, bool) or str(raw).strip() == "":
        raise EngineError("record_link_invalid", "Ожидается ID или ключ записи", field=field.key)
    shown = str(raw).strip()
    if text.is_uuid(shown):
        row = conn.execute(
            "SELECT r.id, r.archived_at, d.type_id, d.site_id FROM records r"
            " JOIN datasets d ON d.seq = r.dataset_seq WHERE r.id = ?",
            (shown,),
        ).fetchone()
        if row is not None:
            _check_target(field, row, site_id)
            return row["id"]
    target = structure.load_schema(conn, field.target_type_id or "")
    keys = [item for item in target.active() if item.unique_values]
    found: list[sqlite3.Row] = []
    for key in keys:
        found += conn.execute(
            "SELECT r.id, r.archived_at, d.type_id, d.site_id FROM record_unique u"
            " JOIN records r ON r.seq = u.record_seq JOIN datasets d ON d.seq = u.dataset_seq"
            " WHERE u.field_key = ? AND u.value = ? AND d.type_id = ? AND d.site_id = ?",
            (key.key, unique_value(key, _typed(key, shown)), target.id, site_id),
        ).fetchall()
    ids = {row["id"] for row in found}
    if len(ids) > 1:
        raise EngineError(
            "record_link_ambiguous",
            f"Поле «{field.name}»: «{shown}» подходит к нескольким записям типа «{target.name}»",
            field=field.key,
        )
    if not ids:
        raise EngineError(
            "record_link_missing",
            f"Поле «{field.name}»: запись «{shown}» типа «{target.name}» на площадке не найдена",
            field=field.key,
        )
    _check_target(field, found[0], site_id)
    return found[0]["id"]


def labels(conn: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    """Подписи записей для ``refs``: первое уникальное поле, иначе первое текстовое."""
    shown: dict[str, str] = {}
    pending = sorted(ids)
    schemas: dict[str, structure.TypeSchema] = {}
    for start in range(0, len(pending), 500):
        chunk = pending[start : start + 500]
        rows = conn.execute(
            "SELECT r.id, r.doc, d.type_id FROM records r JOIN datasets d ON d.seq = r.dataset_seq"
            f" WHERE r.id IN ({','.join('?' * len(chunk))})",
            chunk,
        ).fetchall()
        for row in rows:
            if row["type_id"] not in schemas:
                schemas[row["type_id"]] = structure.load_schema(conn, row["type_id"])
            shown[row["id"]] = label(schemas[row["type_id"]], json.loads(row["doc"]), row["id"])
    return shown


def label(schema: structure.TypeSchema, doc: dict[str, Any], record_id: str) -> str:
    fields = schema.active()
    ordered = [f for f in fields if f.unique_values] + [f for f in fields if f.kind == "text"]
    for field in ordered:
        value = doc.get(field.key)
        if value not in (None, "", []):
            return str(value[0] if isinstance(value, list) else value)
    return record_id[:8]


def backlinks(conn: sqlite3.Connection, record_seq: int) -> list[dict[str, Any]]:
    """Кто ссылается на запись: реестр, запись, поле. Не больше ``MAX_BACKLINKS``."""
    rows = conn.execute(
        "SELECT r.id AS record_id, d.id AS dataset_id, l.field_key FROM record_links l"
        " JOIN records r ON r.seq = l.record_seq JOIN datasets d ON d.seq = r.dataset_seq"
        " WHERE l.target_seq = ? ORDER BY d.name, r.seq LIMIT ?",
        (record_seq, MAX_BACKLINKS),
    ).fetchall()
    return [dict(row) for row in rows]


def _check_target(field: structure.Field, row: sqlite3.Row, site_id: str) -> None:
    if row["type_id"] != field.target_type_id:
        raise EngineError(
            "record_link_type", f"Поле «{field.name}»: запись другого типа", field=field.key
        )
    if row["site_id"] != site_id:
        raise EngineError(
            "record_link_site", f"Поле «{field.name}»: запись другой площадки", field=field.key
        )
    if row["archived_at"]:
        raise EngineError(
            "record_link_archived", f"Поле «{field.name}»: запись в архиве", field=field.key
        )


def _typed(field: structure.Field, shown: str) -> Any:
    """Текст ключа в виде значения поля: номер кабеля 5025 мог прийти строкой."""
    if field.kind in ("integer", "number"):
        try:
            number = float(shown.replace(",", "."))
        except ValueError:
            return shown
        return int(number) if number.is_integer() else number
    return shown


def _taken(
    conn: sqlite3.Connection, field: structure.Field, dataset_seq: int, value: str, shown: Any
) -> EngineError:
    row = conn.execute(
        "SELECT r.id FROM record_unique u JOIN records r ON r.seq = u.record_seq"
        " WHERE u.dataset_seq = ? AND u.field_key = ? AND u.value = ?",
        (dataset_seq, field.key, value),
    ).fetchone()
    return EngineError(
        "value_not_unique",
        f"Значение «{shown}» поля «{field.name}» уже есть в другой записи реестра",
        status=CONFLICT,
        field=field.key,
        details={"record_id": row[0] if row else None},
    )
