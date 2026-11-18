"""Маршруты структуры: площадки, типы, поля, реестры, справочники."""

from __future__ import annotations

from typing import Any

from engine import structure
from server import schemas
from server.api import Call, Endpoint

# Структуру видит каждый вошедший: без неё не показать реестр. Меняет —
# только роль с разделом «Структура».
READ, WRITE = "session", "section.structure"


def list_sites(call: Call) -> dict[str, Any]:
    """Список площадок."""
    return {"items": structure.list_sites(call.conn)}


def create_site(call: Call) -> dict[str, Any]:
    """Создать площадку."""
    return structure.create_site(call.tx, call.body.name)


def rename_site(call: Call) -> dict[str, Any]:
    """Переименовать площадку."""
    return structure.rename_site(call.tx, call.params["id"], call.body.name)


def list_types(call: Call) -> dict[str, Any]:
    """Список типов без полей."""
    return {"items": structure.list_types(call.conn)}


def create_type(call: Call) -> dict[str, Any]:
    """Создать тип."""
    return structure.create_type(call.tx, call.body.name, call.body.description)


def get_type(call: Call) -> dict[str, Any]:
    """Тип с полями в порядке показа; скрытые от роли поля не показываются."""
    found = structure.get_type(call.conn, call.params["id"])
    if not _manages(call):
        hidden = call.access.hidden
        found["fields"] = [item for item in found["fields"] if item["id"] not in hidden]
    return found


def rename_type(call: Call) -> dict[str, Any]:
    """Переименовать тип."""
    return structure.rename_type(call.tx, call.params["id"], call.body.name)


def archive_type(call: Call) -> None:
    """Отправить тип в архив; у него не должно быть действующих реестров."""
    structure.archive_type(call.tx, call.params["id"])


def add_field(call: Call) -> dict[str, Any]:
    """Добавить поле в тип."""
    body = call.body
    return structure.add_field(
        call.tx,
        call.params["id"],
        body.name,
        body.kind,
        key=body.key,
        multiple=body.multiple,
        required=body.required,
        searchable=body.searchable,
        filterable=body.filterable,
        sortable=body.sortable,
        dictionary_id=body.dictionary_id,
        input_mode=body.input_mode,
        kks_role=body.kks_role,
        unique=body.unique,
        target_type_id=body.target_type_id,
        contact_for=body.contact_for,
    )


def update_field(call: Call) -> dict[str, Any]:
    """Изменить название, порядок, флаги или режим ввода поля."""
    return structure.update_field(
        call.tx, call.params["id"], **call.body.model_dump(exclude_unset=True)
    )


def archive_field(call: Call) -> dict[str, Any]:
    """Отправить поле в архив; значения в записях сохраняются."""
    return structure.archive_field(call.tx, call.params["id"], True)


def restore_field(call: Call) -> dict[str, Any]:
    """Вернуть поле из архива."""
    return structure.archive_field(call.tx, call.params["id"], False)


def list_datasets(call: Call) -> dict[str, Any]:
    """Реестры, которые роль может видеть, — по площадке или типу.

    Администратор и роль со структурой видят все реестры: им нужно выдавать
    права и вести структуру реестров, записи которых сами они не читают.
    """
    found = structure.list_datasets(
        call.conn, site_id=call.query.get("site_id"), type_id=call.query.get("type_id")
    )
    if _manages(call):
        return {"items": found}
    return {"items": [item for item in found if call.access.can(item["id"], "view")]}


def _manages(call: Call) -> bool:
    """Описание структуры целиком видно тем, кто её ведёт или раздаёт права."""
    return call.access.has("structure") or call.access.has("admin")


def create_dataset(call: Call) -> dict[str, Any]:
    """Создать реестр."""
    body = call.body
    return structure.create_dataset(call.tx, body.site_id, body.type_id, body.name)


def get_dataset(call: Call) -> dict[str, Any]:
    """Реестр."""
    return structure.get_dataset(call.conn, call.params["id"])


def rename_dataset(call: Call) -> dict[str, Any]:
    """Переименовать реестр."""
    return structure.rename_dataset(call.tx, call.params["id"], call.body.name)


def archive_dataset(call: Call) -> dict[str, Any]:
    """Отправить реестр в архив; запись в него закрывается."""
    return structure.archive_dataset(call.tx, call.params["id"], True)


def restore_dataset(call: Call) -> dict[str, Any]:
    """Вернуть реестр из архива."""
    return structure.archive_dataset(call.tx, call.params["id"], False)


def list_dictionaries(call: Call) -> dict[str, Any]:
    """Список справочников."""
    return {"items": structure.list_dictionaries(call.conn)}


def create_dictionary(call: Call) -> dict[str, Any]:
    """Создать справочник."""
    return structure.create_dictionary(call.tx, call.body.name)


def rename_dictionary(call: Call) -> dict[str, Any]:
    """Переименовать справочник."""
    return structure.rename_dictionary(call.tx, call.params["id"], call.body.name)


def list_values(call: Call) -> dict[str, Any]:
    """Значения справочника; архивные — по параметру archived."""
    return {
        "items": structure.list_dictionary_values(
            call.conn, call.params["id"], include_archived=call.query_bool("archived")
        )
    }


def add_value(call: Call) -> dict[str, Any]:
    """Добавить значение в справочник."""
    return structure.add_dictionary_value(
        call.tx, call.params["id"], call.body.value, call.body.synonyms
    )


def update_value(call: Call) -> dict[str, Any]:
    """Переименовать значение, изменить синонимы, архив."""
    body = call.body
    return structure.update_dictionary_value(
        call.tx, call.params["id"], value=body.value, synonyms=body.synonyms, archived=body.archived
    )


def _e(method: str, path: str, handler, permission: str, **extra: Any) -> Endpoint:  # type: ignore[no-untyped-def]
    return Endpoint(method, path, handler, permission, tag="Структура", **extra)


ENDPOINTS = [
    _e("GET", "/sites", list_sites, READ),
    _e("POST", "/sites", create_site, WRITE, body=schemas.NameBody, status=201),
    _e("PATCH", "/sites/{id}", rename_site, WRITE, body=schemas.NameBody),
    _e("GET", "/types", list_types, READ),
    _e("POST", "/types", create_type, WRITE, body=schemas.TypeCreate, status=201),
    _e("GET", "/types/{id}", get_type, READ),
    _e("PATCH", "/types/{id}", rename_type, WRITE, body=schemas.NameBody),
    _e("POST", "/types/{id}/archive", archive_type, WRITE, status=204),
    _e("POST", "/types/{id}/fields", add_field, WRITE, body=schemas.FieldCreate, status=201),
    _e("PATCH", "/fields/{id}", update_field, WRITE, body=schemas.FieldPatch),
    _e("POST", "/fields/{id}/archive", archive_field, WRITE),
    _e("POST", "/fields/{id}/restore", restore_field, WRITE),
    _e("GET", "/datasets", list_datasets, READ, query=("site_id", "type_id")),
    _e("POST", "/datasets", create_dataset, WRITE, body=schemas.DatasetCreate, status=201),
    _e("GET", "/datasets/{id}", get_dataset, "dataset.view", target="dataset"),
    _e("PATCH", "/datasets/{id}", rename_dataset, WRITE, body=schemas.NameBody),
    _e("POST", "/datasets/{id}/archive", archive_dataset, WRITE),
    _e("POST", "/datasets/{id}/restore", restore_dataset, WRITE),
    _e("GET", "/dictionaries", list_dictionaries, READ),
    _e("POST", "/dictionaries", create_dictionary, WRITE, body=schemas.NameBody, status=201),
    _e("PATCH", "/dictionaries/{id}", rename_dictionary, WRITE, body=schemas.NameBody),
    _e("GET", "/dictionaries/{id}/values", list_values, READ, query=("archived",)),
    _e("POST", "/dictionaries/{id}/values", add_value, WRITE, body=schemas.ValueCreate, status=201),
    _e("PATCH", "/dictionary-values/{id}", update_value, WRITE, body=schemas.ValuePatch),
]
