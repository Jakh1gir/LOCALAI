"""План и применение импорта (ТЗ 6.1, шаги 2–6; IMP-01…12).

План строится тем же кодом, что и применение: все строки выполняются в
части транзакции, которая затем откатывается. Поэтому план точен — повтор
ключа в книге, уникальные поля, справочники и ККС проверены так же, как при
записи. Применение заново выполняет строки плана и сверяет состояние базы с
планом (IMP-04): если запись, схема или права изменились, план устарел и
ничего не записывается.

Режимы:

- ``records`` — в существующий реестр. Запись опознаётся по UUID из шаблона
  выгрузки (столбец «@id»), затем по уникальному полю-ключу, иначе
  создаётся (IMP-01, IMP-02). Пустая ячейка значение не стирает (IMP-07).
- ``new_type`` — «Создать тип и загрузить»: тип, поля и реестр создаются при
  применении; нужен раздел «Структура» (IMP-12).
- ``magnet`` — таблица сигналов раскладывается магнитом ККС (IMP-13…16),
  см. :mod:`server.import_wizard.magnet_mode`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine import Database, EngineError, Tx, events, links, magnet, records, structure, text
from engine.access import Access
from engine.database import dumps
from engine.errors import CONFLICT, FORBIDDEN
from server import xlsx
from server.import_wizard import batches, magnet_mode

ID_COLUMN, VERSION_COLUMN = "@id", "@version"
NEW_KINDS = ("text", "integer", "number", "date", "boolean", "kks")
MAX_ROWS = 5000  # IMP-11


class _Undo(Exception):
    """Откат пробного выполнения: план записывается, данные — нет."""


# Шаг 1–3: лист с заголовками и подсказки -------------------------------------------


def preview(
    conn: Any,
    folder: Path,
    access: Access,
    batch_id: str,
    sheet: str,
    header_row: int,
    dataset_id: str | None,
) -> dict[str, Any]:
    """Заголовки, 20 строк примера, роли столбцов по магниту и поля реестра."""
    batch = batches.row(conn, access, batch_id)
    page = xlsx.read_sheet(
        batches.file_of(folder, batch), sheet, header_row=header_row, max_rows=MAX_ROWS
    )
    split = magnet_mode.splitter(conn)
    layout = magnet.suggest_layout(page.headers, [values for _, values in page.rows], split)
    signals = bool(layout.signal) and any(values.get(layout.signal) for _, values in page.rows)
    mapping: dict[str, str] = {}
    if dataset_id:
        mapping = _guess_mapping(conn, access, dataset_id, page.headers)
    return {
        "sheet": page.name,
        "header_row": header_row,
        "headers": page.headers,
        "rows": len(page.rows),
        "sample": [
            {
                "row": number,
                "cells": {key: value for key, value in values.items() if value not in (None, "")},
            }
            for number, values in page.rows[: xlsx.SAMPLE_ROWS]
        ],
        "suggested": {
            "mode": "magnet" if signals else "records",
            "layout": layout.to_dict(),
            "mapping": mapping,
        },
        "profiles": batches.profiles(conn, headers=page.headers),
    }


def _guess_mapping(
    conn: Any, access: Access, dataset_id: str, headers: list[str]
) -> dict[str, str]:
    """Столбец ↔ поле по совпадению названия или ключа; UUID и версия шаблона выгрузки."""
    dataset = structure.get_dataset(conn, dataset_id)
    schema = structure.load_schema(conn, dataset["type_id"])
    locked = access.locked_keys(schema)
    by_name = {
        text.norm_name(item.name): item.key for item in schema.active() if item.key not in locked
    }
    by_key = {item.key: item.key for item in schema.active() if item.key not in locked}
    found: dict[str, str] = {}
    for header in headers:
        folded = text.norm_name(header)
        if folded in ("uuid", "id", "@id"):
            found[header] = ID_COLUMN
        elif folded in ("версия", "version", "@version"):
            found[header] = VERSION_COLUMN
        elif folded in by_name:
            found[header] = by_name[folded]
        elif header in by_key:
            found[header] = by_key[header]
    return found


# Шаг 3–4: план ---------------------------------------------------------------------


def plan(
    db: Database, folder: Path, access: Access, batch_id: str, settings: dict[str, Any]
) -> dict[str, Any]:
    """Книга читается вне транзакции (API-07), строки проверяются в короткой транзакции."""
    with db.read() as conn:
        batch = batches.row(conn, access, batch_id)
    if batch["status"] == "applied":
        raise EngineError("import_applied", "Этот импорт уже применён", status=CONFLICT)
    batches.check_open(batch)
    page = xlsx.read_sheet(
        batches.file_of(folder, batch),
        settings["sheet"],
        header_row=settings.get("header_row", 1),
        max_rows=MAX_ROWS,
    )
    with db.write(access.actor) as tx:
        _plan(tx, access, batch_id, page, settings)
    with db.read() as conn:
        return batches.view(conn, access, batch_id)


def _plan(
    tx: Tx, access: Access, batch_id: str, page: xlsx.Sheet, settings: dict[str, Any]
) -> None:
    batch = batches.row(tx.conn, access, batch_id)
    if batch["status"] == "applied":
        raise EngineError("import_applied", "Этот импорт уже применён", status=CONFLICT)
    tx.conn.execute("DELETE FROM import_rows WHERE batch_id = ?", (batch_id,))
    mode = settings["mode"]
    if mode == "magnet":
        summary, schema_revs = magnet_mode.plan(tx, access, page, settings)
    else:
        job = _Job(tx, access, page, settings)
        try:
            with tx.savepoint():
                outcomes = job.run(planning=True)
                raise _Undo
        except _Undo:
            pass
        _store_rows(tx, batch_id, outcomes)
        summary, schema_revs = _summary(outcomes, job), job.schema_revs()
    summary["headers"] = page.headers
    tx.conn.execute(
        "UPDATE import_batches SET status = 'planned', settings = ?, summary = ?, report = NULL,"
        " plan_cursor = ?, plan_schema = ?, planned_at = ? WHERE id = ?",
        (
            dumps(settings),
            dumps(summary),
            events.cursor(tx.conn),
            dumps(schema_revs),
            tx.now,
            batch_id,
        ),
    )


def apply(
    db: Database, folder: Path, access: Access, batch_id: str, save_profile: str | None
) -> dict[str, Any]:
    """Шаг 5–6: одной транзакцией (IMP-05); повтор применённого — прежний отчёт (IMP-06)."""
    with db.read() as conn:
        batch = batches.row(conn, access, batch_id)
    page = None
    if batch["status"] == "planned" and batch["settings"].get("mode") == "magnet":
        page = xlsx.read_sheet(  # вне транзакции записи (API-07)
            batches.file_of(folder, batch),
            batch["settings"]["sheet"],
            header_row=batch["settings"].get("header_row", 1),
            max_rows=MAX_ROWS,
        )
    with db.write(access.actor) as tx:
        return _apply(tx, folder, access, batch_id, save_profile, page)


def _apply(
    tx: Tx,
    folder: Path,
    access: Access,
    batch_id: str,
    save_profile: str | None,
    page: xlsx.Sheet | None,
) -> dict[str, Any]:
    batch = batches.row(tx.conn, access, batch_id)
    if batch["status"] == "applied":
        return batch["report"]
    if batch["status"] != "planned":
        raise EngineError("import_not_planned", "Сначала постройте план импорта", status=CONFLICT)
    batches.check_open(batch)
    settings = batch["settings"]
    if _schema_moved(tx.conn, batch["plan_schema"] or {}):
        raise _stale("Структура реестра изменилась после проверки")
    if settings["mode"] == "magnet":
        assert page is not None
        report = magnet_mode.apply(tx, access, page, settings, batch)
    else:
        report = _apply_rows(tx, access, batch)
    if save_profile:
        report["profile"] = batches.save_profile(
            tx, access, save_profile, batch["summary"]["headers"], settings
        )
    tx.conn.execute(
        "UPDATE import_batches SET status = 'applied', report = ?, applied_at = ? WHERE id = ?",
        (dumps(report), tx.now, batch_id),
    )
    tx.conn.execute("DELETE FROM import_rows WHERE batch_id = ?", (batch_id,))
    (folder / f"{batch_id}.xlsx").unlink(missing_ok=True)
    tx.audit("import.apply", "import", batch_id, details=report["counts"])
    return report


def _apply_rows(tx: Tx, access: Access, batch: dict[str, Any]) -> dict[str, Any]:
    settings = batch["settings"]
    planned = {
        item["row_no"]: dict(item)
        for item in tx.conn.execute(
            "SELECT * FROM import_rows WHERE batch_id = ? ORDER BY row_no", (batch["id"],)
        )
    }
    rows = [(number, json.loads(item["cells"])) for number, item in planned.items()]
    job = _Job(
        tx, access, xlsx.Sheet(settings["sheet"], batch["summary"]["headers"], rows), settings
    )
    outcomes = job.run(planning=False, planned=planned)
    counts = {action: 0 for action in batches.ACTIONS}
    skipped = []
    for item in outcomes:
        counts[item["action"]] += 1
        if item["action"] in ("conflict", "error"):
            skipped.append(
                {"row": item["row"], "reason": item["reason"], "message": item["message"]}
            )
    return {
        "counts": counts,
        "skipped": skipped[:200],
        "dataset_id": job.dataset_id,
        "type_id": job.type_id,
    }


# Выполнение строк: и для плана, и для применения -------------------------------------


class _Job:
    def __init__(self, tx: Tx, access: Access, page: xlsx.Sheet, settings: dict[str, Any]) -> None:
        self.tx, self.access, self.page, self.settings = tx, access, page, settings
        self.mode = settings["mode"]
        self.dataset_id: str | None = settings.get("dataset_id")
        self.type_id: str | None = None
        self.mapping: dict[str, str] = dict(settings.get("mapping") or {})
        self.key: str | None = settings.get("key")
        self.multiple: set[str] = set()

    def schema_revs(self) -> dict[str, int]:
        if self.mode != "records" or self.type_id is None:
            return {}
        row = self.tx.conn.execute("SELECT schema_rev FROM types WHERE id = ?", (self.type_id,))
        return {self.type_id: row.fetchone()[0]}

    def run(
        self, *, planning: bool, planned: dict[int, dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        self._target()
        schema = structure.load_schema(self.tx.conn, self.type_id)
        self._check_mapping(schema)
        dataset_seq = structure.load_dataset(self.tx.conn, self.dataset_id)["seq"]
        key_field = schema.by_key(self.key) if self.key else None
        self.multiple = {item.key for item in schema.fields if item.multiple}
        created: dict[str, int] = {}  # ключ созданной записи или ID найденной → строка книги
        outcomes = []
        for number, cells in self.page.rows:
            plan_row = planned.get(number) if planned else None
            outcome = self._row(number, cells, key_field, dataset_seq, created, plan_row, planning)
            outcomes.append(outcome)
        return outcomes

    def _target(self) -> None:
        if self.mode == "records":
            if not self.dataset_id:
                raise EngineError("import_target", "Выберите реестр", field="dataset_id")
            dataset = structure.get_dataset(self.tx.conn, self.dataset_id)
            if dataset["archived_at"]:
                raise EngineError("import_target", "Реестр в архиве", field="dataset_id")
            if not self.access.can(self.dataset_id, "import"):
                raise EngineError(
                    "forbidden", "Недостаточно прав: импорт в этот реестр", status=FORBIDDEN
                )
            self.type_id = dataset["type_id"]
            return
        self._new_type()

    def _new_type(self) -> None:
        """«Создать тип и загрузить»: тип, поля по столбцам и реестр (IMP-12)."""
        if not self.access.has("structure"):
            raise EngineError(
                "forbidden",
                "Недостаточно прав: создать тип может роль со «Структурой»",
                status=FORBIDDEN,
            )
        spec = self.settings.get("new_type") or {}
        if not spec.get("fields"):
            raise EngineError("import_new_type", "Укажите столбцы нового типа", field="new_type")
        created = structure.create_type(
            self.tx, spec.get("type_name", ""), "Создан мастером импорта"
        )
        self.type_id = created["id"]
        self.mapping, self.key = {}, None
        for item in spec["fields"]:
            if item["column"] not in self.page.headers:
                raise EngineError("import_column", f"Столбца «{item['column']}» нет на листе")
            kind = item.get("kind", "text")
            if kind not in NEW_KINDS:
                raise EngineError("import_new_type", f"Недопустимый вид поля: {kind}")
            made = structure.add_field(
                self.tx,
                self.type_id,
                item.get("name") or item["column"],
                kind,
                kks_role="object" if kind == "kks" else None,
                unique=bool(item.get("unique")) and kind != "kks",
                filterable=True,
            )
            self.mapping[item["column"]] = made["key"]
            if item.get("unique") and self.key is None and kind != "kks":
                self.key = made["key"]
        dataset = structure.create_dataset(
            self.tx, spec.get("site_id", ""), self.type_id, spec.get("dataset_name", "")
        )
        self.dataset_id = dataset["id"]

    def _check_mapping(self, schema: structure.TypeSchema) -> None:
        if not self.mapping:
            raise EngineError(
                "import_mapping", "Свяжите хотя бы один столбец с полем", field="mapping"
            )
        locked = self.access.locked_keys(schema)
        used: set[str] = set()
        for header, target in self.mapping.items():
            if header not in self.page.headers:
                raise EngineError(
                    "import_column", f"Столбца «{header}» нет на листе", field="mapping"
                )
            if target in used:
                raise EngineError(
                    "import_mapping", f"С полем «{target}» связаны два столбца", field="mapping"
                )
            used.add(target)
            if target in (ID_COLUMN, VERSION_COLUMN):
                continue
            item = schema.by_key(target)
            if item is None or item.archived_at:
                raise EngineError("import_mapping", f"Поля «{target}» нет в типе", field="mapping")
            if target in locked:
                raise EngineError(
                    "field_forbidden",
                    f"Поле «{item.name}» закрыто для правки вашей роли",
                    status=FORBIDDEN,
                    field="mapping",
                )
        if self.key is not None:
            item = schema.by_key(self.key)
            if item is None or self.key not in used:
                raise EngineError("import_key", "Ключ — одно из сопоставленных полей", field="key")
            if not item.unique_values:
                raise EngineError(
                    "import_key",
                    f"Ключом может быть только уникальное поле; «{item.name}» не уникально"
                    " (код ККС объекта ключом не служит: у объекта много строк)",
                    field="key",
                )

    def _row(
        self,
        number: int,
        cells: dict[str, Any],
        key_field: structure.Field | None,
        dataset_seq: int,
        created: dict[str, int],
        plan_row: dict[str, Any] | None,
        planning: bool,
    ) -> dict[str, Any]:
        kept = {
            header: value
            for header, value in cells.items()
            if header in self.mapping and value not in (None, "")
        }
        values = {
            self.mapping[header]: _split(value) if self.mapping[header] in self.multiple else value
            for header, value in kept.items()
            if self.mapping[header] not in (ID_COLUMN, VERSION_COLUMN)
        }
        outcome: dict[str, Any] = {"row": number, "cells": kept, "record_id": None}
        if plan_row is not None and plan_row["action"] in ("unchanged", "error"):
            return {**outcome, **_from_plan(plan_row)}
        if (
            plan_row is not None
            and plan_row["action"] == "conflict"
            and plan_row["decision"] != "apply"
        ):
            return {**outcome, **_from_plan(plan_row)}
        try:
            with self.tx.savepoint():
                found = self._find(kept, values, key_field, dataset_seq, created)
                if found is not None:
                    if found["id"] in created:
                        raise EngineError(
                            "key_duplicate",
                            f"Эта запись уже задана строкой {created[found['id']]} книги",
                        )
                    created[found["id"]] = number
                if found is None:
                    if plan_row is not None and plan_row["action"] != "create":
                        raise _stale(f"Строка {number}: запись, найденная при проверке, исчезла")
                    made = records.create(self.tx, self.dataset_id, values)
                    if key_field is not None and values.get(key_field.key) is not None:
                        created[links.unique_value(key_field, values[key_field.key])] = number
                    return {**outcome, "action": "create", "record_id": made["id"]}
                return {**outcome, **self._update(found, kept, values, plan_row, planning)}
        except EngineError as error:
            if not planning:
                raise _failed(number, error) from None
            action = "conflict" if error.code in ("version_conflict", "key_duplicate") else "error"
            return {
                **outcome,
                "action": action,
                "reason": error.code,
                "message": error.message,
                "record_id": error.details.get("record_id"),
                "base_version": error.details.get("current_version"),
            }

    def _find(
        self,
        kept: dict[str, Any],
        values: dict[str, Any],
        key_field: structure.Field | None,
        dataset_seq: int,
        created: dict[str, int],
    ) -> dict[str, Any] | None:
        id_header = _header_of(self.mapping, ID_COLUMN)
        if id_header and kept.get(id_header):
            record_id = str(kept[id_header]).strip().lower()
            row = self.tx.conn.execute(
                "SELECT r.id, r.version FROM records r WHERE r.id = ? AND r.dataset_seq = ?",
                (record_id, dataset_seq),
            ).fetchone()
            if row is None:
                raise EngineError(
                    "import_record_unknown",
                    f"Запись {record_id} из шаблона не найдена в этом реестре",
                )
            return dict(row)
        if key_field is None or values.get(key_field.key) in (None, ""):
            return None
        norm = links.unique_value(key_field, values[key_field.key])
        if norm in created:
            raise EngineError(
                "key_duplicate",
                f"Ключ «{values[key_field.key]}» уже встретился в строке {created[norm]} книги",
            )
        row = self.tx.conn.execute(
            "SELECT r.id, r.version FROM record_unique u JOIN records r ON r.seq = u.record_seq"
            " WHERE u.dataset_seq = ? AND u.field_key = ? AND u.value = ?",
            (dataset_seq, key_field.key, norm),
        ).fetchone()
        return dict(row) if row else None

    def _update(
        self,
        found: dict[str, Any],
        kept: dict[str, Any],
        values: dict[str, Any],
        plan_row: dict[str, Any] | None,
        planning: bool,
    ) -> dict[str, Any]:
        version_header = _header_of(self.mapping, VERSION_COLUMN)
        current = found["version"]
        if plan_row is not None:
            if plan_row["base_version"] != current:
                raise _stale("Запись изменена после проверки", plan_row["row_no"])
        elif version_header and kept.get(version_header) not in (None, ""):
            exported = _as_int(kept[version_header])
            if exported != current:
                raise EngineError(
                    "version_conflict",
                    f"Запись изменена после выгрузки: в книге версия {exported}, в базе {current}",
                    details={"record_id": found["id"], "current_version": current},
                )
        before = records.get(self.tx.conn, found["id"])
        after = records.update(self.tx, found["id"], values, current)
        changes = {
            key: [before["doc"].get(key), after["doc"].get(key)]
            for key in sorted(set(before["doc"]) | set(after["doc"]))
            if before["doc"].get(key) != after["doc"].get(key)
        }
        action = "update" if after["version"] != current else "unchanged"
        return {
            "action": action,
            "record_id": found["id"],
            "base_version": current,
            "changes": changes or None,
        }


# Мелочи -----------------------------------------------------------------------------


def _store_rows(tx: Tx, batch_id: str, outcomes: list[dict[str, Any]]) -> None:
    tx.conn.executemany(
        "INSERT INTO import_rows (batch_id, row_no, action, record_id, base_version, cells,"
        " changes, reason, message) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                batch_id,
                item["row"],
                item["action"],
                item.get("record_id"),
                item.get("base_version"),
                dumps(item["cells"]),
                dumps(item["changes"]) if item.get("changes") else None,
                item.get("reason"),
                item.get("message"),
            )
            for item in outcomes
        ],
    )


def _summary(outcomes: list[dict[str, Any]], job: _Job) -> dict[str, Any]:
    counts = {action: 0 for action in batches.ACTIONS}
    for item in outcomes:
        counts[item["action"]] += 1
    return {"mode": job.settings["mode"], "rows": len(outcomes), "counts": counts}


def _from_plan(plan_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": plan_row["action"],
        "record_id": plan_row["record_id"],
        "reason": plan_row["reason"],
        "message": plan_row["message"],
    }


def _schema_moved(conn: Any, revs: dict[str, int]) -> bool:
    for type_id, rev in revs.items():
        row = conn.execute("SELECT schema_rev FROM types WHERE id = ?", (type_id,)).fetchone()
        if row is None or row[0] != rev:
            return True
    return False


def _split(value: Any) -> Any:
    """Несколько значений в одной ячейке — через «;», как их выгружает экспорт."""
    if not isinstance(value, str):
        return [value]
    return [item.strip() for item in value.split(";") if item.strip()]


def _header_of(mapping: dict[str, str], target: str) -> str | None:
    return next((header for header, item in mapping.items() if item == target), None)


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _stale(message: str, row: int | None = None) -> EngineError:
    return EngineError(
        "import_stale",
        f"{message}. План устарел: постройте его заново",
        status=CONFLICT,
        details={"row": row} if row else {},
    )


def _failed(number: int, error: EngineError) -> EngineError:
    if error.code == "import_stale":
        return error
    return EngineError(
        "import_failed",
        f"Строка {number}: {error.message}. Ничего не записано",
        status=CONFLICT,
        details={"row": number, "code": error.code},
    )
