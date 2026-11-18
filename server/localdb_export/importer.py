"""Раскладка выгрузки Local_DB по реестрам LOCALAI с магнитом ККС (IMP-13…17, MIG-16).

Лист делится по столбцу «Режим БД». Режим без кодов сигналов — реестр
«строка — аппарат» с уникальным ККС. Режим с кодами сигналов раскладывается
магнитом: реестр сигналов (ключ — канал ввода-вывода или код сигнала) и
реестр кабелей (ключ — номер кабеля), сигнал ссылается на кабель.

Повторный перенос той же выгрузки ничего не создаёт заново: запись находится
по уникальному ключу, меняются только отличающиеся значения, пустая ячейка
значение не стирает (IMP-06, IMP-07). Всё идёт одной транзакцией; пробный
прогон выполняет её целиком и откатывает, поэтому его счётчики точны.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from engine import Actor, EngineError, Tx, kks, kks_rules, links, magnet, records, structure, text
from engine.magnet import Layout
from server.xlsx import Sheet

CONSOLE = Actor(None, "Перенос выгрузки Local_DB", "system")
SERVICE = ("№",)
MODE_HEADERS = ("режим бд", "режим", "mode")
KIND_SIGNAL, KIND_SPARE = "Сигнал", "Резерв"


@dataclass
class _Counts:
    created: int = 0
    updated: int = 0
    unchanged: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class Result:
    report: dict[str, Any] = field(default_factory=dict)
    failed: bool = False


class _Rollback(Exception):
    """Пробный прогон или отказ: транзакция откатывается, отчёт остаётся."""


def run(
    db: Any, sheet: Sheet, *, apply: bool, site_name: str | None = None, actor: Actor = CONSOLE
) -> dict[str, Any]:
    """Пробный прогон (``apply=False``) или перенос. Возвращает отчёт."""
    result = Result()
    try:
        with db.write(actor) as tx:
            result.report = _load(tx, sheet, site_name)
            result.failed = bool(result.report["write_errors"])
            if result.failed or not apply:
                raise _Rollback
    except _Rollback:
        pass
    result.report["mode"] = (
        "failed" if result.failed and apply else "applied" if apply else "dry_run"
    )
    return result.report


def _load(tx: Tx, sheet: Sheet, site_name: str | None) -> dict[str, Any]:
    site = _site(tx, site_name)
    mode_column = next(
        (name for name in sheet.headers if " ".join(name.casefold().split()) in MODE_HEADERS), None
    )
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for number, row in sheet.rows:
        mode = str(row.get(mode_column) or "").strip() if mode_column else ""
        groups.setdefault(mode or sheet.name, []).append((number, row))
    service = [name for name in sheet.headers if name in SERVICE or name == mode_column]
    split = _splitter(tx)
    report: dict[str, Any] = {
        "sheet": sheet.name,
        "site": site["name"],
        "rows": len(sheet.rows),
        "modes": {},
        "write_errors": [],
    }
    for mode, rows in groups.items():
        layout = magnet.suggest_layout(sheet.headers, [row for _, row in rows], split)
        layout.service = service
        signals = layout.signal and any(row.get(layout.signal) not in (None, "") for _, row in rows)
        loader = _Signals if signals else _Devices
        report["modes"][mode] = loader(tx, site, mode, sheet.headers, layout, report).load(rows)
    return report


class _Writer:
    def __init__(
        self,
        tx: Tx,
        site: dict[str, Any],
        mode: str,
        headers: list[str],
        layout: Layout,
        report: dict[str, Any],
    ) -> None:
        self.tx, self.site, self.mode, self.headers, self.layout = tx, site, mode, headers, layout
        self.report = report

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
        schema = structure.load_schema(self.tx.conn, kind["id"])
        keys: dict[str, str] = {}
        for spec in specs:
            existing = next(
                (
                    f
                    for f in schema.active()
                    if text.norm_name(f.name) == text.norm_name(spec["name"])
                ),
                None,
            )
            if existing is None:
                options = {key: value for key, value in spec.items() if key not in ("name", "kind")}
                if "contact_for" in options:
                    options["contact_for"] = keys[options["contact_for"]]
                made = structure.add_field(
                    self.tx, kind["id"], spec["name"], spec["kind"], **options
                )
                keys[spec["name"]] = made["key"]
            elif existing.kind != spec["kind"]:
                raise EngineError(
                    "export_field_kind",
                    f"В типе «{name}» поле «{spec['name']}» уже есть, но другого вида ({existing.kind})",
                )
            else:
                keys[spec["name"]] = existing.key
        return kind, keys

    def dataset(self, name: str, type_id: str) -> dict[str, Any]:
        for item in structure.list_datasets(self.tx.conn, site_id=self.site["id"]):
            if text.norm_name(item["name"]) == text.norm_name(name):
                if item["type_id"] != type_id:
                    raise EngineError("export_dataset_type", f"Реестр «{name}» другого типа")
                return item
        return structure.create_dataset(self.tx, self.site["id"], type_id, name)

    def upsert(
        self,
        dataset: dict[str, Any],
        schema: structure.TypeSchema,
        key: str,
        values: dict[str, Any],
        counts: _Counts,
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
            self.report["write_errors"].append(
                {"mode": self.mode, "row": row, "code": error.code, "message": error.message}
            )
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

    def others(self, used: set[str]) -> list[str]:
        """Остальные столбцы — текстовые поля: перенос без потерь."""
        return [
            name for name in self.headers if name not in used and name not in self.layout.service
        ]


class _Devices(_Writer):
    """Режим «строка — аппарат»: ККС уникален в реестре."""

    def load(self, rows: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
        kks_column = self.layout.kks
        if kks_column is None:
            raise EngineError("export_no_kks", f"В режиме «{self.mode}» не найден столбец ККС")
        plan = magnet.plan_devices(rows, kks_column, self.layout.service)
        filled = {
            name
            for _, row in plan.rows.values()
            for name, value in row.items()
            if value not in (None, "")
        }
        columns = [name for name in self.others({kks_column}) if name in filled]
        specs = [{"name": kks_column, "kind": "kks", "kks_role": "object", "unique": True}]
        specs += [{"name": name, "kind": "text"} for name in columns]
        kind, keys = self.type_with(self.mode, specs)
        dataset = self.dataset(self.mode, kind["id"])
        schema = structure.load_schema(self.tx.conn, kind["id"])
        counts = _Counts()
        for number, row in plan.rows.values():
            values = {keys[name]: _shown(row.get(name)) for name in [kks_column, *columns]}
            self.upsert(dataset, schema, keys[kks_column], values, counts, number)
        return {
            "kind": "devices",
            "dataset": dataset["name"],
            "plan": _plain(plan.summary()),
            "records": counts.to_dict(),
            "issues": [item.to_dict() for item in plan.issues],
        }


class _Signals(_Writer):
    """Режим «строка на сигнал»: сигналы и кабели, притянутые к ККС аппаратов."""

    def load(self, rows: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
        lay = self.layout
        plan = magnet.plan_signals(rows, lay, _splitter(self.tx))
        cable_type, cable_keys = self.type_with(
            "Кабель",
            [
                {"name": "Номер кабеля", "kind": "text", "unique": True},
                *(
                    {"name": place, "kind": "kks", "kks_role": role}
                    for place, role in lay.places.items()
                ),
            ],
        )
        cables = self.dataset(f"{self.mode} — кабели", cable_type["id"])
        cable_schema = structure.load_schema(self.tx.conn, cable_type["id"])
        cable_counts = _Counts()
        for cable in plan.cables.values():
            values = {cable_keys["Номер кабеля"]: cable["number"]}
            values.update({cable_keys[place]: code for place, code in cable["places"].items()})
            self.upsert(cables, cable_schema, cable_keys["Номер кабеля"], values, cable_counts, 0)

        used = {name for name in (lay.signal, lay.kks, lay.cable) if name} | set(lay.places)
        filled = {name for item in plan.signals for name in item.values}
        specs: list[dict[str, Any]] = [
            {"name": "Код сигнала", "kind": "text", "unique": True},
            {"name": "Аппарат", "kind": "kks", "kks_role": "object"},
            {"name": "Вид", "kind": "text"},
            {"name": "Кабель", "kind": "record", "target_type_id": cable_type["id"]},
        ]
        specs += [
            {"name": place, "kind": "kks", "kks_role": role} for place, role in lay.places.items()
        ]
        specs += [
            {"name": f"Подключение · {place}", "kind": "text", "contact_for": place}
            for place in lay.contacts
        ]
        if lay.channel:
            specs.append({"name": "Канал ввода-вывода", "kind": "text", "unique": True})
        others = [name for name in self.others(used) if name in filled]
        specs += [{"name": name, "kind": "text"} for name in others]
        signal_type, keys = self.type_with("Сигнал", specs)
        signals = self.dataset(f"{self.mode} — сигналы", signal_type["id"])
        schema = structure.load_schema(self.tx.conn, signal_type["id"])
        key = keys["Канал ввода-вывода"] if lay.channel else keys["Код сигнала"]
        counts = _Counts()
        skipped_spares = 0
        for item in plan.signals:
            if item.kind == "spare" and not item.channel:
                skipped_spares += 1  # без канала резерв не опознать при повторе
                continue
            row = item.values
            values = {
                keys["Код сигнала"]: item.code,
                keys["Аппарат"]: item.device,
                keys["Вид"]: KIND_SPARE if item.kind == "spare" else KIND_SIGNAL,
                keys["Кабель"]: item.cable,
            }
            values.update({keys[place]: _shown(row.get(place)) for place in lay.places})
            values.update(
                {
                    keys[f"Подключение · {place}"]: magnet.contact_text(row, columns)
                    for place, columns in lay.contacts.items()
                }
            )
            if lay.channel:
                values[keys["Канал ввода-вывода"]] = item.channel
            values.update({keys[name]: _shown(row.get(name)) for name in others})
            self.upsert(signals, schema, key, values, counts, item.row)
        return {
            "kind": "signals",
            "layout": lay.to_dict(),
            "datasets": {"signals": signals["name"], "cables": cables["name"]},
            "plan": _plain(plan.summary()),
            "records": {"signals": counts.to_dict(), "cables": cable_counts.to_dict()},
            "spares_without_channel": skipped_spares,
            "issues": [item.to_dict() for item in plan.issues],
        }


def _site(tx: Tx, name: str | None) -> dict[str, Any]:
    sites = [item for item in structure.list_sites(tx.conn) if not item["archived_at"]]
    if name:
        found = next(
            (item for item in sites if text.norm_name(item["name"]) == text.norm_name(name)), None
        )
        return found or structure.create_site(tx, name)
    if len(sites) == 1:
        return sites[0]
    if not sites:
        return structure.create_site(tx, "Площадка 1")
    raise EngineError("export_site", "В базе несколько площадок: укажите --site")


def _splitter(tx: Tx):  # type: ignore[no-untyped-def]
    return kks_rules.splitter(tx.conn)


def _shown(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip() or None


def _plain(value: Any) -> Any:
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value
