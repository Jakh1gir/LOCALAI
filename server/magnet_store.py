"""Запись раскладки магнита ККС: типы, поля, реестры и записи по уникальному ключу.

Общая часть переноса выгрузки Excel (MIG-17) и переноса базы Local_DB с
магнитом (MIG-16). Повтор ничего не создаёт заново: запись находится по
уникальному ключу, меняются только отличающиеся значения, пустое значение
не стирает имеющееся (IMP-06, IMP-07).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from engine import EngineError, Tx, kks, links, records, structure, text

OnError = Callable[[int, EngineError], None]  # строка источника, ошибка


@dataclass
class Counts:
    created: int = 0
    updated: int = 0
    unchanged: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class Store:
    def __init__(self, tx: Tx, site: dict[str, Any], on_error: OnError) -> None:
        self.tx, self.site, self.on_error = tx, site, on_error

    def type_with(
        self, name: str, specs: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Тип по названию и ключи его полей по названиям; недостающие поля создаются."""
        found = next(
            (
                item
                for item in structure.list_types(self.tx.conn)
                if text.norm_name(item["name"]) == text.norm_name(name) and not item["archived_at"]
            ),
            None,
        )
        kind = found or structure.create_type(self.tx, name)
        return kind, self.fields_on(kind["id"], specs)

    def fields_on(
        self, type_id: str, specs: list[dict[str, Any]], *, protected: frozenset[str] = frozenset()
    ) -> dict[str, str]:
        """Ключи полей по названиям. Поле с ключом из ``protected`` не берётся:
        это данные источника, их нельзя перезаписывать вычисленными значениями."""
        schema = structure.load_schema(self.tx.conn, type_id)
        keys: dict[str, str] = {}
        for spec in specs:
            name = spec["name"]
            existing = next(
                (f for f in schema.active() if text.norm_name(f.name) == text.norm_name(name)),
                None,
            )
            if existing is None:
                options = {key: value for key, value in spec.items() if key not in ("name", "kind")}
                if "contact_for" in options:
                    options["contact_for"] = keys[options["contact_for"]]
                made = structure.add_field(self.tx, type_id, name, spec["kind"], **options)
                keys[name] = made["key"]
            elif existing.key in protected or existing.kind != spec["kind"]:
                raise EngineError(
                    "magnet_field_taken",
                    f"Поле «{name}» уже есть в типе и занято другими данными ({existing.kind})",
                    details={"field": name},
                )
            else:
                keys[name] = existing.key
        return keys

    def dataset(self, name: str, type_id: str) -> dict[str, Any]:
        for item in structure.list_datasets(self.tx.conn, site_id=self.site["id"]):
            if text.norm_name(item["name"]) == text.norm_name(name):
                if item["type_id"] != type_id:
                    raise EngineError("magnet_dataset_type", f"Реестр «{name}» другого типа")
                return item
        return structure.create_dataset(self.tx, self.site["id"], type_id, name)

    def upsert(
        self,
        dataset: dict[str, Any],
        schema: structure.TypeSchema,
        key: str,
        values: dict[str, Any],
        counts: Counts,
        row: int,
    ) -> str | None:
        """Создать запись или дополнить найденную по уникальному ключу."""
        values = {name: value for name, value in values.items() if value not in (None, "")}
        try:
            with self.tx.savepoint():
                found = self._find(dataset, schema.by_key(key), values.get(key))
                if found is None:
                    made = records.create(self.tx, dataset["id"], values)
                    counts.created += 1
                    return made["id"]
                changed = records.update(self.tx, found["id"], values, found["version"])
                if changed["version"] == found["version"]:
                    counts.unchanged += 1
                else:
                    counts.updated += 1
                return found["id"]
        except EngineError as error:
            self.on_error(row, error)
            return None

    def _find(
        self, dataset: dict[str, Any], key: structure.Field | None, value: Any
    ) -> dict[str, Any] | None:
        if key is None or value in (None, ""):
            return None
        if key.kind == "kks":
            found = kks.resolve(self.tx.conn, self.site["id"], str(value))
            if found is None:
                return None
            norm = found["id"]
        else:
            norm = links.unique_value(key, str(value))
        row = self.tx.conn.execute(
            "SELECT r.id, r.version FROM record_unique u JOIN records r ON r.seq = u.record_seq"
            " WHERE u.dataset_seq = ? AND u.field_key = ? AND u.value = ?",
            (structure.load_dataset(self.tx.conn, dataset["id"])["seq"], key.key, norm),
        ).fetchone()
        return dict(row) if row else None


def cable_specs(places: dict[str, str]) -> list[dict[str, Any]]:
    """Поля типа «Кабель»: номер — ключ, места — объекты ККС с ролью связи."""
    return [
        {"name": "Номер кабеля", "kind": "text", "unique": True},
        *({"name": place, "kind": "kks", "kks_role": role} for place, role in places.items()),
    ]
