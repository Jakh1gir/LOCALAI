"""Проверка и приведение значений записи по схеме типа (DATA-02).

На входе — значения в том виде, в каком их присылает клиент или импорт.
На выходе — документ для хранения: пустые значения отсутствуют, списки и
ККС заменены на ID. Правила одни для ручной правки, импорта и ИИ.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from engine import kks, structure, text
from engine.database import Tx
from engine.errors import EngineError

_TRUE = {"да", "true", "1", "yes", "истина", "+"}
_FALSE = {"нет", "false", "0", "no", "ложь", "-"}
MAX_TEXT = 20_000
MAX_ITEMS = 500
# Целые хранятся в SQLite как 64-битные: за этими границами фильтр и
# сортировка по значению невозможны, поэтому такие числа не принимаются.
MIN_INT, MAX_INT = -(2**63), 2**63 - 1
_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")
_SPACES = (" ", "\u00a0")


def coerce_scalar(kind: str, value: Any, field: str) -> Any:
    """Приводит одно значение простого вида. ``None`` означает «пусто»."""
    if value is None:
        return None
    if kind == "text":
        return _text(value, field)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    if kind == "integer":
        return _integer(value, field)
    if kind == "number":
        return _number(value, field)
    if kind == "boolean":
        return _boolean(value, field)
    if kind == "date":
        return _date(value, field)
    raise EngineError("field_kind", f"Вид поля «{kind}» не является простым", field=field)


def _text(value: Any, field: str) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise EngineError("value_text", "Ожидается текст", field=field)
    shown = str(value).strip()
    if len(shown) > MAX_TEXT:
        raise EngineError("value_too_long", f"Текст длиннее {MAX_TEXT} символов", field=field)
    return shown or None


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise EngineError("value_integer", "Ожидается целое число", field=field)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str):
        cleaned = _no_spaces(value)
        if _INTEGER_TEXT.match(cleaned):
            value = int(cleaned)
    if isinstance(value, int):
        return _in_range(value, field)
    raise EngineError("value_integer", "Ожидается целое число", field=field)


def _number(value: Any, field: str) -> int | float:
    """Число без потери точности: целое — точно, дробное — если хватает 15 знаков."""
    if isinstance(value, bool):
        raise EngineError("value_number", "Ожидается число", field=field)
    if isinstance(value, str):
        cleaned = _no_spaces(value).replace(",", ".")
        if _INTEGER_TEXT.match(cleaned):
            return _in_range(int(cleaned), field)
        try:
            exact = Decimal(cleaned)
            value = float(cleaned)
        except (InvalidOperation, ValueError):
            raise EngineError("value_number", "Ожидается число", field=field) from None
        if math.isfinite(value) and Decimal(repr(value)).normalize() != exact.normalize():
            raise EngineError(
                "value_precision",
                "Число не сохраняется точно: больше 15 значащих цифр",
                field=field,
            )
    if isinstance(value, int):
        return _in_range(value, field)
    if isinstance(value, float) and math.isfinite(value):
        return _in_range(int(value), field) if value.is_integer() else value
    raise EngineError("value_number", "Ожидается число", field=field)


def _no_spaces(value: str) -> str:
    for space in _SPACES:
        value = value.replace(space, "")
    return value


def _in_range(value: int, field: str) -> int:
    if not MIN_INT <= value <= MAX_INT:
        raise EngineError(
            "value_out_of_range",
            "Целое число вне допустимого диапазона (до 19 цифр)",
            field=field,
        )
    return value


def _boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in _TRUE:
        return True
    if isinstance(value, str) and value.casefold() in _FALSE:
        return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise EngineError("value_boolean", "Ожидается «да» или «нет»", field=field)


def _date(value: Any, field: str) -> str:
    """Дата ``ГГГГ-ММ-ДД``, ``ДД.ММ.ГГГГ`` или полная отметка времени ISO.

    От отметки времени берётся дата; любая другая строка отклоняется целиком,
    а не обрезается до первых десяти символов.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            if len(value) == 10 and value[2] == "." and value[5] == ".":
                return date.fromisoformat(f"{value[6:10]}-{value[3:5]}-{value[0:2]}").isoformat()
            if len(value) == 10:
                return date.fromisoformat(value).isoformat()
            if len(value) > 10 and value[10] in "T ":
                return datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            pass
    raise EngineError("value_date", "Ожидается дата в виде ГГГГ-ММ-ДД или ДД.ММ.ГГГГ", field=field)


class Normalizer:
    """Приводит значения записи одного реестра; хранит кэш справочников и ККС."""

    def __init__(
        self,
        tx: Tx,
        schema: structure.TypeSchema,
        site_id: str,
        *,
        create_missing_kks: bool = True,
        audit_kks: bool = True,
    ) -> None:
        self.tx = tx
        self.schema = schema
        self.site_id = site_id
        self.create_missing_kks = create_missing_kks
        self.audit_kks = audit_kks
        self.kks_created = 0  # для итога пакетной загрузки
        self._choices: dict[str, dict[str, Any]] = {}
        self._kks: dict[str, str] = {}

    def full(self, values: dict[str, Any]) -> dict[str, Any]:
        """Документ новой записи: все обязательные поля должны быть заполнены."""
        changes = self.partial(values, current={})
        doc = {key: value for key, value in changes.items() if value is not None}
        for field in self.schema.active():
            if field.required and field.key not in doc:
                raise EngineError(
                    "value_required", f"Поле «{field.name}» обязательно", field=field.key
                )
        return doc

    def partial(self, values: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """Изменения записи: ``{ключ: значение}``, где ``None`` — очистить поле."""
        if not isinstance(values, dict):
            raise EngineError("values_invalid", "Значения записи: ожидается объект")
        result: dict[str, Any] = {}
        for key, raw in values.items():
            field = self.schema.by_key(key)
            if field is None:
                raise EngineError("field_unknown", f"В типе нет поля «{key}»", field=key)
            if field.archived_at is not None:
                raise EngineError("field_archived", f"Поле «{field.name}» в архиве", field=key)
            value = self._value(field, raw, current.get(key))
            if value is None and field.required:
                raise EngineError("value_required", f"Поле «{field.name}» обязательно", field=key)
            result[key] = value
        return result

    def _value(self, field: structure.Field, raw: Any, old: Any) -> Any:
        if not field.multiple:
            if isinstance(raw, list):
                raise EngineError(
                    "value_single",
                    f"Поле «{field.name}» хранит одно значение",
                    field=field.key,
                )
            return self._item(field, raw, old)
        items = raw if isinstance(raw, list) else [raw]
        if len(items) > MAX_ITEMS:
            raise EngineError(
                "value_too_many", f"Больше {MAX_ITEMS} значений в поле", field=field.key
            )
        kept = old if isinstance(old, list) else []
        cleaned = [self._item(field, item, kept) for item in items]
        cleaned = [item for item in cleaned if item is not None]
        return cleaned or None

    def _item(self, field: structure.Field, raw: Any, old: Any) -> Any:
        if field.kind == "choice":
            return self._choice(field, raw, old)
        if field.kind == "kks":
            return self._kks_id(field, raw, old)
        return coerce_scalar(field.kind, raw, field.key)

    # Справочники --------------------------------------------------------------

    def _choice(self, field: structure.Field, raw: Any, old: Any) -> str | None:
        if raw is None or raw == "":
            return None
        values = self._dictionary(field.dictionary_id)
        if isinstance(raw, dict) and text.is_uuid(raw.get("id")):
            found = values["by_id"].get(raw["id"])
            if found is None:
                raise EngineError("value_choice", "Значения нет в справочнике", field=field.key)
            if found["archived"] and not _was(old, raw["id"]):
                raise EngineError(
                    "value_choice_archived",
                    f"Значение «{found['value']}» в архиве",
                    field=field.key,
                )
            return raw["id"]
        if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            raise EngineError("value_choice", "Ожидается значение справочника", field=field.key)
        shown = text.clean_name(str(raw), "Значение", structure.MAX_VALUE)
        value_id = values["by_name"].get(text.norm_name(shown))
        if value_id is not None:
            if values["by_id"][value_id]["archived"] and not _was(old, value_id):
                raise EngineError(
                    "value_choice_archived", f"Значение «{shown}» в архиве", field=field.key
                )
            return value_id
        if field.input_mode == "strict":
            raise EngineError(
                "value_choice",
                f"«{shown}» нет в справочнике поля «{field.name}»",
                field=field.key,
            )
        created = structure.add_dictionary_value(self.tx, field.dictionary_id, shown)
        values["by_id"][created["id"]] = {"value": shown, "archived": False}
        values["by_name"][text.norm_name(shown)] = created["id"]
        return created["id"]

    def _dictionary(self, dictionary_id: str | None) -> dict[str, Any]:
        assert dictionary_id is not None
        if dictionary_id not in self._choices:
            self._choices[dictionary_id] = load_dictionary(self.tx.conn, dictionary_id)
        return self._choices[dictionary_id]

    # ККС ----------------------------------------------------------------------

    def _kks_id(self, field: structure.Field, raw: Any, old: Any) -> str | None:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if isinstance(raw, dict) and text.is_uuid(raw.get("id")):
            found = kks.get(self.tx.conn, raw["id"])
            if found["site_id"] != self.site_id:
                raise EngineError(
                    "kks_other_site", "Объект ККС принадлежит другой площадке", field=field.key
                )
            if found["archived_at"] and not _was(old, raw["id"]):
                raise EngineError(
                    "kks_archived",
                    f"Объект ККС «{found['code']}» в архиве; его нужно восстановить",
                    field=field.key,
                )
            return raw["id"]
        if not isinstance(raw, str):
            raise EngineError("kks_invalid", "Ожидается код ККС", field=field.key)
        try:
            _, norm = text.norm_kks(raw)
        except EngineError as error:
            error.field = field.key
            raise
        if norm not in self._kks:
            try:
                self._kks[norm], created = kks.ensure(
                    self.tx,
                    self.site_id,
                    raw,
                    create_missing=self.create_missing_kks,
                    audit=self.audit_kks,
                )
                self.kks_created += int(created)
            except EngineError as error:
                error.field = field.key
                raise
        return self._kks[norm]


def _was(old: Any, value_id: str) -> bool:
    """Значение уже стояло в записи: архивное значение можно оставить, но не выбрать заново."""
    return value_id == old or (isinstance(old, list) and value_id in old)


def load_dictionary(conn: sqlite3.Connection, dictionary_id: str) -> dict[str, Any]:
    """Справочник в памяти: по ID и по названию или синониму."""
    rows = conn.execute(
        "SELECT id, value, value_norm, synonyms, archived_at FROM dictionary_values"
        " WHERE dictionary_id = ?",
        (dictionary_id,),
    ).fetchall()
    by_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, str] = {}
    for row in rows:
        by_id[row["id"]] = {"value": row["value"], "archived": row["archived_at"] is not None}
        by_name[row["value_norm"]] = row["id"]
        for synonym in json.loads(row["synonyms"]):
            by_name.setdefault(text.norm_name(synonym), row["id"])
    return {"by_id": by_id, "by_name": by_name}
