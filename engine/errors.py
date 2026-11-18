"""Единая ошибка движка: код, понятный текст на русском, поле (API-08)."""

from __future__ import annotations

from typing import Any

NOT_FOUND = "not_found"
INVALID = "invalid"
CONFLICT = "conflict"
TOO_LARGE = "too_large"
UNAUTHORIZED = "unauthorized"
FORBIDDEN = "forbidden"
TOO_MANY = "too_many"


class EngineError(Exception):
    """Ошибка, которую сервер отдаёт клиенту без переформулировки.

    ``status`` — класс ошибки (not_found, invalid, conflict); сервер
    переводит его в код HTTP. ``code`` — устойчивый машинный код.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: str = INVALID,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field = field
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            body["field"] = self.field
        if self.details:
            body["details"] = self.details
        return body


def not_found(kind: str, entity_id: str) -> EngineError:
    return EngineError(
        f"{kind}_not_found",
        f"Не найдено: {_KIND_NAMES.get(kind, kind)} {entity_id}",
        status=NOT_FOUND,
    )


_KIND_NAMES = {
    "site": "площадка",
    "type": "тип",
    "field": "поле",
    "dataset": "реестр",
    "dictionary": "справочник",
    "dictionary_value": "значение справочника",
    "record": "запись",
    "kks": "объект ККС",
    "kks_alias": "псевдоним ККС",
    "role": "роль",
    "user": "пользователь",
}
