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
    kind: Literal["text", "integer", "number", "boolean", "date", "choice", "kks"]
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


class FieldPatch(Body):
    name: str | None = None
    position: StrictInt | None = None
    required: bool | None = None
    searchable: bool | None = None
    filterable: bool | None = None
    sortable: bool | None = None
    input_mode: Literal["strict", "suggest"] | None = None


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
