"""Действия над группой записей одного реестра (F-007, F-009).

Группа задаётся списком ``[{"id": UUID, "expected_version": n}]``: каждая
запись проверяется на свою версию, как при одиночной правке (DATA-04).
Действие идёт одной транзакцией: ошибка на любой записи отменяет всё и
называет номер строки списка и ID записи.
"""

from __future__ import annotations

import json
from typing import Any

from engine.database import Tx
from engine.errors import CONFLICT, EngineError
from engine.records import MAX_BULK, _row_for_write, _Writer


def update_many(
    tx: Tx, dataset_id: str, items: list[dict[str, Any]], patch: dict[str, Any]
) -> dict[str, Any]:
    """Одни и те же значения в выбранных записях. У каждой — своя версия и история."""
    _check_items(items)
    if not isinstance(patch, dict) or not patch:
        raise EngineError("values_invalid", "Укажите хотя бы одно поле и значение", field="values")
    writer = _Writer(tx, dataset_id)
    result = []
    changed = 0
    for number, item in enumerate(items):
        try:
            row = _row_for_write(tx.conn, item["id"], item["expected_version"])
            _check_dataset(row, dataset_id)
            old = json.loads(row["doc"])
            changes = writer.normalizer.partial(patch, current=old)
            diff = {key: [old.get(key), new] for key, new in changes.items() if old.get(key) != new}
            version = row["version"]
            if diff:
                writer.apply(row, old, diff, "update")
                version += 1
                changed += 1
                tx.audit(
                    "record.update",
                    "record",
                    row["id"],
                    dataset_id=dataset_id,
                    details={"fields": sorted(diff), "batch": True},
                )
        except EngineError as error:
            error.details = {**error.details, "row": number, "record_id": item["id"]}
            raise
        result.append({"id": row["id"], "version": version})
    tx.audit(
        "record.update_many",
        "dataset",
        dataset_id,
        dataset_id=dataset_id,
        details={"count": len(items), "changed": changed, "fields": sorted(patch)},
    )
    return {"changed": changed, "items": result}


def purge_many(tx: Tx, dataset_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Окончательно удаляет записи из архива: документ, историю, связи ККС, поиск.

    Удаляются только записи в архиве: действующую запись сначала отправляют в
    архив. Объекты ККС остаются в каталоге. В журнале остаётся строка
    ``record.purge`` с ID записи и её последней версией — без значений.
    """
    _check_items(items)
    for number, item in enumerate(items):
        try:
            row = _row_for_write(tx.conn, item["id"], item["expected_version"], allow_archived=True)
            _check_dataset(row, dataset_id)
            if row["archived_at"] is None:
                raise EngineError(
                    "record_not_archived",
                    "Окончательно удаляется только запись из архива",
                    status=CONFLICT,
                )
        except EngineError as error:
            error.details = {**error.details, "row": number, "record_id": item["id"]}
            raise
        referenced = tx.conn.execute(
            "SELECT 1 FROM record_links WHERE target_seq = ? LIMIT 1", (row["seq"],)
        ).fetchone()
        if referenced:
            raise EngineError(
                "record_referenced",
                "На запись ссылаются другие записи; сначала уберите ссылки",
                status=CONFLICT,
                details={"row": number, "record_id": item["id"]},
            )
        tx.conn.execute("DELETE FROM record_fts WHERE rowid = ?", (row["seq"],))
        # История, связи ККС, ссылки и уникальные значения удаляются каскадом.
        tx.conn.execute("DELETE FROM records WHERE seq = ?", (row["seq"],))
        tx.audit(
            "record.purge",
            "record",
            row["id"],
            dataset_id=dataset_id,
            details={"version": row["version"], "archived_at": row["archived_at"]},
        )
    tx.audit(
        "record.purge_many",
        "dataset",
        dataset_id,
        dataset_id=dataset_id,
        details={"count": len(items)},
    )
    return {"purged": len(items)}


def _check_items(items: Any) -> None:
    if not isinstance(items, list) or not items:
        raise EngineError("items_invalid", "Выберите хотя бы одну запись", field="items")
    if len(items) > MAX_BULK:
        raise EngineError(
            "bulk_too_large", f"За один раз обрабатывается до {MAX_BULK} записей", field="items"
        )
    seen: set[str] = set()
    for number, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or type(item.get("expected_version")) is not int
        ):
            raise EngineError(
                "items_invalid",
                f"Строка {number + 1}: нужны id и expected_version",
                field=f"items.{number}",
            )
        if item["id"] in seen:
            raise EngineError(
                "items_duplicate",
                f"Строка {number + 1}: запись указана дважды",
                field=f"items.{number}",
            )
        seen.add(item["id"])


def _check_dataset(row: Any, dataset_id: str) -> None:
    if row["dataset_id"] != dataset_id:
        raise EngineError("record_other_dataset", "Запись принадлежит другому реестру")
