"""Тела запросов API версии 1. Лишние поля отклоняются."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NameBody(Body):
    name: str


class TypeCreate(Body):
    name: str
    description: str | None = None


class FieldCreate(Body):
    name: str
    kind: Literal["text", "integer", "number", "boolean", "date", "choice", "kks", "record"]
    key: str | None = None
    multiple: bool = False
    required: bool = False
    searchable: bool = True
    filterable: bool = False
    sortable: bool = False
    dictionary_id: str | None = None
    input_mode: Literal["strict", "suggest"] | None = None
    kks_role: (
        Literal["object", "signal", "route_start", "route_end", "route_via", "related"] | None
    ) = None
    unique: bool = False
    target_type_id: str | None = None
    contact_for: str | None = None


class FieldPatch(Body):
    name: str | None = None
    position: StrictInt | None = None
    required: bool | None = None
    searchable: bool | None = None
    filterable: bool | None = None
    sortable: bool | None = None
    input_mode: Literal["strict", "suggest"] | None = None
    unique: bool | None = None
    contact_for: str | None = None  # пустая строка снимает связь с полем


class DatasetCreate(Body):
    site_id: str
    type_id: str
    name: str


class ValueCreate(Body):
    value: str
    synonyms: list[str] = Field(default_factory=list)


class ValuePatch(Body):
    value: str | None = None
    synonyms: list[str] | None = None
    archived: bool | None = None


class RecordQuery(Body):
    """Запрос на языке запросов: docs/contract/query-language.md."""

    where: list[dict[str, Any]] | None = None
    search: str | None = None
    sort: dict[str, Any] | list[dict[str, Any]] | None = None
    limit: StrictInt | None = None
    cursor: str | None = None
    archived: bool | Literal["any"] = False
    kks: str | None = None


class RecordCount(Body):
    where: list[dict[str, Any]] | None = None
    search: str | None = None
    archived: bool | Literal["any"] = False
    kks: str | None = None


class ValueCounts(RecordCount):
    """Число записей по значениям поля при тех же условиях, что у подсчёта."""

    field: str
    limit: StrictInt = 50


class BatchItem(Body):
    id: str
    expected_version: StrictInt


class BatchUpdate(Body):
    items: list[BatchItem]
    values: dict[str, Any]


class BatchPurge(Body):
    items: list[BatchItem]


class RecordCreate(Body):
    values: dict[str, Any]


class RecordPatch(Body):
    expected_version: StrictInt
    values: dict[str, Any]


class VersionBody(Body):
    expected_version: StrictInt


class RollbackBody(Body):
    expected_version: StrictInt
    to_version: StrictInt


class KksCreate(Body):
    code: str
    name: str | None = None


class KksPatch(Body):
    code: str | None = None
    name: str | None = None
    archived: bool | None = None


class AliasCreate(Body):
    alias: str


# Доступ ---------------------------------------------------------------------


class Login(Body):
    login: str
    password: str


class PasswordChange(Body):
    current_password: str
    new_password: str


class RoleCreate(Body):
    name: str
    description: str | None = None
    copy_from: str | None = None


class RolePatch(Body):
    name: str | None = None
    description: str | None = None


class Permission(Body):
    scope: Literal["section", "dataset", "field", "doc_type"]
    target: str
    action: str


class Permissions(Body):
    permissions: list[Permission]


class UserCreate(Body):
    login: str
    name: str
    role_id: str


class UserPatch(Body):
    name: str | None = None
    role_id: str | None = None


class BackupCreate(Body):
    password: str
    label: str | None = None


class BackupRestore(Body):
    password: str


class NetworkCreate(Body):
    network: str
    note: str | None = None


class KksRules(Body):
    signal_pattern: str


class ChatPost(Body):
    text: str


class ChatEdit(Body):
    text: str
    version: StrictInt


class ChatHide(Body):
    reason: str | None = None


class ChatRead(Body):
    seq: StrictInt


class ImportNewField(Body):
    column: str
    name: str | None = None
    kind: Literal["text", "integer", "number", "date", "boolean", "kks"] = "text"
    unique: bool = False


class ImportNewType(Body):
    type_name: str
    dataset_name: str
    site_id: str
    fields: list[ImportNewField]


class ImportPlan(Body):
    sheet: str
    header_row: StrictInt = 1
    mode: Literal["records", "new_type", "magnet"]
    dataset_id: str | None = None
    mapping: dict[str, str] = {}
    key: str | None = None
    new_type: ImportNewType | None = None
    site_id: str | None = None
    name: str | None = None
    layout: dict[str, Any] | None = None


class ImportDecision(Body):
    decision: Literal["skip", "apply"]


class ImportApply(Body):
    save_profile: str | None = None


class ExportRequest(Body):
    format: Literal["xlsx", "csv", "json"] = "xlsx"
    query: RecordCount | None = None
    sort: dict[str, Any] | list[dict[str, Any]] | None = None
    columns: list[str] | None = None
    headers: dict[str, str] | None = None
    group_by: str | None = None
    template: bool = True
