"""Небольшая база в форме Local_DB для тестов импортёра.

Описаны только таблицы и столбцы, которые читает импортёр. Значения пишутся
так же, как их хранит Local_DB: значения списков — ссылкой на справочник,
снимки версий — полным набором значений в виде текста.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,
    checksum TEXT NOT NULL, applied_at TEXT NOT NULL);
CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE data_tables (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE dictionary_sets (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE dictionary_values (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
    dictionary_id INTEGER NOT NULL, canonical_value TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
    merged_into_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE dictionary_aliases (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
    dictionary_id INTEGER NOT NULL, dictionary_value_id INTEGER NOT NULL,
    alias_value TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    created_by TEXT NOT NULL);
CREATE TABLE field_definitions (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
    table_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL, data_type TEXT NOT NULL,
    input_mode TEXT NOT NULL DEFAULT 'free', searchable INTEGER NOT NULL DEFAULT 0,
    filterable INTEGER NOT NULL DEFAULT 0, multiple INTEGER NOT NULL DEFAULT 0,
    required INTEGER NOT NULL DEFAULT 0, is_calculated INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
    dictionary_id INTEGER, archived_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    visible INTEGER NOT NULL DEFAULT 1, UNIQUE(table_id, code));
CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
    table_id INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    created_by TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
    archived_at TEXT);
CREATE TABLE record_values (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id INTEGER NOT NULL,
    field_id INTEGER NOT NULL, ordinal INTEGER NOT NULL DEFAULT 0, value_text TEXT,
    value_integer INTEGER, value_decimal TEXT, value_date TEXT, value_boolean INTEGER,
    dictionary_value_id INTEGER, created_at TEXT NOT NULL, UNIQUE(record_id, field_id, ordinal));
CREATE TABLE record_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id INTEGER NOT NULL,
    version INTEGER NOT NULL, operation TEXT NOT NULL, snapshot_json TEXT NOT NULL,
    actor_name TEXT NOT NULL, ip_address TEXT, mode TEXT NOT NULL, occurred_at TEXT NOT NULL,
    restored_from_version INTEGER, UNIQUE(record_id, version));
CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL, actor_name TEXT NOT NULL, ip_address TEXT, mode TEXT NOT NULL,
    operation TEXT NOT NULL, entity_type TEXT NOT NULL, entity_uuid TEXT, field_code TEXT,
    before_json TEXT, after_json TEXT, metadata_json TEXT);
CREATE TABLE record_attachments (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL,
    record_uuid TEXT NOT NULL, size_bytes INTEGER NOT NULL);
"""

NOW = "2026-01-15T08:00:00+00:00"
SCALAR_COLUMN = {
    "text": "value_text",
    "integer": "value_integer",
    "decimal": "value_decimal",
    "date": "value_date",
    "boolean": "value_boolean",
}


class LocalDb:
    """Построитель тестовой базы Local_DB."""

    def __init__(self, path: Path, *, schema: int = 34, database_uuid: str | None = None) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("INSERT INTO schema_migrations VALUES (?, 'x', 'x', ?)", (schema, NOW))
        self.conn.execute(
            "INSERT INTO app_metadata VALUES ('database_uuid', ?, ?)",
            (database_uuid or str(uuid.uuid4()), NOW),
        )
        self.conn.execute(
            "INSERT INTO app_metadata VALUES ('starter_profile', 'asu_tp', ?)", (NOW,)
        )
        self.table_uuid = str(uuid.uuid4())
        self.table_id = self._insert(
            "data_tables",
            uuid=self.table_uuid,
            code="equipment",
            name="Оборудование",
            created_at=NOW,
            updated_at=NOW,
        )
        self.fields: dict[str, dict[str, Any]] = {}
        self.dictionaries: dict[str, int] = {}
        self.records: dict[str, int] = {}

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # Структура ----------------------------------------------------------------

    def field(
        self,
        code: str,
        name: str,
        data_type: str,
        *,
        multiple: bool = False,
        input_mode: str = "free",
        active: bool = True,
        visible: bool = True,
        searchable: bool = True,
        filterable: bool = True,
    ) -> None:
        dictionary_id = None
        if data_type == "smartfield":
            dictionary_id = self._insert(
                "dictionary_sets",
                uuid=str(uuid.uuid4()),
                code=code,
                name=name,
                created_at=NOW,
                updated_at=NOW,
            )
            self.dictionaries[code] = dictionary_id
        field_uuid = str(uuid.uuid4())
        field_id = self._insert(
            "field_definitions",
            uuid=field_uuid,
            table_id=self.table_id,
            code=code,
            name=name,
            data_type=data_type,
            input_mode=input_mode,
            multiple=int(multiple),
            searchable=int(searchable),
            filterable=int(filterable),
            active=int(active),
            visible=int(visible),
            sort_order=len(self.fields),
            dictionary_id=dictionary_id,
            created_at=NOW,
            updated_at=NOW,
        )
        self.fields[code] = {
            "id": field_id,
            "uuid": field_uuid,
            "type": data_type,
            "multiple": multiple,
            "dictionary_id": dictionary_id,
        }

    def value(
        self,
        code: str,
        text: str,
        *,
        active: bool = True,
        merged_into: int | None = None,
        aliases: tuple[str, ...] = (),
    ) -> int:
        dictionary_id = self.dictionaries[code]
        found = self.conn.execute(
            "SELECT id FROM dictionary_values WHERE dictionary_id = ? AND canonical_value = ?",
            (dictionary_id, text),
        ).fetchone()
        if found:
            return found[0]
        value_id = self._insert(
            "dictionary_values",
            uuid=str(uuid.uuid4()),
            dictionary_id=dictionary_id,
            canonical_value=text,
            active=int(active),
            merged_into_id=merged_into,
            created_at=NOW,
            updated_at=NOW,
        )
        for alias in aliases:
            self._insert(
                "dictionary_aliases",
                uuid=str(uuid.uuid4()),
                dictionary_id=dictionary_id,
                dictionary_value_id=value_id,
                alias_value=alias,
                created_at=NOW,
                created_by="t",
            )
        return value_id

    # Записи -------------------------------------------------------------------

    def record(
        self,
        values: dict[str, Any],
        *,
        history: list[tuple[str, dict[str, Any]]] = (),
        archived: bool = False,
        actor: str = "Иванов",
    ) -> str:
        """Запись с текущими значениями; ``history`` — снимки более ранних версий."""
        record_uuid = str(uuid.uuid4())
        version = len(history) + 1
        record_id = self._insert(
            "records",
            uuid=record_uuid,
            table_id=self.table_id,
            version=version,
            created_at=NOW,
            created_by=actor,
            updated_at=f"2026-02-{version:02d}T09:00:00+00:00",
            updated_by=actor,
            archived_at=NOW if archived else None,
        )
        self.records[record_uuid] = record_id
        self._write_values(record_id, values)
        snapshots = [*history, ("UPDATE_RECORD" if history else "CREATE_RECORD", values)]
        for number, (operation, snapshot) in enumerate(snapshots, start=1):
            if number == 1:
                operation = "CREATE_RECORD"
            self._snapshot(record_id, number, operation, snapshot, actor)
        return record_uuid

    def update(self, record_uuid: str, values: dict[str, Any], *, actor: str = "Петров") -> None:
        """Новая версия записи, как её пишет Local_DB."""
        record_id = self.records[record_uuid]
        version = (
            self.conn.execute("SELECT version FROM records WHERE id = ?", (record_id,)).fetchone()[
                0
            ]
            + 1
        )
        self.conn.execute("DELETE FROM record_values WHERE record_id = ?", (record_id,))
        self._write_values(record_id, values)
        self.conn.execute(
            "UPDATE records SET version = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (version, f"2026-03-{version:02d}T09:00:00+00:00", actor, record_id),
        )
        self._snapshot(record_id, version, "UPDATE_RECORD", values, actor)

    def audit(self, operation: str, entity_uuid: str | None = None, actor: str = "Иванов") -> None:
        self._insert(
            "audit_events",
            uuid=str(uuid.uuid4()),
            occurred_at=NOW,
            actor_name=actor,
            mode="engineer",
            operation=operation,
            entity_type="RECORD",
            entity_uuid=entity_uuid,
            after_json=json.dumps({"ok": True}),
        )

    def raw_value(self, record_uuid: str, code: str, column: str, value: Any) -> None:
        """Значение в обход проверок — для повреждённых данных."""
        self._insert(
            "record_values",
            record_id=self.records[record_uuid],
            field_id=self.fields[code]["id"],
            ordinal=0,
            created_at=NOW,
            **{column: value},
        )

    def _write_values(self, record_id: int, values: dict[str, Any]) -> None:
        for code, raw in values.items():
            meta = self.fields[code]
            items = raw if isinstance(raw, list) else [raw]
            for ordinal, item in enumerate(items):
                row: dict[str, Any] = {
                    "record_id": record_id,
                    "field_id": meta["id"],
                    "ordinal": ordinal,
                    "created_at": NOW,
                }
                if meta["type"] == "smartfield":
                    row["dictionary_value_id"] = self.value(code, item)
                else:
                    row[SCALAR_COLUMN[meta["type"]]] = item
                self._insert("record_values", **row)

    def _snapshot(
        self, record_id: int, version: int, operation: str, values: dict[str, Any], actor: str
    ) -> None:
        self._insert(
            "record_versions",
            record_id=record_id,
            version=version,
            operation=operation,
            snapshot_json=json.dumps({"archived": False, "values": values}, ensure_ascii=False),
            actor_name=actor,
            mode="engineer",
            occurred_at=f"2026-02-{version:02d}T09:00:00.123456+00:00",
        )

    def _insert(self, table: str, **values: Any) -> int:
        columns = ", ".join(values)
        marks = ", ".join("?" * len(values))
        cursor = self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


def standard(path: Path) -> LocalDb:
    """Типовая база: два режима, ККС, справочники, история, архив, журнал."""
    base = LocalDb(path)
    base.field("database_mode", "Режим БД", "smartfield", input_mode="strict")
    base.field("kks", "ККС", "smartfield", multiple=True, input_mode="suggest")
    base.field("sensor", "Наименование", "smartfield", input_mode="suggest")
    base.field("manufacturer", "Производитель", "smartfield", input_mode="suggest")
    base.field("installed_qty", "Количество", "integer")
    base.field("power", "Мощность", "decimal")
    base.field("next_check_date", "Следующая дата проверки", "date")
    base.field("notes", "Примечание", "text", visible=False)
    base.field("old_code", "Старый шифр", "text", active=False)
    base.value("database_mode", "В эксплуатации")
    base.value("database_mode", "Склад")
    base.value("manufacturer", "Siemens", aliases=("Сименс",))
    abb = base.value("manufacturer", "ABB")
    base.value("manufacturer", "АББ старое", merged_into=abb)
    return base
