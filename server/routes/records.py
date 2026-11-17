"""Маршруты записей: страница, подсчёт, создание, правка, архив, история, откат."""

from __future__ import annotations

from typing import Any

from engine import EngineError, query, records
from server import schemas
from server.api import Call, Endpoint

READ, WRITE = "records.read", "records.write"


def query_records(call: Call) -> dict[str, Any]:
    """Страница записей реестра на языке запросов."""
    request = call.body.model_dump(exclude_none=True)
    return query.list_records(call.conn, call.params["id"], request)


def count_records(call: Call) -> dict[str, Any]:
    """Число записей по условиям."""
    request = call.body.model_dump(exclude_none=True)
    return {"count": query.count_records(call.conn, call.params["id"], request)}


def create_record(call: Call) -> dict[str, Any]:
    """Создать запись."""
    return records.create(call.tx, call.params["id"], call.body.values)


def get_record(call: Call) -> dict[str, Any]:
    """Запись и названия для её ID."""
    record = records.get(call.conn, call.params["id"])
    return {"record": record, "refs": query.refs(call.conn, record["dataset_id"], [record["doc"]])}


def update_record(call: Call) -> dict[str, Any]:
    """Изменить значения записи с проверкой ожидаемой версии."""
    return records.update(call.tx, call.params["id"], call.body.values, call.body.expected_version)


def archive_record(call: Call) -> dict[str, Any]:
    """Отправить запись в архив."""
    return records.set_archived(call.tx, call.params["id"], True, call.body.expected_version)


def restore_record(call: Call) -> dict[str, Any]:
    """Вернуть запись из архива."""
    return records.set_archived(call.tx, call.params["id"], False, call.body.expected_version)


def history(call: Call) -> dict[str, Any]:
    """Версии записи от новой к старой."""
    return {"items": records.history(call.conn, call.params["id"])}


def version(call: Call) -> dict[str, Any]:
    """Документ записи на выбранной версии."""
    try:
        number = int(call.params["n"])
    except ValueError:
        raise EngineError("version_unknown", "Номер версии — целое число", field="n") from None
    record = records.get(call.conn, call.params["id"])
    doc = records.doc_at(call.conn, call.params["id"], number)
    return {"doc": doc, "refs": query.refs(call.conn, record["dataset_id"], [doc])}


def rollback(call: Call) -> dict[str, Any]:
    """Вернуть значения выбранной версии новой версией."""
    body = call.body
    return records.rollback(call.tx, call.params["id"], body.to_version, body.expected_version)


def _e(method: str, path: str, handler, permission: str, **extra: Any) -> Endpoint:  # type: ignore[no-untyped-def]
    return Endpoint(method, path, handler, permission, tag="Записи", **extra)


ENDPOINTS = [
    _e("POST", "/datasets/{id}/records/query", query_records, READ, body=schemas.RecordQuery),
    _e("POST", "/datasets/{id}/records/count", count_records, READ, body=schemas.RecordCount),
    _e(
        "POST",
        "/datasets/{id}/records",
        create_record,
        WRITE,
        body=schemas.RecordCreate,
        status=201,
    ),
    _e("GET", "/records/{id}", get_record, READ),
    _e("PATCH", "/records/{id}", update_record, WRITE, body=schemas.RecordPatch),
    _e("POST", "/records/{id}/archive", archive_record, WRITE, body=schemas.VersionBody),
    _e("POST", "/records/{id}/restore", restore_record, WRITE, body=schemas.VersionBody),
    _e("GET", "/records/{id}/history", history, READ),
    _e("GET", "/records/{id}/versions/{n}", version, READ),
    _e("POST", "/records/{id}/rollback", rollback, WRITE, body=schemas.RollbackBody),
]
