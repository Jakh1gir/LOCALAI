"""Чтение базы Local_DB: только через проверенную копию, оригинал не меняется (MIG-02).

Модуль знает схему Local_DB ровно настолько, насколько её нужно прочитать
для переноса. Код Local_DB не используется: схема описана здесь заново.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from engine.errors import EngineError

DATABASE_FILE = "local_db.sqlite3"
# Схемы, которые умеет читать импортёр. Нижняя граница — миграция 005
# (видимость полей); верхняя — последняя выпущенная (034). Кандидаты 035–038
# не выпускались. Перечень реально используемых версий уточняется по рабочим базам.
MIN_SCHEMA = 5
MAX_SCHEMA = 34
REQUIRED_TABLES = (
    "schema_migrations", "app_metadata", "data_tables", "field_definitions", "records",
    "record_values", "record_versions", "dictionary_sets", "dictionary_values",
    "dictionary_aliases", "audit_events",
)  # fmt: skip
BATCH = 500


def _register_functions(conn: sqlite3.Connection) -> None:
    """SQL-функции, на которые опираются триггеры и индексы схемы Local_DB.

    Без них проверка целостности и запросы по таким индексам завершаются
    ошибкой «no such function». Поведение повторяет описание из схемы:
    свёртка регистра и та же свёртка с обрезкой пробелов.
    """
    conn.create_function(
        "LOCALDB_FOLD", 1, lambda value: "" if value is None else str(value).casefold(),
        deterministic=True,
    )  # fmt: skip
    conn.create_function(
        "LOCALDB_IDENTIFIER", 1,
        lambda value: "" if value is None else str(value).strip().casefold(),
        deterministic=True,
    )  # fmt: skip


@dataclass
class SourceValue:
    id: int
    uuid: str
    dictionary_id: int
    canonical: str
    active: bool
    merged_into: int | None
    aliases: list[str] = field(default_factory=list)


@dataclass
class SourceDictionary:
    id: int
    uuid: str
    code: str
    name: str
    values: list[SourceValue] = field(default_factory=list)


@dataclass
class SourceField:
    id: int
    uuid: str
    code: str
    name: str
    data_type: str
    input_mode: str
    searchable: bool
    filterable: bool
    multiple: bool
    required: bool
    calculated: bool
    active: bool
    visible: bool
    sort_order: int
    dictionary_id: int | None


@dataclass
class SourceTable:
    id: int
    uuid: str
    code: str
    name: str
    active: bool
    fields: list[SourceField] = field(default_factory=list)


@dataclass
class SourceVersion:
    version: int
    operation: str
    archived: bool
    values: dict[str, Any]
    actor: str
    at: str
    restored_from: int | None


@dataclass
class SourceRecord:
    id: int
    uuid: str
    version: int
    created_at: str
    created_by: str
    updated_at: str
    updated_by: str
    archived_at: str | None
    # (id поля, позиция, значение, id значения справочника)
    values: list[tuple[int, int, Any, int | None]]
    versions: list[SourceVersion]


class Source:
    """Открытая копия базы Local_DB. Используется как контекстный менеджер."""

    def __init__(self, path: str | Path) -> None:
        self.path = _database_file(Path(path))
        self._tmp = tempfile.TemporaryDirectory(prefix="localai-import-")
        self.conn = _read_only_copy(self.path, Path(self._tmp.name) / "source.sqlite3")
        self._check_tables()
        self.schema_version = self._scalar("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        meta = dict(self.conn.execute("SELECT key, value FROM app_metadata").fetchall())
        self.database_uuid = str(meta.get("database_uuid") or "")
        self.profile = str(meta.get("starter_profile") or "")
        self.dictionaries = self._dictionaries()
        self.tables = self._tables()

    def __enter__(self) -> Source:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def check_schema(self) -> None:
        if self.schema_version < MIN_SCHEMA:
            raise EngineError(
                "localdb_schema_old",
                f"База Local_DB слишком старая (схема {self.schema_version}). Обновите Local_DB"
                f" до версии со схемой {MIN_SCHEMA} или новее и повторите перенос.",
            )
        if self.schema_version > MAX_SCHEMA:
            raise EngineError(
                "localdb_schema_new",
                f"Схема базы Local_DB {self.schema_version} новее известной импортёру"
                f" ({MAX_SCHEMA}). Нужна новая версия импортёра.",
            )

    # Чтение записей ---------------------------------------------------------------

    def record_count(self, table_id: int) -> int:
        return self._scalar("SELECT COUNT(*) FROM records WHERE table_id = ?", (table_id,))

    def records(self, table_id: int) -> Iterator[list[SourceRecord]]:
        """Записи таблицы пачками по ``BATCH`` вместе со значениями и снимками версий."""
        last = 0
        while True:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE table_id = ? AND id > ? ORDER BY id LIMIT ?",
                (table_id, last, BATCH),
            ).fetchall()
            if not rows:
                return
            last = rows[-1]["id"]
            ids = [row["id"] for row in rows]
            values = self._values(ids)
            versions = self._versions(ids)
            yield [
                SourceRecord(
                    id=row["id"], uuid=str(row["uuid"]).lower(), version=row["version"],
                    created_at=row["created_at"], created_by=row["created_by"],
                    updated_at=row["updated_at"], updated_by=row["updated_by"],
                    archived_at=row["archived_at"], values=values.get(row["id"], []),
                    versions=versions.get(row["id"], []),
                )  # fmt: skip
                for row in rows
            ]

    def audit_events(self, after_id: int) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT * FROM audit_events WHERE id > ? ORDER BY id", (after_id,)
        )

    def other_counts(self) -> dict[str, dict[str, Any]]:
        """Данные, которые переносятся на следующих этапах: только счёт для отчёта."""
        groups = {
            "files": ("record_attachments", 4, "Файлы и вложения"),
            "file_links": ("file_record_links", 4, "Связи файлов с записями"),
            "document_types": ("document_types", 4, "Типы документов"),
            "scheme_evidence": ("scheme_kks_evidence", 6, "Места ККС на схемах"),
            "roles": ("access_role_state", 2, "Роли"),
            "role_table_rights": ("access_role_table_permissions", 2, "Права ролей по таблицам"),
            "role_field_rights": ("access_role_field_permissions", 2, "Права ролей по полям"),
            "document_rights": ("document_role_permissions", 2, "Права ролей по документам"),
            "allowed_ips": ("allowed_ips", 2, "Разрешённые IP-адреса"),
            "inspections": ("record_inspection_settings", 7, "Настройки проверок"),
            "timesheet_employees": ("timesheet_employees", 7, "Табель: сотрудники"),
            "timesheet_entries": ("timesheet_entries", 7, "Табель: записи"),
            "column_profiles": ("equipment_column_view_profiles", 3, "Профили столбцов"),
            "backups": ("database_backups", 2, "Резервные копии"),
        }
        found: dict[str, dict[str, Any]] = {}
        for key, (table, stage, title) in groups.items():
            if self._has_table(table):
                count = self._scalar(f"SELECT COUNT(*) FROM {table}")
                if count:
                    found[key] = {"title": title, "count": count, "stage": stage}
        if "files" in found:
            found["files"]["bytes"] = self._scalar(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM record_attachments"
            )
        return found

    # Внутреннее -------------------------------------------------------------------

    def _check_tables(self) -> None:
        missing = [name for name in REQUIRED_TABLES if not self._has_table(name)]
        if missing:
            raise EngineError(
                "localdb_not_recognized",
                f"Файл не похож на базу Local_DB: нет таблиц {', '.join(missing[:4])}",
            )

    def _has_table(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _scalar(self, sql: str, params: Any = ()) -> Any:
        return self.conn.execute(sql, params).fetchone()[0]

    def _dictionaries(self) -> dict[int, SourceDictionary]:
        found = {
            row["id"]: SourceDictionary(row["id"], str(row["uuid"]).lower(), row["code"], row["name"])
            for row in self.conn.execute("SELECT * FROM dictionary_sets ORDER BY id")
        }
        values: dict[int, SourceValue] = {}
        for row in self.conn.execute("SELECT * FROM dictionary_values ORDER BY id"):
            value = SourceValue(
                id=row["id"], uuid=str(row["uuid"]).lower(), dictionary_id=row["dictionary_id"],
                canonical=row["canonical_value"] or "", active=bool(row["active"]),
                merged_into=row["merged_into_id"],
            )  # fmt: skip
            values[value.id] = value
            if value.dictionary_id in found:
                found[value.dictionary_id].values.append(value)
        for row in self.conn.execute(
            "SELECT dictionary_value_id, alias_value FROM dictionary_aliases"
            " WHERE active = 1 ORDER BY id"
        ):
            if row[0] in values and row[1]:
                values[row[0]].aliases.append(row[1])
        return found

    def _tables(self) -> list[SourceTable]:
        has_visible = "visible" in self._columns("field_definitions")
        tables = []
        for row in self.conn.execute("SELECT * FROM data_tables ORDER BY id"):
            table = SourceTable(
                row["id"], str(row["uuid"]).lower(), row["code"], row["name"], bool(row["active"])
            )
            for item in self.conn.execute(
                "SELECT * FROM field_definitions WHERE table_id = ? ORDER BY sort_order, id",
                (table.id,),
            ):
                table.fields.append(
                    SourceField(
                        id=item["id"], uuid=str(item["uuid"]).lower(), code=item["code"],
                        name=item["name"], data_type=item["data_type"],
                        input_mode=item["input_mode"], searchable=bool(item["searchable"]),
                        filterable=bool(item["filterable"]), multiple=bool(item["multiple"]),
                        required=bool(item["required"]), calculated=bool(item["is_calculated"]),
                        active=bool(item["active"]) and item["archived_at"] is None,
                        visible=bool(item["visible"]) if has_visible else True,
                        sort_order=item["sort_order"], dictionary_id=item["dictionary_id"],
                    )  # fmt: skip
                )
            tables.append(table)
        return tables

    def _values(self, ids: list[int]) -> dict[int, list[tuple[int, int, Any, int | None]]]:
        marks = ",".join("?" * len(ids))
        found: dict[int, list[tuple[int, int, Any, int | None]]] = {}
        for row in self.conn.execute(
            "SELECT record_id, field_id, ordinal, value_text, value_integer, value_decimal,"
            " value_date, value_boolean, dictionary_value_id FROM record_values"
            f" WHERE record_id IN ({marks}) ORDER BY record_id, field_id, ordinal",
            ids,
        ):
            found.setdefault(row["record_id"], []).append(
                (row["field_id"], row["ordinal"], _scalar_of(row), row["dictionary_value_id"])
            )
        return found

    def _versions(self, ids: list[int]) -> dict[int, list[SourceVersion]]:
        marks = ",".join("?" * len(ids))
        found: dict[int, list[SourceVersion]] = {}
        for row in self.conn.execute(
            f"SELECT * FROM record_versions WHERE record_id IN ({marks}) ORDER BY record_id, version",
            ids,
        ):
            try:
                snapshot = json.loads(row["snapshot_json"])
            except (TypeError, ValueError):
                snapshot = {}
            values = snapshot.get("values") if isinstance(snapshot, dict) else None
            found.setdefault(row["record_id"], []).append(
                SourceVersion(
                    version=row["version"], operation=row["operation"],
                    archived=bool(snapshot.get("archived")) if isinstance(snapshot, dict) else False,
                    values=values if isinstance(values, dict) else {},
                    actor=row["actor_name"], at=row["occurred_at"],
                    restored_from=row["restored_from_version"],
                )  # fmt: skip
            )
        return found


def _scalar_of(row: sqlite3.Row) -> Any:
    for column in ("value_text", "value_integer", "value_decimal", "value_date"):
        if row[column] is not None:
            return row[column]
    if row["value_boolean"] is not None:
        return bool(row["value_boolean"])
    return None


def _database_file(path: Path) -> Path:
    """Каталог данных или сам файл базы."""
    candidate = path / DATABASE_FILE if path.is_dir() else path
    if not candidate.is_file():
        raise EngineError(
            "localdb_not_found",
            f"Не найден файл базы Local_DB: {candidate}. Укажите каталог данных или файл"
            f" {DATABASE_FILE}.",
        )
    if candidate.suffix.lower() == ".ldbpack":
        raise EngineError(
            "localdb_pack_unsupported",
            "Шифрованные пакеты .ldbpack пока не поддерживаются: укажите каталог данных"
            " Local_DB или восстановленный файл базы.",
        )
    return candidate


def _read_only_copy(path: Path, target: Path) -> sqlite3.Connection:
    """Согласованная копия источника через API резервного копирования SQLite.

    Источник открывается в режиме только чтения. Копия делается, чтобы
    чтение не держало блокировку на рабочей базе Local_DB и видело один
    момент времени, даже если Local_DB продолжает работать.
    """
    # Если файла журнала WAL нет, Local_DB не запущена и базу можно открыть как
    # неизменяемую: так SQLite не создаёт рядом с ней служебных файлов. Если
    # журнал есть, Local_DB работает или завершилась аварийно, и его нужно прочитать.
    wal = path.with_name(path.name + "-wal")
    options = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
    uri = f"file:{quote(str(path.resolve()))}?{options}"
    try:
        source = sqlite3.connect(uri, uri=True)
        _register_functions(source)
        try:
            copy = sqlite3.connect(target)
            source.backup(copy)
            copy.close()
        finally:
            source.close()
    except sqlite3.Error as error:
        raise EngineError(
            "localdb_unreadable", f"Не удалось прочитать базу Local_DB: {error}"
        ) from error
    conn = sqlite3.connect(target)
    _register_functions(conn)
    conn.row_factory = sqlite3.Row
    verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if verdict != "ok":
        conn.close()
        raise EngineError(
            "localdb_damaged", f"База Local_DB повреждена: {verdict}. Перенос не начат."
        )
    return conn
