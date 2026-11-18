"""Маршруты каталога ККС."""

from __future__ import annotations

from typing import Any

from engine import EngineError, kks, kks_rules, structure
from engine.errors import NOT_FOUND
from server import schemas
from server.api import Call, Endpoint

# Каталог ККС общий для площадки: искать и открывать карточку может каждый
# вошедший, менять коды и псевдонимы — роль с разделом «Каталог ККС».
READ, WRITE = "session", "section.kks"


def search(call: Call) -> dict[str, Any]:
    """Подсказки по части кода: сначала совпадения с начала кода."""
    structure.get_site(call.conn, call.params["id"])
    found = kks.search(
        call.conn, call.params["id"], call.query.get("q", ""), limit=call.query_int("limit", 20)
    )
    return {"items": found}


def resolve(call: Call) -> dict[str, Any]:
    """Объект по коду, псевдониму или коду сигнала (``via: "signal"``)."""
    code = call.query.get("code", "")
    if not code.strip():
        raise EngineError("request_invalid", "Нужен параметр code", field="code")
    found = kks_rules.resolve(call.conn, call.params["id"], code)
    if found is None:
        raise EngineError(
            "kks_not_found", f"Код ККС «{code.strip()}» не найден на площадке", status=NOT_FOUND
        )
    return found


def create(call: Call) -> dict[str, Any]:
    """Создать объект ККС."""
    return kks.create(call.tx, call.params["id"], call.body.code, call.body.name)


def card(call: Call) -> dict[str, Any]:
    """Карточка ККС: объект, псевдонимы, связанные записи по реестрам и ролям."""
    found = kks.card(call.conn, call.params["id"])
    found["usage"] = [
        item for item in found["usage"] if call.access.can(item["dataset_id"], "view")
    ]
    found["via_links"] = [
        item for item in found["via_links"] if call.access.can(item["dataset_id"], "view")
    ]
    return found


def get_rules(call: Call) -> dict[str, Any]:
    """Правило кода станции: признак сигнала в конце кода и пример разбора."""
    return {**kks_rules.get_rule(call.conn), "example": kks_rules.example(call.conn)}


def set_rules(call: Call) -> dict[str, Any]:
    """Изменить правило признака сигнала (DATA-16)."""
    kks_rules.set_rule(call.tx, call.body.signal_pattern)
    return {**kks_rules.get_rule(call.tx.conn), "example": kks_rules.example(call.tx.conn)}


def update(call: Call) -> dict[str, Any]:
    """Сменить код (прежний станет псевдонимом), описание или архив."""
    body = call.body
    found = kks.get(call.tx.conn, call.params["id"])
    if body.code is not None:
        found = kks.rename(call.tx, call.params["id"], body.code)
    if body.name is not None or body.archived is not None:
        found = kks.update(call.tx, call.params["id"], name=body.name, archived=body.archived)
    return found


def add_alias(call: Call) -> dict[str, Any]:
    """Добавить псевдоним."""
    return kks.add_alias(call.tx, call.params["id"], call.body.alias)


def remove_alias(call: Call) -> None:
    """Удалить псевдоним."""
    kks.remove_alias(call.tx, call.params["id"])


def _e(method: str, path: str, handler, permission: str, **extra: Any) -> Endpoint:  # type: ignore[no-untyped-def]
    return Endpoint(method, path, handler, permission, tag="ККС", **extra)


ENDPOINTS = [
    _e("GET", "/sites/{id}/kks/search", search, READ, query=("q", "limit")),
    _e("GET", "/sites/{id}/kks/resolve", resolve, READ, query=("code",)),
    _e("POST", "/sites/{id}/kks", create, WRITE, body=schemas.KksCreate, status=201),
    _e("GET", "/kks/rules", get_rules, READ),
    _e("PUT", "/kks/rules", set_rules, "section.structure", body=schemas.KksRules),
    _e("GET", "/kks/{id}", card, READ),
    _e("PATCH", "/kks/{id}", update, WRITE, body=schemas.KksPatch),
    _e("POST", "/kks/{id}/aliases", add_alias, WRITE, body=schemas.AliasCreate, status=201),
    _e("DELETE", "/kks-aliases/{id}", remove_alias, WRITE, status=204),
]
