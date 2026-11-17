"""Площадки, типы, поля, реестры и справочники (DATA-01, DATA-03).

Всё создаётся данными, без изменения кода и без миграции схемы базы.
Названия только отображаются: связи идут по ID, ключ поля в документе
записи не меняется при переименовании.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from engine import text
from engine.database import Tx
from engine.errors import EngineError, not_found

FIELD_KINDS = ("text", "integer", "number", "boolean", "date", "choice", "kks")
KKS_ROLES = ("object", "signal", "route_start", "route_end", "route_via", "related")
INPUT_MODES = ("strict", "suggest")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FIELD_FLAGS = ("required", "searchable", "filterable", "sortable")


@dataclass(frozen=True)
class Field:
    id: str
    type_id: str
    key: str
    name: str
    kind: str
    multiple: bool
    required: bool
    searchable: bool
    filterable: bool
    sortable: bool
    dictionary_id: str | None
    input_mode: str | None
    kks_role: str | None
    position: int
    archived_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class TypeSchema:
    id: str
    name: str
    schema_rev: int
    fields: tuple[Field, ...]

    def active(self) -> tuple[Field, ...]:
        return tuple(field for field in self.fields if field.archived_at is None)

    def by_key(self, key: str) -> Field | None:
        for field in self.fields:
            if field.key == key:
                return field
        return None


# Площадки -----------------------------------------------------------------


def create_site(tx: Tx, name: str) -> dict[str, Any]:
    return _create_named(tx, "sites", "site", name, {})


def rename_site(tx: Tx, site_id: str, name: str) -> dict[str, Any]:
    return _rename(tx, "sites", "site", site_id, name)


def list_sites(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM sites ORDER BY name_norm")


def get_site(conn: sqlite3.Connection, site_id: str) -> dict[str, Any]:
    return _get(conn, "sites", "site", site_id)


# Типы и поля ----------------------------------------------------------------


def create_type(tx: Tx, name: str, description: str | None = None) -> dict[str, Any]:
    return _create_named(tx, "types", "type", name, {"description": description})


def rename_type(tx: Tx, type_id: str, name: str) -> dict[str, Any]:
    return _rename(tx, "types", "type", type_id, name)


def archive_type(tx: Tx, type_id: str) -> None:
    _get(tx.conn, "types", "type", type_id)
    live = tx.conn.execute(
        "SELECT 1 FROM datasets WHERE type_id = ? AND archived_at IS NULL", (type_id,)
    ).fetchone()
    if live:
        raise EngineError(
            "type_in_use", "У типа есть действующие реестры; сначала отправьте их в архив"
        )
    _set_archived(tx, "types", "type", type_id, True)


def list_types(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM types ORDER BY name_norm")


def get_type(conn: sqlite3.Connection, type_id: str) -> dict[str, Any]:
    """Тип вместе с полями в порядке показа."""
    found = _get(conn, "types", "type", type_id)
    found.pop("next_field_no")
    found["fields"] = [field.to_dict() for field in load_schema(conn, type_id).fields]
    return found


def load_schema(conn: sqlite3.Connection, type_id: str) -> TypeSchema:
    row = conn.execute(
        "SELECT id, name, schema_rev FROM types WHERE id = ?", (type_id,)
    ).fetchone()
    if row is None:
        raise not_found("type", type_id)
    rows = conn.execute(
        "SELECT * FROM fields WHERE type_id = ? ORDER BY position, key", (type_id,)
    ).fetchall()
    return TypeSchema(row["id"], row["name"], row["schema_rev"], tuple(map(_field, rows)))


def add_field(
    tx: Tx,
    type_id: str,
    name: str,
    kind: str,
    *,
    key: str | None = None,
    multiple: bool = False,
    required: bool = False,
    searchable: bool = True,
    filterable: bool = False,
    sortable: bool = False,
    dictionary_id: str | None = None,
    input_mode: str | None = None,
    kks_role: str | None = None,
) -> dict[str, Any]:
    owner = _get(tx.conn, "types", "type", type_id)
    shown = text.clean_name(name, "Название поля")
    if kind not in FIELD_KINDS:
        raise EngineError("field_kind", f"Неизвестный вид поля: {kind}", field="kind")
    if multiple and sortable:
        raise EngineError(
            "field_sort_multiple", "По полю с несколькими значениями сортировать нельзя"
        )
    dictionary_id, input_mode, kks_role = _kind_options(
        tx.conn, kind, dictionary_id, input_mode, kks_role
    )
    _check_field_name(tx.conn, type_id, shown, None)
    key = _field_key(tx.conn, owner, key)
    position = tx.conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM fields WHERE type_id = ?", (type_id,)
    ).fetchone()[0]
    field_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO fields (id, type_id, key, name, name_norm, kind, multiple, required,"
        " searchable, filterable, sortable, dictionary_id, input_mode, kks_role, position,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            field_id, type_id, key, shown, text.norm_name(shown), kind, int(multiple),
            int(required), int(searchable), int(filterable), int(sortable), dictionary_id,
            input_mode, kks_role, position, tx.now, tx.now,
        ),
    )  # fmt: skip
    _bump_schema(tx, type_id)
    sync_field_index(tx.conn, key)
    tx.audit("field.create", "field", field_id, details={"type_id": type_id, "name": shown})
    return get_field(tx.conn, field_id).to_dict()


def update_field(tx: Tx, field_id: str, **changes: Any) -> dict[str, Any]:
    """Меняет название, флаги, порядок и режим ввода. Вид и ключ поля неизменны."""
    field = get_field(tx.conn, field_id)
    allowed = {"name", "position", "input_mode", *_FIELD_FLAGS}
    unknown = set(changes) - allowed
    if unknown:
        raise EngineError(
            "field_change", f"Это свойство поля изменить нельзя: {sorted(unknown)[0]}"
        )
    values: dict[str, Any] = {}
    if "name" in changes:
        shown = text.clean_name(changes["name"], "Название поля")
        _check_field_name(tx.conn, field.type_id, shown, field_id)
        values.update(name=shown, name_norm=text.norm_name(shown))
    for flag in _FIELD_FLAGS:
        if flag in changes:
            values[flag] = int(bool(changes[flag]))
    if values.get("sortable") and field.multiple:
        raise EngineError(
            "field_sort_multiple", "По полю с несколькими значениями сортировать нельзя"
        )
    if "position" in changes:
        values["position"] = int(changes["position"])
    if "input_mode" in changes:
        if field.kind != "choice" or changes["input_mode"] not in INPUT_MODES:
            raise EngineError("field_input_mode", "Режим ввода задаётся только для списка")
        values["input_mode"] = changes["input_mode"]
    if not values:
        return field.to_dict()
    _update(tx, "fields", field_id, values)
    _bump_schema(tx, field.type_id)
    sync_field_index(tx.conn, field.key)
    if "searchable" in values and bool(values["searchable"]) != field.searchable:
        _fulltext().reindex_type(tx.conn, field.type_id)
    tx.audit("field.update", "field", field_id, details={"changed": sorted(values)})
    return get_field(tx.conn, field_id).to_dict()


def archive_field(tx: Tx, field_id: str, archived: bool = True) -> dict[str, Any]:
    """Архив поля сохраняет значения в записях; поле перестаёт показываться и искаться."""
    field = get_field(tx.conn, field_id)
    if (field.archived_at is not None) == archived:
        return field.to_dict()
    if not archived:
        _check_field_name(tx.conn, field.type_id, field.name, field_id)
    _set_archived(tx, "fields", "field", field_id, archived)
    _bump_schema(tx, field.type_id)
    sync_field_index(tx.conn, field.key)
    _fulltext().reindex_type(tx.conn, field.type_id)
    return get_field(tx.conn, field_id).to_dict()


def get_field(conn: sqlite3.Connection, field_id: str) -> Field:
    row = conn.execute("SELECT * FROM fields WHERE id = ?", (field_id,)).fetchone()
    if row is None:
        raise not_found("field", field_id)
    return _field(row)


def index_name(key: str) -> str:
    return f"ix_doc_{key}"


def sync_field_index(conn: sqlite3.Connection, key: str) -> None:
    """Индекс по выражению JSON для фильтруемого или сортируемого поля (DATA-03).

    Ключи полей повторяются между типами, поэтому индекс общий для ключа и
    всегда начинается с реестра. Справочные виды и поля с несколькими
    значениями ищутся иначе и индекса не получают.
    """
    needed = conn.execute(
        "SELECT 1 FROM fields WHERE key = ? AND archived_at IS NULL AND multiple = 0"
        " AND kind != 'kks' AND (filterable = 1 OR sortable = 1) LIMIT 1",
        (key,),
    ).fetchone()
    if not KEY_RE.match(key):
        raise EngineError("field_key", f"Недопустимый ключ поля: {key}")
    if needed:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name(key)} ON records"
            f" (dataset_seq, json_extract(doc, '$.{key}')) WHERE archived_at IS NULL"
        )
    else:
        conn.execute(f"DROP INDEX IF EXISTS {index_name(key)}")


# Реестры ------------------------------------------------------------------


def create_dataset(tx: Tx, site_id: str, type_id: str, name: str) -> dict[str, Any]:
    site = _get(tx.conn, "sites", "site", site_id)
    kind = _get(tx.conn, "types", "type", type_id)
    if site["archived_at"] or kind["archived_at"]:
        raise EngineError("dataset_parent_archived", "Площадка или тип находятся в архиве")
    shown = text.clean_name(name, "Название реестра")
    taken = tx.conn.execute(
        "SELECT 1 FROM datasets WHERE site_id = ? AND name_norm = ?",
        (site_id, text.norm_name(shown)),
    ).fetchone()
    if taken:
        raise EngineError(
            "name_taken", f"На этой площадке уже есть реестр «{shown}»", field="name"
        )
    dataset_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO datasets (id, site_id, type_id, name, name_norm, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?)",
        (dataset_id, site_id, type_id, shown, text.norm_name(shown), tx.now, tx.now),
    )
    tx.audit("dataset.create", "dataset", dataset_id, dataset_id=dataset_id,
             details={"name": shown, "type_id": type_id, "site_id": site_id})  # fmt: skip
    return get_dataset(tx.conn, dataset_id)


def rename_dataset(tx: Tx, dataset_id: str, name: str) -> dict[str, Any]:
    current = get_dataset(tx.conn, dataset_id)
    shown = text.clean_name(name, "Название реестра")
    taken = tx.conn.execute(
        "SELECT 1 FROM datasets WHERE site_id = ? AND name_norm = ? AND id != ?",
        (current["site_id"], text.norm_name(shown), dataset_id),
    ).fetchone()
    if taken:
        raise EngineError(
            "name_taken", f"На этой площадке уже есть реестр «{shown}»", field="name"
        )
    _update(tx, "datasets", dataset_id, {"name": shown, "name_norm": text.norm_name(shown)})
    tx.audit("dataset.rename", "dataset", dataset_id, dataset_id=dataset_id,
             details={"from": current["name"], "to": shown})  # fmt: skip
    return get_dataset(tx.conn, dataset_id)


def archive_dataset(tx: Tx, dataset_id: str, archived: bool = True) -> dict[str, Any]:
    get_dataset(tx.conn, dataset_id)
    _set_archived(tx, "datasets", "dataset", dataset_id, archived)
    return get_dataset(tx.conn, dataset_id)


def get_dataset(conn: sqlite3.Connection, dataset_id: str) -> dict[str, Any]:
    found = load_dataset(conn, dataset_id)
    found.pop("seq")
    return found


def load_dataset(conn: sqlite3.Connection, dataset_id: str) -> dict[str, Any]:
    """Реестр вместе с внутренним номером ``seq`` — для слоя записей и запросов."""
    return _get(conn, "datasets", "dataset", dataset_id)


def list_datasets(
    conn: sqlite3.Connection, *, site_id: str | None = None, type_id: str | None = None
) -> list[dict[str, Any]]:
    where, params = ["1 = 1"], []
    if site_id:
        where.append("site_id = ?")
        params.append(site_id)
    if type_id:
        where.append("type_id = ?")
        params.append(type_id)
    return _rows(
        conn, f"SELECT * FROM datasets WHERE {' AND '.join(where)} ORDER BY name_norm", params
    )


# Справочники ----------------------------------------------------------------


def create_dictionary(tx: Tx, name: str) -> dict[str, Any]:
    return _create_named(tx, "dictionaries", "dictionary", name, {})


def rename_dictionary(tx: Tx, dictionary_id: str, name: str) -> dict[str, Any]:
    return _rename(tx, "dictionaries", "dictionary", dictionary_id, name)


def list_dictionaries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(conn, "SELECT * FROM dictionaries ORDER BY name_norm")


def list_dictionary_values(
    conn: sqlite3.Connection, dictionary_id: str, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    _get(conn, "dictionaries", "dictionary", dictionary_id)
    tail = "" if include_archived else " AND archived_at IS NULL"
    rows = _rows(
        conn,
        f"SELECT * FROM dictionary_values WHERE dictionary_id = ?{tail}"
        " ORDER BY position, value_norm",
        (dictionary_id,),
    )
    for row in rows:
        row.pop("value_norm")
        row["synonyms"] = json.loads(row["synonyms"])
    return rows


def add_dictionary_value(
    tx: Tx, dictionary_id: str, value: str, synonyms: list[str] | None = None
) -> dict[str, Any]:
    _get(tx.conn, "dictionaries", "dictionary", dictionary_id)
    shown = text.clean_name(value, "Значение")
    extra = _clean_synonyms(synonyms or [], shown)
    _check_value_free(tx.conn, dictionary_id, [shown, *extra], None)
    position = tx.conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM dictionary_values"
        " WHERE dictionary_id = ?",
        (dictionary_id,),
    ).fetchone()[0]
    value_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO dictionary_values (id, dictionary_id, value, value_norm, synonyms,"
        " position, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (value_id, dictionary_id, shown, text.norm_name(shown),
         json.dumps(extra, ensure_ascii=False), position, tx.now, tx.now),
    )  # fmt: skip
    tx.audit("dictionary_value.create", "dictionary_value", value_id,
             details={"dictionary_id": dictionary_id, "value": shown})  # fmt: skip
    return get_dictionary_value(tx.conn, value_id)


def update_dictionary_value(
    tx: Tx,
    value_id: str,
    *,
    value: str | None = None,
    synonyms: list[str] | None = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    """Переименование значения сразу видно во всех записях: в них хранится ID."""
    current = get_dictionary_value(tx.conn, value_id)
    shown = text.clean_name(value, "Значение") if value is not None else current["value"]
    extra = current["synonyms"] if synonyms is None else _clean_synonyms(synonyms, shown)
    extra = [item for item in extra if text.norm_name(item) != text.norm_name(shown)]
    _check_value_free(tx.conn, current["dictionary_id"], [shown, *extra], value_id)
    values: dict[str, Any] = {
        "value": shown,
        "value_norm": text.norm_name(shown),
        "synonyms": json.dumps(extra, ensure_ascii=False),
    }
    if archived is not None:
        values["archived_at"] = tx.now if archived else None
    _update(tx, "dictionary_values", value_id, values)
    if shown != current["value"]:
        _fulltext().reindex_choice(tx.conn, current["dictionary_id"], value_id)
    tx.audit("dictionary_value.update", "dictionary_value", value_id,
             details={"from": current["value"], "to": shown})  # fmt: skip
    return get_dictionary_value(tx.conn, value_id)


def get_dictionary_value(conn: sqlite3.Connection, value_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM dictionary_values WHERE id = ?", (value_id,)).fetchone()
    if row is None:
        raise not_found("dictionary_value", value_id)
    found = dict(row)
    found.pop("value_norm")
    found["synonyms"] = json.loads(found["synonyms"])
    return found


# Внутреннее -----------------------------------------------------------------


def _fulltext() -> Any:
    # Индекс поиска строится по схеме типа, поэтому модуль подключается здесь.
    from engine import fulltext

    return fulltext


def _field(row: sqlite3.Row) -> Field:
    return Field(
        id=row["id"], type_id=row["type_id"], key=row["key"], name=row["name"],
        kind=row["kind"], multiple=bool(row["multiple"]), required=bool(row["required"]),
        searchable=bool(row["searchable"]), filterable=bool(row["filterable"]),
        sortable=bool(row["sortable"]), dictionary_id=row["dictionary_id"],
        input_mode=row["input_mode"], kks_role=row["kks_role"], position=row["position"],
        archived_at=row["archived_at"],
    )  # fmt: skip


def _kind_options(
    conn: sqlite3.Connection,
    kind: str,
    dictionary_id: str | None,
    input_mode: str | None,
    kks_role: str | None,
) -> tuple[str | None, str | None, str | None]:
    if kind == "choice":
        if not dictionary_id:
            raise EngineError(
                "field_dictionary", "Для поля-списка нужен справочник", field="dictionary_id"
            )
        _get(conn, "dictionaries", "dictionary", dictionary_id)
        mode = input_mode or "strict"
        if mode not in INPUT_MODES:
            raise EngineError("field_input_mode", f"Неизвестный режим ввода: {mode}")
        return dictionary_id, mode, None
    if kind == "kks":
        role = kks_role or "object"
        if role not in KKS_ROLES:
            raise EngineError("field_kks_role", f"Неизвестная роль связи ККС: {role}")
        return None, None, role
    return None, None, None


def _field_key(conn: sqlite3.Connection, owner: dict[str, Any], key: str | None) -> str:
    type_id = owner["id"]
    if key is not None:
        if not KEY_RE.match(key):
            raise EngineError(
                "field_key",
                "Ключ поля: латинские строчные буквы, цифры и «_», до 32 символов",
                field="key",
            )
        taken = conn.execute(
            "SELECT 1 FROM fields WHERE type_id = ? AND key = ?", (type_id, key)
        ).fetchone()
        if taken:
            raise EngineError("field_key_taken", f"Ключ «{key}» уже занят", field="key")
        return key
    number = owner["next_field_no"]
    while conn.execute(
        "SELECT 1 FROM fields WHERE type_id = ? AND key = ?", (type_id, f"f{number}")
    ).fetchone():
        number += 1
    conn.execute("UPDATE types SET next_field_no = ? WHERE id = ?", (number + 1, type_id))
    return f"f{number}"


def _check_field_name(
    conn: sqlite3.Connection, type_id: str, shown: str, except_id: str | None
) -> None:
    row = conn.execute(
        "SELECT id FROM fields WHERE type_id = ? AND name_norm = ? AND archived_at IS NULL",
        (type_id, text.norm_name(shown)),
    ).fetchone()
    if row and row["id"] != except_id:
        raise EngineError("name_taken", f"В типе уже есть поле «{shown}»", field="name")


def _clean_synonyms(synonyms: list[str], shown: str) -> list[str]:
    seen = {text.norm_name(shown)}
    cleaned = []
    for item in synonyms:
        value = text.clean_name(item, "Синоним")
        if text.norm_name(value) not in seen:
            seen.add(text.norm_name(value))
            cleaned.append(value)
    return cleaned


def _check_value_free(
    conn: sqlite3.Connection, dictionary_id: str, names: list[str], except_id: str | None
) -> None:
    """Значение и синонимы не должны совпадать с другими значениями справочника."""
    wanted = {text.norm_name(name): name for name in names}
    rows = conn.execute(
        "SELECT id, value_norm, synonyms FROM dictionary_values WHERE dictionary_id = ?",
        (dictionary_id,),
    ).fetchall()
    for row in rows:
        if row["id"] == except_id:
            continue
        used = {row["value_norm"], *map(text.norm_name, json.loads(row["synonyms"]))}
        clash = used & set(wanted)
        if clash:
            raise EngineError(
                "value_taken",
                f"В справочнике уже есть «{wanted[sorted(clash)[0]]}»",
                field="value",
            )


def _bump_schema(tx: Tx, type_id: str) -> None:
    tx.conn.execute(
        "UPDATE types SET schema_rev = schema_rev + 1, updated_at = ? WHERE id = ?",
        (tx.now, type_id),
    )


_TABLES = {"sites", "types", "fields", "datasets", "dictionaries", "dictionary_values"}


def _create_named(
    tx: Tx, table: str, kind: str, name: str, extra: dict[str, Any]
) -> dict[str, Any]:
    shown = text.clean_name(name)
    _check_name_free(tx.conn, table, shown, None)
    entity_id = text.new_id()
    columns = ["id", "name", "name_norm", "created_at", "updated_at", *extra]
    values = [entity_id, shown, text.norm_name(shown), tx.now, tx.now, *extra.values()]
    marks = ",".join("?" * len(columns))
    tx.conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})", values)
    tx.audit(f"{kind}.create", kind, entity_id, details={"name": shown})
    return _get(tx.conn, table, kind, entity_id)


def _rename(tx: Tx, table: str, kind: str, entity_id: str, name: str) -> dict[str, Any]:
    current = _get(tx.conn, table, kind, entity_id)
    shown = text.clean_name(name)
    _check_name_free(tx.conn, table, shown, entity_id)
    _update(tx, table, entity_id, {"name": shown, "name_norm": text.norm_name(shown)})
    tx.audit(f"{kind}.rename", kind, entity_id, details={"from": current["name"], "to": shown})
    return _get(tx.conn, table, kind, entity_id)


def _check_name_free(
    conn: sqlite3.Connection, table: str, shown: str, except_id: str | None
) -> None:
    row = conn.execute(
        f"SELECT id FROM {table} WHERE name_norm = ?", (text.norm_name(shown),)
    ).fetchone()
    if row and row["id"] != except_id:
        raise EngineError("name_taken", f"Название «{shown}» уже занято", field="name")


def _set_archived(tx: Tx, table: str, kind: str, entity_id: str, archived: bool) -> None:
    _update(tx, table, entity_id, {"archived_at": tx.now if archived else None})
    tx.audit(f"{kind}.{'archive' if archived else 'restore'}", kind, entity_id)


def _update(tx: Tx, table: str, entity_id: str, values: dict[str, Any]) -> None:
    assert table in _TABLES
    sets = ", ".join(f"{column} = ?" for column in values)
    tx.conn.execute(
        f"UPDATE {table} SET {sets}, updated_at = ? WHERE id = ?",
        (*values.values(), tx.now, entity_id),
    )


def _get(conn: sqlite3.Connection, table: str, kind: str, entity_id: str) -> dict[str, Any]:
    assert table in _TABLES
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise not_found(kind, entity_id)
    found = dict(row)
    found.pop("name_norm", None)
    return found


def _rows(conn: sqlite3.Connection, sql: str, params: Any = ()) -> list[dict[str, Any]]:
    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    for row in rows:
        row.pop("name_norm", None)
        row.pop("next_field_no", None)
        row.pop("seq", None)
    return rows
