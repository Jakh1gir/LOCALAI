"""Перенос базы Local_DB в LOCALAI (ТЗ, раздел 14).

Весь перенос — одна транзакция записи. Пробный прогон выполняет тот же
путь до конца, включая сверку, и откатывает транзакцию: отчёт пробного
прогона совпадает с тем, что сделает перенос. Перед применением создаётся
защитный снимок базы LOCALAI (MIG-12).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine import Actor, Database, kks, records, snapshot, structure, text
from engine.database import Tx, dumps
from engine.errors import EngineError
from server.localdb_import.convert import (
    KIND_BY_TYPE,
    ORIGIN,
    FieldPlan,
    Mapper,
    build_versions,
    loose_kks,
    moment,
)
from server.localdb_import.reconcile import Reconciler, Stats
from server.localdb_import.report import Issues
from server.localdb_import.source import Source, SourceField, SourceRecord, SourceTable

IMPORTER = Actor(None, "Перенос Local_DB", "localdb_import")
SOURCE_KIND = "localdb"
_NAMESPACE = uuid.UUID("6f1d7c1e-1b52-4f0e-9a51-2c7f4c0d8a10")
_MAX_SIMILAR = 50


@dataclass
class Decisions:
    """Решения владельца на шаге «Решения» (ТЗ 14.1)."""

    site_id: str | None = None
    site_name: str | None = None
    dataset_names: dict[str, str] = field(default_factory=dict)  # UUID режима → название
    kks_fields: tuple[str, ...] = ("kks",)
    mode_field: str = "database_mode"
    include_audit: bool = True


class _Stop(Exception):
    """Откат транзакции без ошибки: пробный прогон или найдены ошибки данных."""


def run(
    db: Database,
    source_path: str | Path,
    decisions: Decisions | None = None,
    *,
    apply: bool = False,
    actor: Actor = IMPORTER,
    snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Пробный прогон (по умолчанию) или перенос. Возвращает отчёт."""
    started, started_at = time.monotonic(), text.now()
    decisions = decisions or Decisions()
    issues = Issues()
    report: dict[str, Any] = {"mode": "dry_run"}
    with Source(source_path) as source:
        report["source"] = {
            "path": str(source.path),
            "schema_version": source.schema_version,
            "database_uuid": source.database_uuid,
            "profile": source.profile,
        }
        source.check_schema()
        report["not_transferred"] = source.other_counts()
        if apply:
            taken = snapshot.take(db.connect(), db.path, "before-localdb-import", snapshot_dir)
            report["snapshot"] = str(taken)
        try:
            with db.write(actor) as tx:
                job = _Job(tx, source, decisions, issues, report)
                job.run()
                if issues.blocking() or not apply:
                    raise _Stop
                job.finish(started_at, source_path)
        except _Stop:
            pass
    if issues.blocking():
        report["mode"] = "refused"
    elif apply:
        report["mode"] = "applied"
    report["issues"] = issues.to_list()
    report["seconds"] = round(time.monotonic() - started, 2)
    return report


class _Job:
    def __init__(
        self, tx: Tx, source: Source, decisions: Decisions, issues: Issues, report: dict[str, Any]
    ) -> None:
        self.tx = tx
        self.source = source
        self.decisions = decisions
        self.issues = issues
        self.report = report
        self.stats: dict[str, Stats] = {}
        self.to_archive: set[str] = set()
        self.audit_cursor = 0
        self.site: dict[str, Any] | None = None
        self.structure = {
            "types": 0,
            "fields": {"created": 0, "archived": 0, "hidden": 0, "by_kind": {}},
            "dictionaries": 0,
            "values": {"created": 0, "archived": 0, "merged": 0},
        }
        self.dictionaries: dict[int, str] = {}  # справочник Local_DB → справочник LOCALAI
        self.value_dictionary: dict[int, int] = {}
        self.final_text: dict[int, str] = {}  # значение Local_DB → текст конечного значения
        for dictionary in source.dictionaries.values():
            by_id = {value.id: value for value in dictionary.values}
            for value in dictionary.values:
                self.value_dictionary[value.id] = dictionary.id
                self.final_text[value.id] = _final(value, by_id).canonical

    # Ход переноса -------------------------------------------------------------

    def run(self) -> None:
        self.site = self._site()
        if self.site is None:
            return
        self.report["site"] = {"id": self.site["id"], "name": self.site["name"]}
        self.mapper = Mapper(self.tx, self.site["id"], self.issues)
        self.mapper.source_texts = {
            value.id: value.canonical
            for dictionary in self.source.dictionaries.values()
            for value in dictionary.values
        }
        self.reconciler = Reconciler(
            self.tx, self.site["id"], self.mapper.source_texts, self.final_text
        )
        for table in self.source.tables:
            self._table(table)
        for dataset_id in sorted(self.to_archive):
            if structure.get_dataset(self.tx.conn, dataset_id)["archived_at"] is None:
                structure.archive_dataset(self.tx, dataset_id)
        self.report["structure"] = self.structure
        self.report["kks"] = self._kks_summary()
        if self.decisions.include_audit:
            self.report["audit"] = {"events": self._audit()}
        self.report["datasets"] = [stats.summary() for stats in self.stats.values()]
        self.report["reconciliation"] = [
            self.reconciler.check(stats, self.issues) for stats in self.stats.values()
        ]

    def finish(self, started_at: str, source_path: str | Path) -> None:
        assert self.site is not None
        run_id = text.new_id()
        summary = {
            key: self.report[key]
            for key in ("source", "site", "structure", "datasets", "kks", "audit", "reconciliation")
            if key in self.report
        }
        self.tx.conn.execute(
            "INSERT INTO import_runs (id, source_kind, source_id, source_path, source_schema,"
            " site_id, actor_id, actor_label, started_at, finished_at, audit_cursor, report)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                SOURCE_KIND,
                self._source_id(),
                str(source_path),
                self.source.schema_version,
                self.site["id"],
                self.tx.actor.id,
                self.tx.actor.label,
                started_at,
                self.tx.now,
                self.audit_cursor,
                dumps(summary),
            ),
        )
        totals = {
            "records_new": sum(stats.records_new for stats in self.stats.values()),
            "records_extended": sum(stats.records_extended for stats in self.stats.values()),
        }
        self.tx.audit("localdb.import", "import", run_id, details=totals)
        self.report["run_id"] = run_id

    # Площадка -----------------------------------------------------------------

    def _site(self) -> dict[str, Any] | None:
        conn, wanted = self.tx.conn, self.decisions
        if wanted.site_id:
            return structure.get_site(conn, wanted.site_id)
        sites = structure.list_sites(conn)
        if wanted.site_name:
            norm = text.norm_name(wanted.site_name)
            for site in sites:
                if text.norm_name(site["name"]) == norm:
                    return site
            return structure.create_site(self.tx, wanted.site_name)
        active = [site for site in sites if site["archived_at"] is None]
        if not active:
            return structure.create_site(self.tx, "Основная площадка")
        if len(active) == 1:
            return active[0]
        self.issues.add(
            "error",
            "site_required",
            "В LOCALAI несколько площадок: укажите, на какую переносить",
            ", ".join(site["name"] for site in active),
        )
        return None

    # Таблица Local_DB → тип и реестры -----------------------------------------------

    def _table(self, table: SourceTable) -> None:
        type_id = _uuid(table.uuid, f"type:{table.uuid}")
        if self._exists("types", type_id) is None:
            name = self._free_name("types", table.name)
            structure.create_type(self.tx, name, "Перенесено из Local_DB", entity_id=type_id)
            self.structure["types"] += 1
        mode = next((f for f in table.fields if f.code == self.decisions.mode_field), None)
        plans = self._fields(table, type_id)
        datasets = self._datasets(table, type_id, mode)
        by_code = {plan.source.code: plan for plan in plans.values()}
        for batch in self.source.records(table.id):
            fresh: dict[str, list[records.LoadedRecord]] = {}
            for record in batch:
                dataset_id = self._dataset_of(record, mode, datasets, table, type_id)
                self._one(record, dataset_id, plans, by_code, fresh)
            for dataset_id, items in fresh.items():
                records.load(self.tx, dataset_id, items)

    def _one(
        self,
        record: SourceRecord,
        dataset_id: str,
        plans: dict[int, FieldPlan],
        by_code: dict[str, FieldPlan],
        fresh: dict[str, list[records.LoadedRecord]],
    ) -> None:
        """Одна запись: сначала решение, потом преобразование значений.

        Пропущенная запись не преобразуется вовсе, поэтому не добавляет в
        каталог ККС и в справочники ничего своего (MIG-11).
        """
        stats = self.stats[dataset_id]
        final = max([record.version, *(item.version for item in record.versions)])
        verdict, kept_rows = self._decide(record, dataset_id, final)
        if verdict == "skip":
            stats.records_skipped += 1
            return
        if verdict == "unchanged":
            stats.records_unchanged += 1
            self.reconciler.remember(stats, record, plans, kept_rows, final)
            return
        if verdict == "new":
            loaded = self._record(record, plans, by_code)
            if loaded is not None:
                fresh.setdefault(dataset_id, []).append(loaded)
                stats.records_new += 1
                stats.versions += len(loaded.versions)
                self.reconciler.remember(stats, record, plans, len(loaded.versions), final)
            return
        try:
            with self.tx.savepoint():  # запись дописывается целиком или остаётся прежней
                loaded = self._record(record, plans, by_code)
                added = records.extend(self.tx, loaded) if loaded is not None else 0
        except EngineError as error:
            self.mapper.forget()
            self.issues.add("warning", error.code, error.message, record.uuid)
            stats.records_skipped += 1
            return
        if loaded is not None:
            stats.records_extended += 1
            stats.versions += added
            self.reconciler.remember(stats, record, plans, kept_rows + added, final)

    def _decide(self, record: SourceRecord, dataset_id: str, final: int) -> tuple[str, int]:
        """«new», «extend», «unchanged» или «skip» и число уже сохранённых версий (MIG-11)."""
        existing = records.latest_origin(self.tx.conn, record.uuid)
        if existing is None:
            return "new", 0
        version, origin = existing
        row = self.tx.conn.execute(
            "SELECT d.id AS dataset_id, (SELECT COUNT(*) FROM record_versions v"
            " WHERE v.record_seq = r.seq) AS kept FROM records r"
            " JOIN datasets d ON d.seq = r.dataset_seq WHERE r.id = ?",
            (record.uuid,),
        ).fetchone()
        problem = None
        if row["dataset_id"] != dataset_id:
            problem = ("record_moved", "Записи сменили режим в Local_DB после переноса")
        elif origin != ORIGIN:
            problem = (
                "record_edited",
                "Записи изменены в LOCALAI; новые версии из Local_DB не взяты",
            )
        elif version > final:
            problem = ("record_ahead", "Версия записи в LOCALAI новее, чем в Local_DB")
        if problem:
            self.issues.add("warning", problem[0], problem[1], record.uuid)
            return "skip", row["kept"]
        return ("unchanged" if version == final else "extend"), row["kept"]

    def _record(
        self, record: SourceRecord, plans: dict[int, FieldPlan], by_code: dict[str, FieldPlan]
    ) -> records.LoadedRecord | None:
        try:
            current = self.mapper.current_doc(record, plans, self.value_dictionary)
        except EngineError as error:
            self.issues.add(
                "error",
                "value_invalid",
                "Значения, которые не приводятся к виду поля",
                {"message": error.message, **error.details},
            )
            return None
        snapshots = [
            (version, self.mapper.history_doc(version.values, by_code))
            for version in record.versions
        ]
        return build_versions(record, current, snapshots, self.issues)

    # Поля и справочники -------------------------------------------------------

    def _fields(self, table: SourceTable, type_id: str) -> dict[int, FieldPlan]:
        existing = {
            row["id"]: row
            for row in self.tx.conn.execute("SELECT * FROM fields WHERE type_id = ?", (type_id,))
        }
        plans: dict[int, FieldPlan] = {}
        for source_field in table.fields:
            if source_field.code == self.decisions.mode_field:
                continue
            kind = self._kind(source_field)
            dictionary_id = (
                self._dictionary(source_field.dictionary_id) if kind == "choice" else None
            )
            field_id = _uuid(source_field.uuid, f"field:{source_field.uuid}")
            if field_id in existing:
                row = existing[field_id]
                if row["kind"] != kind:
                    self.issues.add(
                        "error",
                        "field_kind_changed",
                        "Поля, у которых в Local_DB сменился вид после прошлого переноса",
                        source_field.name,
                    )
                plans[source_field.id] = FieldPlan(
                    source_field, row["key"], row["kind"], row["dictionary_id"]
                )
                continue
            plans[source_field.id] = self._new_field(
                source_field, type_id, field_id, kind, dictionary_id
            )
        return plans

    def _kind(self, source_field: SourceField) -> str:
        if source_field.code in self.decisions.kks_fields:
            return "kks"
        kind = KIND_BY_TYPE.get(source_field.data_type)
        if kind is None:
            self.issues.add(
                "warning",
                "field_type_unknown",
                "Поля неизвестного вида; перенесены как текст",
                f"{source_field.name}: {source_field.data_type}",
            )
            return "text"
        if kind == "choice" and source_field.dictionary_id not in self.source.dictionaries:
            self.issues.add(
                "warning",
                "field_without_dictionary",
                "Поля-списки без справочника; перенесены как текст",
                source_field.name,
            )
            return "text"
        return kind

    def _new_field(
        self,
        source_field: SourceField,
        type_id: str,
        field_id: str,
        kind: str,
        dictionary_id: str | None,
    ) -> FieldPlan:
        taken = self.tx.conn.execute(
            "SELECT 1 FROM fields WHERE type_id = ? AND key = ?", (type_id, source_field.code)
        ).fetchone()
        key = source_field.code if structure.KEY_RE.match(source_field.code) and not taken else None
        created = structure.add_field(
            self.tx,
            type_id,
            self._free_field_name(type_id, source_field.name),
            kind,
            key=key,
            multiple=source_field.multiple,
            required=source_field.required,
            searchable=source_field.searchable,
            filterable=source_field.filterable,
            sortable=source_field.filterable and not source_field.multiple and kind != "kks",
            dictionary_id=dictionary_id,
            input_mode=("strict" if source_field.input_mode == "strict" else "suggest")
            if kind == "choice"
            else None,
            kks_role="object" if kind == "kks" else None,
            entity_id=field_id,
        )
        counters = self.structure["fields"]
        counters["created"] += 1
        counters["by_kind"][kind] = counters["by_kind"].get(kind, 0) + 1
        if not source_field.active:
            structure.archive_field(self.tx, created["id"])
            counters["archived"] += 1
        if not source_field.visible:
            counters["hidden"] += 1
            self.issues.add(
                "info",
                "field_hidden",
                "Скрытые поля перенесены видимыми; видимость настраивается в интерфейсе",
                source_field.name,
            )
        if source_field.calculated:
            self.issues.add(
                "info",
                "field_calculated",
                "Вычисляемые поля перенесены со значениями, без формулы",
                source_field.name,
            )
        return FieldPlan(source_field, created["key"], kind, dictionary_id)

    def _dictionary(self, source_id: int | None) -> str:
        assert source_id is not None
        if source_id in self.dictionaries:
            return self.dictionaries[source_id]
        source = self.source.dictionaries[source_id]
        dictionary_id = _uuid(source.uuid, f"dictionary:{source.uuid}")
        if self._exists("dictionaries", dictionary_id) is None:
            name = self._free_name("dictionaries", source.name)
            structure.create_dictionary(self.tx, name, entity_id=dictionary_id)
            self.structure["dictionaries"] += 1
        self.dictionaries[source_id] = dictionary_id
        names = self.mapper.remember_names(dictionary_id)
        counters = self.structure["values"]
        for value in source.values:
            if value.merged_into is None:
                self.mapper.choice_ids[value.id] = self._value(dictionary_id, value, names)
        by_id = {value.id: value for value in source.values}
        for value in source.values:
            if value.merged_into is not None:
                final = _final(value, by_id)
                target = self.mapper.choice_ids.get(final.id)
                self.mapper.choice_ids[value.id] = target
                if target is not None and value.canonical.strip():
                    names.setdefault(text.norm_name(value.canonical), target)
                counters["merged"] += 1
        return dictionary_id

    def _value(self, dictionary_id: str, value: Any, names: dict[str, str]) -> str | None:
        counters = self.structure["values"]
        value_id = _uuid(value.uuid, f"value:{value.uuid}")
        if self._exists("dictionary_values", value_id) is not None:
            return value_id
        try:
            shown = text.clean_name(value.canonical, "Значение", structure.MAX_VALUE)
        except EngineError as error:
            level = "error" if error.code == "name_too_long" else "info"
            self.issues.add(level, f"value_{error.code}", error.message, value.canonical[:80])
            return None
        norm = text.norm_name(shown)
        if norm in names:
            counters["merged"] += 1
            self.issues.add(
                "info",
                "value_case_duplicate",
                "Значения справочника, отличающиеся только регистром или «ё»; объединены",
                shown,
            )
            return names[norm]
        synonyms = []
        for alias in value.aliases:
            alias_norm = text.norm_name(alias)
            if alias_norm and alias_norm not in names and alias_norm != norm:
                synonyms.append(alias)
                names[alias_norm] = value_id
        created = structure.add_dictionary_value(
            self.tx,
            dictionary_id,
            shown,
            synonyms,
            archived=not value.active,
            entity_id=value_id,
            audit=False,
        )
        names[norm] = created["id"]
        counters["created"] += 1
        counters["archived"] += int(not value.active)
        return created["id"]

    # Режимы → реестры ---------------------------------------------------------

    def _datasets(
        self, table: SourceTable, type_id: str, mode: SourceField | None
    ) -> dict[int, str]:
        if mode is None or mode.dictionary_id not in self.source.dictionaries:
            self._dataset(type_id, _uuid5(f"localdb:{table.uuid}"), table.name)
            return {}
        values = self.source.dictionaries[mode.dictionary_id].values
        by_id = {value.id: value for value in values}
        found: dict[int, str] = {}
        for value in values:
            if value.merged_into is not None or not value.canonical.strip():
                continue
            dataset_id = _uuid5(f"localdb:{table.uuid}:{value.uuid}")
            name = self.decisions.dataset_names.get(value.uuid) or value.canonical
            self._dataset(type_id, dataset_id, name)
            if not value.active:
                self.to_archive.add(dataset_id)
            found[value.id] = dataset_id
        for value in values:
            if value.merged_into is not None:
                final = _final(value, by_id)
                if final.id in found:
                    found[value.id] = found[final.id]
        return found

    def _dataset(self, type_id: str, dataset_id: str, name: str) -> str:
        assert self.site is not None
        existing = self._exists("datasets", dataset_id)
        if existing is None:
            free = self._free_dataset_name(name)
            created = structure.create_dataset(
                self.tx, self.site["id"], type_id, free, entity_id=dataset_id
            )
            existing = created
        self.stats.setdefault(dataset_id, Stats(dataset_id, existing["name"]))
        return dataset_id

    def _dataset_of(
        self,
        record: SourceRecord,
        mode: SourceField | None,
        datasets: dict[int, str],
        table: SourceTable,
        type_id: str,
    ) -> str:
        if mode is None or not datasets:
            return _uuid5(f"localdb:{table.uuid}")
        for field_id, _ordinal, raw, value_id in record.values:
            if field_id != mode.id:
                continue
            if value_id in datasets:
                return datasets[value_id]
            if raw:
                norm = text.norm_name(str(raw))
                for value in self.source.dictionaries[mode.dictionary_id or 0].values:
                    if value.id in datasets and text.norm_name(value.canonical) == norm:
                        return datasets[value.id]
        self.issues.add(
            "warning",
            "record_without_mode",
            "Записи без режима; перенесены в реестр «Без режима»",
            record.uuid,
        )
        return self._dataset(type_id, _uuid5(f"localdb:{table.uuid}:none"), "Без режима")

    # ККС и журнал -------------------------------------------------------------

    def _kks_summary(self) -> dict[str, Any]:
        assert self.site is not None
        history_only = sorted(self.mapper.kks_new - self.mapper.kks_current)
        for kks_id in history_only:
            kks.update(self.tx, kks_id, archived=True)
        groups: dict[str, set[str]] = {}
        fresh: set[str] = set()
        for row in self.tx.conn.execute(
            "SELECT id, code FROM kks_objects WHERE site_id = ?", (self.site["id"],)
        ):
            groups.setdefault(loose_kks(row["code"]), set()).add(row["code"])
            if row["id"] in self.mapper.kks_new:
                fresh.add(row["code"])
        similar = [sorted(codes) for codes in groups.values() if len(codes) > 1 and codes & fresh]
        for codes in similar:
            self.issues.add(
                "warning",
                "kks_similar",
                "Похожие коды ККС не объединены; проверьте их вручную",
                " · ".join(codes),
            )
        return {
            "created": len(self.mapper.kks_new) - len(history_only),
            "existing": len(self.mapper.kks_current - self.mapper.kks_new),
            "history_only": len(history_only),
            "similar": sorted(similar)[:_MAX_SIMILAR],
        }

    def _audit(self) -> int:
        row = self.tx.conn.execute(
            "SELECT MAX(audit_cursor) FROM import_runs WHERE source_kind = ? AND source_id = ?",
            (SOURCE_KIND, self._source_id()),
        ).fetchone()
        cursor = row[0] or 0
        rows = []
        for event in self.source.audit_events(cursor):
            rows.append(
                (
                    moment(event["occurred_at"]),
                    f"{event['actor_name']} (из Local_DB)",
                    ORIGIN,
                    f"localdb.{str(event['operation']).lower()}",
                    str(event["entity_type"]).lower(),
                    event["entity_uuid"],
                    dumps(
                        {
                            key: event[key]
                            for key in (
                                "uuid",
                                "field_code",
                                "before_json",
                                "after_json",
                                "metadata_json",
                                "ip_address",
                                "mode",
                            )
                            if event[key] is not None
                        }
                    ),
                )
            )
            cursor = event["id"]
        self.tx.conn.executemany(
            "INSERT INTO audit_log (at, actor_id, actor_label, origin, action, entity_kind,"
            " entity_id, dataset_id, details) VALUES (?, NULL, ?, ?, ?, ?, ?, NULL, ?)",
            rows,
        )
        self.audit_cursor = cursor
        return len(rows)

    def _source_id(self) -> str:
        return self.source.database_uuid or f"path:{self.source.path.resolve()}"

    # Мелочи -------------------------------------------------------------------

    def _exists(self, table: str, entity_id: str) -> dict[str, Any] | None:
        row = self.tx.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
        return None if row is None else dict(row)

    def _free_name(self, table: str, name: str) -> str:
        shown = text.clean_name(name or "Без названия")
        candidate, number = shown, 1
        while self.tx.conn.execute(
            f"SELECT 1 FROM {table} WHERE name_norm = ?", (text.norm_name(candidate),)
        ).fetchone():
            number += 1
            candidate = f"{shown} (Local_DB{'' if number == 2 else f' {number - 1}'})"
        return candidate

    def _free_field_name(self, type_id: str, name: str) -> str:
        shown = text.clean_name(name or "Поле", "Название поля")
        candidate, number = shown, 1
        while self.tx.conn.execute(
            "SELECT 1 FROM fields WHERE type_id = ? AND name_norm = ? AND archived_at IS NULL",
            (type_id, text.norm_name(candidate)),
        ).fetchone():
            number += 1
            candidate = f"{shown} ({number})"
        return candidate

    def _free_dataset_name(self, name: str) -> str:
        assert self.site is not None
        shown = text.clean_name(name or "Реестр", "Название реестра")
        candidate, number = shown, 1
        while self.tx.conn.execute(
            "SELECT 1 FROM datasets WHERE site_id = ? AND name_norm = ?",
            (self.site["id"], text.norm_name(candidate)),
        ).fetchone():
            number += 1
            candidate = f"{shown} ({number})"
        return candidate


def _final(value: Any, by_id: dict[int, Any]) -> Any:
    """Конечное значение цепочки объединений справочника Local_DB."""
    seen = set()
    while value.merged_into is not None and value.merged_into in by_id and value.id not in seen:
        seen.add(value.id)
        value = by_id[value.merged_into]
    return value


def _uuid(value: str, seed: str) -> str:
    return value if text.is_uuid(value) else _uuid5(seed)


def _uuid5(seed: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, seed))
