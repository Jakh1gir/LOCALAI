"""Перевод значений Local_DB в вид хранения LOCALAI и сборка истории версий."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from engine import kks, structure, text
from engine.database import Tx
from engine.errors import EngineError
from engine.records import LoadedRecord, LoadedVersion
from engine.validation import coerce_scalar
from server.localdb_import.report import Issues
from server.localdb_import.source import SourceField, SourceRecord, SourceVersion

KIND_BY_TYPE = {
    "text": "text",
    "integer": "integer",
    "decimal": "number",
    "date": "date",
    "boolean": "boolean",
    "smartfield": "choice",
}
OP_BY_OPERATION = {
    "CREATE_RECORD": "create",
    "MERGE_CREATE_RECORD": "create",
    "UPDATE_RECORD": "update",
    "BATCH_UPDATE_RECORD": "update",
    "MERGE_RECORD": "update",
    "ARCHIVE_RECORD": "archive",
    "RESTORE_RECORD": "restore",
    "RESTORE_RECORD_VERSION": "rollback",
}
FROM_LOCALDB = " (из Local_DB)"
ORIGIN = "localdb"
_LOOKALIKE = str.maketrans("АВЕКМНОРСТУХЁ", "ABEKMHOPCTYXE")
_NOT_ALNUM = re.compile(r"[^0-9A-ZА-Я]")


@dataclass(frozen=True)
class FieldPlan:
    """Как поле Local_DB переносится в LOCALAI."""

    source: SourceField
    key: str
    kind: str
    dictionary_id: str | None  # справочник LOCALAI для поля-списка


def moment(value: Any) -> str:
    """Время Local_DB в формате LOCALAI: UTC, миллисекунды, «Z»."""
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    stamp = stamp.astimezone(UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def loose_kks(code: str) -> str:
    """Ключ похожести кодов: без разделителей, кириллица-двойник как латиница.

    Коды с одинаковым ключом, но разным написанием не объединяются, а
    выносятся на разбор (MIG-06).
    """
    return _NOT_ALNUM.sub("", code.upper().translate(_LOOKALIKE))


def canonical(kind: str, value: Any) -> str:
    """Единое написание значения для контрольной суммы сверки (MIG-13)."""
    if kind in ("integer", "number"):
        try:
            return format(Decimal(str(value)).normalize(), "f")
        except InvalidOperation:
            return str(value)
    if kind == "boolean":
        return "true" if value else "false"
    if kind == "choice":
        return text.norm_name(str(value))
    if kind == "kks":
        return text.norm_kks(str(value))[1]
    return str(value).strip()


class Mapper:
    """Состояние одного переноса: соответствие справочников и каталога ККС."""

    def __init__(self, tx: Tx, site_id: str, issues: Issues) -> None:
        self.tx = tx
        self.site_id = site_id
        self.issues = issues
        self.choice_ids: dict[int, str | None] = {}  # id значения Local_DB → ID LOCALAI
        self._names: dict[str, dict[str, str]] = {}  # справочник → свёрнутое имя → ID
        self._kks: dict[str, str] = {}
        self.kks_new: set[str] = set()
        self.kks_current: set[str] = set()
        self.source_texts: dict[int, str] = {}  # id значения Local_DB → его текст

    def forget(self) -> None:
        """Сбросить запомненное после отката части транзакции.

        Объекты ККС и значения справочников, созданные в откатанной части,
        исчезли из базы; запомненные ID указывали бы в пустоту.
        """
        self._kks.clear()
        self._names.clear()
        for found in (self.kks_new, self.kks_current):
            alive = (
                {
                    row[0]
                    for row in self.tx.conn.execute(
                        f"SELECT id FROM kks_objects WHERE id IN ({','.join('?' * len(found))})",
                        sorted(found),
                    )
                }
                if found
                else set()
            )
            found.intersection_update(alive)

    # Справочники --------------------------------------------------------------

    def remember_names(self, dictionary_id: str) -> dict[str, str]:
        if dictionary_id not in self._names:
            names: dict[str, str] = {}
            for value in structure.list_dictionary_values(
                self.tx.conn, dictionary_id, include_archived=True
            ):
                names.setdefault(text.norm_name(value["value"]), value["id"])
                for synonym in value["synonyms"]:
                    names.setdefault(text.norm_name(synonym), value["id"])
            self._names[dictionary_id] = names
        return self._names[dictionary_id]

    def choice_by_text(self, plan: FieldPlan, raw: str, *, archived: bool) -> str | None:
        """ID значения по тексту; неизвестный текст добавляется в справочник."""
        assert plan.dictionary_id is not None
        try:
            shown = text.clean_name(raw, "Значение", structure.MAX_VALUE)
        except EngineError:
            return None
        names = self.remember_names(plan.dictionary_id)
        found = names.get(text.norm_name(shown))
        if found is not None:
            return found
        created = structure.add_dictionary_value(
            self.tx, plan.dictionary_id, shown, archived=archived, audit=False
        )
        names[text.norm_name(shown)] = created["id"]
        code = "value_history_only" if archived else "value_from_records"
        self.issues.add(
            "info",
            code,
            "Значения из истории записей, которых уже нет в справочнике; добавлены в архив"
            if archived
            else "Значения, введённые текстом без справочника; добавлены в справочник",
            f"{plan.source.name}: {shown}",
        )
        return created["id"]

    # ККС ----------------------------------------------------------------------

    def kks_id(self, raw: str, *, current: bool) -> str | None:
        try:
            _, norm = text.norm_kks(raw)
        except EngineError:
            return None
        found = self._kks.get(norm)
        if found is None:
            resolved = kks.resolve(self.tx.conn, self.site_id, raw)
            if resolved is not None:
                found = resolved["id"]
            else:
                found = kks.create(self.tx, self.site_id, raw, audit=False)["id"]
                self.kks_new.add(found)
            self._kks[norm] = found
        if current:
            self.kks_current.add(found)
        return found

    # Значения -----------------------------------------------------------------

    def current_doc(
        self,
        record: SourceRecord,
        plans: dict[int, FieldPlan],
        value_dictionary: dict[int, int],
    ) -> dict[str, Any]:
        """Документ текущих значений записи. Ошибка значения — ``EngineError``.

        ``value_dictionary`` — справочник Local_DB каждого значения: ссылка на
        значение чужого справочника считается ошибкой данных, а не переносится.
        """
        grouped: dict[str, list[Any]] = {}
        for field_id, _ordinal, raw, dictionary_value_id in record.values:
            plan = plans.get(field_id)
            if plan is None:
                continue
            if (
                plan.kind == "choice"
                and dictionary_value_id is not None
                and value_dictionary.get(dictionary_value_id) != plan.source.dictionary_id
            ):
                raise EngineError(
                    "localdb_foreign_value",
                    "Значение записи ссылается на чужой справочник",
                    details={"record_id": record.uuid, "field": plan.source.name},
                )
            value = self._current_value(plan, raw, dictionary_value_id, record.uuid)
            if value is not None:
                grouped.setdefault(plan.key, []).append(value)
        doc: dict[str, Any] = {}
        by_key = {plan.key: plan for plan in plans.values()}
        for key, items in grouped.items():
            if by_key[key].source.multiple:
                doc[key] = items
            else:
                if len(items) > 1:
                    self.issues.add(
                        "warning",
                        "single_many",
                        "В одиночном поле несколько значений; взято последнее, как в Local_DB",
                        f"{record.uuid}: {by_key[key].source.name}",
                    )
                doc[key] = items[-1]
        return doc

    def _current_value(
        self, plan: FieldPlan, raw: Any, dictionary_value_id: int | None, record_uuid: str
    ) -> Any:
        if plan.kind == "kks":
            shown = (
                raw if dictionary_value_id is None else self.source_texts.get(dictionary_value_id)
            )
            return None if shown in (None, "") else self.kks_id(str(shown), current=True)
        if plan.kind == "choice":
            if dictionary_value_id is not None:
                return self.choice_ids.get(dictionary_value_id)
            if raw in (None, ""):
                return None
            return self.choice_by_text(plan, str(raw), archived=False)
        try:
            return coerce_scalar(plan.kind, _as_text(plan.kind, raw), plan.key)
        except EngineError as error:
            error.details = {"record_id": record_uuid, "field": plan.source.name, "value": raw}
            raise

    def history_doc(self, values: dict[str, Any], plans: dict[str, FieldPlan]) -> dict[str, Any]:
        """Документ из снимка версии. Неприводимое значение остаётся текстом."""
        doc: dict[str, Any] = {}
        for code, raw in values.items():
            plan = plans.get(code)
            if plan is None:
                continue
            items = raw if isinstance(raw, list) else [raw]
            converted = [self._history_value(plan, item) for item in items]
            converted = [item for item in converted if item is not None]
            if not converted:
                continue
            doc[plan.key] = converted if plan.source.multiple else converted[-1]
        return doc

    def _history_value(self, plan: FieldPlan, raw: Any) -> Any:
        if raw is None or raw == "":
            return None
        if plan.kind == "kks":
            return self.kks_id(str(raw), current=False)
        if plan.kind == "choice":
            return self.choice_by_text(plan, str(raw), archived=True)
        try:
            return coerce_scalar(plan.kind, _as_text(plan.kind, raw), plan.key)
        except EngineError:
            self.issues.add(
                "warning",
                "history_raw",
                "Значения в истории, которые не приводятся к виду поля; сохранены как текст",
                f"{plan.source.name}: {raw!r}",
            )
            return str(raw)


def _as_text(kind: str, raw: Any) -> Any:
    return str(raw) if kind == "text" and not isinstance(raw, str) else raw


def build_versions(
    record: SourceRecord,
    current: dict[str, Any],
    snapshots: list[tuple[SourceVersion, dict[str, Any]]],
    issues: Issues,
) -> LoadedRecord:
    """Версии записи: разницы между соседними снимками, последняя — до текущих значений.

    Номера версий Local_DB сохраняются (MIG-05). Если снимка последней
    версии нет, она строится по полям «кто и когда изменил» самой записи.
    """
    final = max([record.version, *(item.version for item, _ in snapshots)])
    versions: list[LoadedVersion] = []
    previous: dict[str, Any] | None = None
    for item, doc in snapshots:
        state = current if item.version == final else doc
        if item.version == final and doc != current:
            issues.add(
                "info",
                "snapshot_mismatch",
                "Последний снимок версии расходится с текущими значениями; взяты текущие",
                record.uuid,
            )
        versions.append(_version(item, {} if previous is None else diff(previous, state)))
        previous = state
    if not versions or versions[-1].version != final:
        op = "create" if final == 1 else ("update" if versions else "import")
        actor = record.created_by if final == 1 else record.updated_by
        at = record.created_at if final == 1 else record.updated_at
        changes = {} if previous is None else diff(previous, current)
        versions.append(LoadedVersion(final, op, changes, actor + FROM_LOCALDB, moment(at), ORIGIN))
        if previous is not None:
            issues.add(
                "info",
                "history_partial",
                "Записи с неполной историей версий в Local_DB",
                record.uuid,
            )
    return LoadedRecord(
        id=record.uuid,
        doc=current,
        versions=tuple(versions),
        created_at=moment(record.created_at),
        updated_at=moment(record.updated_at),
        archived_at=None if record.archived_at is None else moment(record.archived_at),
    )


def _version(item: SourceVersion, changes: dict[str, Any]) -> LoadedVersion:
    op = OP_BY_OPERATION.get(item.operation, "update")
    return LoadedVersion(
        version=item.version,
        op=op,
        changes=changes,
        actor_label=(item.actor or "неизвестно") + FROM_LOCALDB,
        at=moment(item.at),
        origin=ORIGIN,
        restored_from=item.restored_from if op == "rollback" else None,
    )


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[Any]]:
    keys = sorted(set(before) | set(after))
    return {
        key: [before.get(key), after.get(key)] for key in keys if before.get(key) != after.get(key)
    }
