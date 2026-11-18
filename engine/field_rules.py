"""Проверки свойств поля: ссылка на запись, уникальность, контакт (DATA-12…14)."""

from __future__ import annotations

import sqlite3

from engine.errors import EngineError, not_found

UNIQUE_KINDS = ("text", "integer", "number", "date", "kks")


def target_type(
    conn: sqlite3.Connection, kind: str, target_type_id: str | None, sortable: bool
) -> str | None:
    if kind != "record":
        if target_type_id is not None:
            raise EngineError(
                "field_target",
                "Тип цели задаётся только для ссылки на запись",
                field="target_type_id",
            )
        return None
    if not target_type_id:
        raise EngineError(
            "field_target", "Для ссылки на запись нужен тип записей-целей", field="target_type_id"
        )
    if conn.execute("SELECT 1 FROM types WHERE id = ?", (target_type_id,)).fetchone() is None:
        raise not_found("type", target_type_id)
    if sortable:
        raise EngineError("field_sort_record", "По ссылке на запись сортировать нельзя")
    return target_type_id


def check_unique(kind: str, multiple: bool, unique: bool) -> None:
    if unique and (multiple or kind not in UNIQUE_KINDS):
        raise EngineError(
            "field_unique",
            "Уникальным может быть одно значение вида текст, число, дата или ККС",
            field="unique",
        )


def check_contact(
    conn: sqlite3.Connection,
    type_id: str,
    kind: str,
    contact_for: str | None,
    own_key: str | None = None,
) -> str | None:
    """Контакт — текстовое поле, которое подписывает связь поля ККС или ссылки (DATA-14)."""
    if contact_for is None:
        return None
    if kind != "text":
        raise EngineError("field_contact", "Контактом может быть только текстовое поле")
    row = conn.execute(
        "SELECT kind FROM fields WHERE type_id = ? AND key = ? AND archived_at IS NULL",
        (type_id, contact_for),
    ).fetchone()
    if row is None or row["kind"] not in ("kks", "record") or contact_for == own_key:
        raise EngineError(
            "field_contact",
            f"Поле «{contact_for}» не найдено среди полей ККС и ссылок этого типа",
            field="contact_for",
        )
    return contact_for
