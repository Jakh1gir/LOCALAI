"""Магнит ККС при переносе базы Local_DB (MIG-16, IMP-13…16).

Режим Local_DB, где строка — сигнал, а ККС повторяется, переносится как
есть: каждая запись сохраняет UUID, номера версий и историю. Магнит
добавляет к ней то, чего в Local_DB не было:

- **Аппарат** — объект ККС, к которому притянута строка: из столбца ККС или,
  если он пуст, из кода сигнала по правилу станции (DATA-16);
- **Кабель** — ссылка на запись реестра «<режим> — кабели» (ключ — номер);
- **места** (шкафы, коробки) как объекты ККС и **подключение** к ним (клеммы).

Значения магнита вычисляются для каждой версии из её собственных данных,
поэтому история остаётся непрерывной и повторный перенос дописывает новые
версии без разрывов. Поля магнита не сверяются с Local_DB: в источнике их
нет, они вычислены (сверка исходных полей — без изменений).

Пробный прогон всегда показывает предложенную раскладку. Применяется она
только по решению администратора (``Decisions.magnet``); раскладку можно
поправить файлом решений (``Decisions.layouts``).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from engine import EngineError, kks_rules, magnet, structure, text
from engine.database import Tx
from engine.magnet import Layout, Row
from server.localdb_import.convert import FieldPlan, Mapper
from server.localdb_import.report import Issues
from server.localdb_import.source import SourceRecord
from server.magnet_store import Counts, Store, cable_specs

MAX_EXAMPLES = 10
DEVICE, CABLE = "Аппарат", "Кабель"


@dataclass
class _Mode:
    layout: Layout
    words: set[str]
    keys: dict[str, str]  # роль поля магнита → ключ поля
    cables: dict[str, str]  # номер кабеля (верхний регистр) → ID записи кабеля
    laid: int = 0
    stale: list[str] = field(default_factory=list)


class MagnetPass:
    def __init__(
        self,
        tx: Tx,
        site: dict[str, Any],
        mapper: Mapper,
        issues: Issues,
        *,
        apply: bool,
        layouts: dict[str, dict[str, Any]],
        previous: dict[str, dict[str, Any]],
    ) -> None:
        """``previous`` — раскладки, применённые прошлым переносом этого источника.

        Применённая раскладка применяется и при повторе, даже без ``apply``:
        иначе новые версии записей разошлись бы с уже сохранённой историей.
        """
        self.tx, self.site, self.mapper, self.issues = tx, site, mapper, issues
        self.apply, self.layouts, self.previous = apply, layouts, previous
        self.split = kks_rules.splitter(tx.conn)
        self.modes: dict[str, _Mode] = {}
        self.entries: dict[str, dict[str, Any]] = {}
        self.ignored: set[str] = set()  # поля магнита: в сверке не участвуют
        self.store = Store(tx, site, self._error)

    # Подготовка: раскладка режима и реестр кабелей ----------------------------------

    def prepare(
        self,
        type_id: str,
        plans: dict[int, FieldPlan],
        rows: dict[str, list[tuple[str, Row]]],
        names: dict[str, str],
    ) -> None:
        """``rows`` — текущие значения записей по реестрам: (UUID записи, строка)."""
        headers = [plan.source.name for plan in plans.values()]
        protected = frozenset(plan.key for plan in plans.values())
        for dataset_id, items in rows.items():
            name = names[dataset_id]
            layout = self._layout(name, headers, [row for _, row in items])
            if layout is None:
                continue
            numbered = [(index, row) for index, (_, row) in enumerate(items, start=1)]
            plan = magnet.plan_signals(numbered, layout, self.split)
            entry = {
                "dataset": name,
                "applied": False,
                "layout": layout.to_dict(),
                "plan": _plain(plan.summary()),
                "issues": _issues(plan.issues, [uuid for uuid, _ in items]),
            }
            self.entries[dataset_id] = entry
            if not self.apply and name not in self.previous:
                self.issues.add(
                    "info",
                    "magnet_proposed",
                    "Предложена раскладка магнитом ККС (раздел «Магнит ККС»);"
                    " после проверки примените её ключом --magnet",
                    name,
                )
                continue
            try:
                with self.tx.savepoint():
                    mode, counts = self._build(type_id, protected, name, layout, plan)
            except EngineError as error:
                self.issues.add(
                    "warning",
                    error.code,
                    f"Раскладка магнитом не применена: {error.message}",
                    name,
                )
                continue
            mode.words = magnet.spare_words(numbered, layout, self.split)
            self.modes[dataset_id] = mode
            self.ignored.update(mode.keys.values())
            entry["applied"] = True
            entry["cables"] = counts.to_dict()

    def _layout(self, name: str, headers: list[str], rows: list[Row]) -> Layout | None:
        given = self.layouts.get(name) or self.previous.get(name)
        if given is not None:
            try:
                layout = magnet.from_dict(given)
            except (TypeError, ValueError) as error:
                raise EngineError(
                    "magnet_layout_invalid", f"Раскладка реестра «{name}»: {error}"
                ) from None
        else:
            layout = magnet.suggest_layout(headers, rows, self.split)
        if not layout.signal or not any(_text(row.get(layout.signal)) for row in rows):
            return None
        return layout

    def _build(
        self,
        type_id: str,
        protected: frozenset[str],
        name: str,
        layout: Layout,
        plan: magnet.SignalPlan,
    ) -> tuple[_Mode, Counts]:
        cable_type, cable_keys = self.store.type_with(CABLE, cable_specs(layout.places))
        dataset = self.store.dataset(f"{name} — кабели", cable_type["id"])
        schema = structure.load_schema(self.tx.conn, cable_type["id"])
        counts, cables = Counts(), {}
        for number, cable in plan.cables.items():
            for code in cable["places"].values():
                self.mapper.kks_id(code, current=True)  # новые шкафы — в счёт каталога
            values = {cable_keys["Номер кабеля"]: cable["number"]}
            values.update({cable_keys[place]: code for place, code in cable["places"].items()})
            made = self.store.upsert(dataset, schema, cable_keys["Номер кабеля"], values, counts, 0)
            if made:
                cables[number] = made
        roles = {"device": DEVICE, "cable": CABLE}
        roles.update({f"place:{place}": f"{place} · объект" for place in layout.places})
        roles.update({f"contact:{place}": f"Подключение · {place}" for place in layout.contacts})
        shown = self._free_names(type_id, protected, roles)
        specs: list[dict[str, Any]] = [
            {"name": shown["device"], "kind": "kks", "kks_role": "object"},
            {"name": shown["cable"], "kind": "record", "target_type_id": cable_type["id"]},
        ]
        specs += [
            {"name": shown[f"place:{place}"], "kind": "kks", "kks_role": role}
            for place, role in layout.places.items()
        ]
        specs += [
            {
                "name": shown[f"contact:{place}"],
                "kind": "text",
                "contact_for": shown[f"place:{place}"],
            }
            for place in layout.contacts
            if f"place:{place}" in shown
        ]
        by_name = self.store.fields_on(type_id, specs, protected=protected)
        keys = {role: by_name[label] for role, label in shown.items() if label in by_name}
        return _Mode(layout, set(), keys, cables), counts

    def _free_names(
        self, type_id: str, protected: frozenset[str], roles: dict[str, str]
    ) -> dict[str, str]:
        """Название поля магнита; занятое данными Local_DB получает пометку «(магнит)»."""
        taken = {
            text.norm_name(item.name)
            for item in structure.load_schema(self.tx.conn, type_id).active()
            if item.key in protected
        }
        return {
            role: f"{label} (магнит)" if text.norm_name(label) in taken else label
            for role, label in roles.items()
        }

    # Значения магнита для версии записи ----------------------------------------

    def values(self, dataset_id: str, row: Row, *, current: bool) -> dict[str, Any]:
        mode = self.modes.get(dataset_id)
        if mode is None:
            return {}
        item = magnet.attach(row, mode.layout, self.split, mode.words)
        if item is None:
            return {}
        found: dict[str, Any] = {}
        if item.device and (device := self.mapper.kks_id(item.device, current=current)):
            found[mode.keys["device"]] = device
        if item.cable and (cable := mode.cables.get(item.cable.upper())):
            found[mode.keys["cable"]] = cable
        for place in mode.layout.places:
            code = _text(row.get(place))
            key = mode.keys.get(f"place:{place}")
            if code and key and (spot := self.mapper.kks_id(code, current=current)):
                found[key] = spot
        for place, columns in mode.layout.contacts.items():
            shown = magnet.contact_text(row, columns)
            key = mode.keys.get(f"contact:{place}")
            if shown and key:
                found[key] = shown
        if current and found:
            mode.laid += 1
        return found

    def since(self, dataset_id: str, record_id: str) -> int:
        """С какой версии записи вычислять поля магнита при дописывании истории.

        Запись, перенесённая раньше без магнита, получает его только в новых
        версиях: сохранённая история не переписывается.
        """
        mode = self.modes.get(dataset_id)
        if mode is None:
            return 0
        row = self.tx.conn.execute(
            "SELECT doc, version FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        laid = any(key in json.loads(row["doc"]) for key in mode.keys.values())
        return 0 if laid else row["version"] + 1

    def note_unchanged(self, dataset_id: str, record_id: str, row: Row) -> None:
        """Запись перенесена раньше и с тех пор не менялась: есть ли у неё поля магнита."""
        mode = self.modes.get(dataset_id)
        if mode is None or magnet.attach(row, mode.layout, self.split, mode.words) is None:
            return
        stored = self.tx.conn.execute("SELECT doc FROM records WHERE id = ?", (record_id,))
        doc = json.loads(stored.fetchone()[0])
        if not any(key in doc for key in mode.keys.values()):
            mode.stale.append(record_id)

    def summary(self) -> list[dict[str, Any]]:
        for dataset_id, mode in self.modes.items():
            entry = self.entries[dataset_id]
            entry["records_laid_out"] = mode.laid
            entry["records_without_magnet"] = len(mode.stale)
            for record_id in mode.stale[:MAX_EXAMPLES]:
                self.issues.add(
                    "info",
                    "magnet_not_applied",
                    "Записи перенесены раньше без магнита и в Local_DB не менялись; раскладка"
                    " ляжет на них при переносе в новую базу LOCALAI",
                    record_id,
                )
        return list(self.entries.values())

    def _error(self, row: int, error: EngineError) -> None:
        self.issues.add(
            "error", "magnet_write", "Не записан кабель из раскладки магнита", error.message
        )


# Строки для магнита: значения записи по названиям полей --------------------------


def current_row(record: SourceRecord, plans: dict[int, FieldPlan], texts: dict[int, str]) -> Row:
    """Текущие значения записи. Из многозначного поля берётся первое значение."""
    row: Row = {}
    for field_id, _ordinal, raw, value_id in record.values:
        plan = plans.get(field_id)
        if plan is None:
            continue
        shown = texts.get(value_id) if value_id is not None else raw
        if _text(shown) is not None:
            row.setdefault(plan.source.name, shown)
    return row


def history_row(values: dict[str, Any], by_code: dict[str, FieldPlan]) -> Row:
    row: Row = {}
    for code, raw in values.items():
        plan = by_code.get(code)
        first = (raw[0] if raw else None) if isinstance(raw, list) else raw
        if plan is not None and _text(first) is not None:
            row[plan.source.name] = first
    return row


def _issues(found: list[magnet.Issue], uuids: list[str]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for issue in found:
        item = grouped.setdefault(issue.code, {"count": 0, "examples": []})
        item["count"] += 1
        if len(item["examples"]) < MAX_EXAMPLES:
            item["examples"].append({"record": uuids[issue.row - 1], "message": issue.message})
    return grouped


def _plain(value: Any) -> Any:
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    shown = str(value).strip()
    return shown or None
