"""Устойчивые имена вместо случайных UUID и служебных отметок времени."""

from __future__ import annotations

import re
from typing import Any

from .format import QuestionError

UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
VOLATILE = {"created_at", "updated_at", "database_id", "next_cursor"}


class Canonical:
    def __init__(self, identities: dict[str, str], unique: dict[str, list[str]]) -> None:
        self.identities = identities.copy()
        self.unique = unique

    def learn(self, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                self.learn(item)
        if not isinstance(value, dict):
            return
        if "id" in value and "code" in value and "site_id" in value:
            site = self.identities.get(value["site_id"], value["site_id"])
            self.identities[value["id"]] = f"kks:{site}/{value['code']}"
        refs = value.get("refs", {})
        for kind, items in refs.items():
            if isinstance(items, dict):
                for identity, label in items.items():
                    self.identities.setdefault(identity, f"{kind}:{label}")
        if "doc" in value and "id" in value:
            keys = self.unique.get(value.get("dataset_id"), [])
            key = next(
                (
                    key
                    for key in [*keys, "serial", "code", "number"]
                    if value["doc"].get(key) not in (None, "")
                ),
                None,
            )
            if key is None:
                raise QuestionError(
                    "У записи нет устойчивого ключа; нужен уникальный номер или serial"
                )
            dataset = self.identities[value["dataset_id"]]
            self.identities[value["id"]] = f"record:{dataset}/{value['doc'][key]}"
        if value.get("kind") in ("kks", "record") and "label" in value:
            identity = value.get("kks_id") or value.get("record_id")
            scope = self.identities.get(value.get("dataset_id", ""), "")
            self.identities.setdefault(identity, f"{value['kind']}:{scope}/{value['label']}")
        for child in value.values():
            self.learn(child)

    def normalize(self, value: Any, *, business: bool = False) -> Any:
        if isinstance(value, dict):
            return {
                self.normalize(key): (None if item is None else True)
                if key == "archived_at" and not business
                else self.normalize(item, business=business or key == "doc")
                for key, item in value.items()
                if business or key not in VOLATILE
            }
        if isinstance(value, list):
            return [self.normalize(item, business=business) for item in value]
        if isinstance(value, str):

            def replace(match: re.Match[str]) -> str:
                identity = self.identities.get(match[0])
                if identity is None:
                    raise QuestionError("Ответ содержит UUID без устойчивого имени")
                return identity

            return UUID.sub(replace, value)
        return value
