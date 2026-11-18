"""Маршруты записей: страница, подсчёт, создание, правка, архив, история, откат."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from starlette.responses import Response

from engine import EngineError, batch, facets, links, query, records, structure
from engine.errors import FORBIDDEN
from server import export, schemas
from server.api import Call, Endpoint


def _schema(call: Call, dataset_id: str) -> structure.TypeSchema:
    dataset = structure.load_dataset(call.reader, dataset_id)
    return structure.load_schema(call.reader, dataset["type_id"])


def _hidden(call: Call, dataset_id: str) -> frozenset[str]:
    return call.access.hidden_keys(_schema(call, dataset_id))


def _check_editable(call: Call, dataset_id: str, keys: Iterable[str]) -> None:
    """Скрытые и закрытые для правки поля роль менять не может (ROLE-03)."""
    locked = sorted(set(keys) & call.access.locked_keys(_schema(call, dataset_id)))
    if locked:
        raise EngineError(
            "field_forbidden",
            "Недостаточно прав на изменение поля",
            status=FORBIDDEN,
            field=locked[0],
        )


def _visible(doc: dict[str, Any], hidden: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in doc.items() if key not in hidden}


def query_records(call: Call) -> dict[str, Any]:
    """Страница записей реестра на языке запросов."""
    request = call.body.model_dump(exclude_none=True)
    dataset_id = call.params["id"]
    return query.list_records(call.conn, dataset_id, request, hidden=_hidden(call, dataset_id))


def count_records(call: Call) -> dict[str, Any]:
    """Число записей по условиям."""
    request = call.body.model_dump(exclude_none=True)
    dataset_id = call.params["id"]
    hidden = _hidden(call, dataset_id)
    return {"count": query.count_records(call.conn, dataset_id, request, hidden=hidden)}


def value_counts(call: Call) -> dict[str, Any]:
    """Значения поля и число записей с каждым — для фильтра столбца."""
    request = call.body.model_dump(exclude_none=True)
    field, limit = request.pop("field"), request.pop("limit")
    dataset_id = call.params["id"]
    return facets.value_counts(
        call.conn, dataset_id, field, request, limit=limit, hidden=_hidden(call, dataset_id)
    )


def export_records(call: Call) -> Response:
    """Выгрузить записи реестра в Excel, CSV или JSON с текущими условиями."""
    dataset_id = call.params["id"]
    if not call.access.can(dataset_id, "view"):
        raise EngineError(
            "forbidden",
            "Недостаточно прав: видеть записи",
            status=FORBIDDEN,
            details={"permission": "dataset.view"},
        )
    request = call.body.model_dump(exclude_none=True)
    body, filename, media = export.export(call.conn, call.access, dataset_id, request)
    return Response(
        body, media_type=media, headers={"content-disposition": export.disposition(filename)}
    )


def batch_update(call: Call) -> dict[str, Any]:
    """Одни и те же значения в выбранных записях; всё или ничего."""
    dataset_id = call.params["id"]
    _check_editable(call, dataset_id, call.body.values)
    items = [item.model_dump() for item in call.body.items]
    return batch.update_many(call.tx, dataset_id, items, call.body.values)


def batch_purge(call: Call) -> dict[str, Any]:
    """Окончательно удалить записи из архива; всё или ничего."""
    items = [item.model_dump() for item in call.body.items]
    return batch.purge_many(call.tx, call.params["id"], items)


def create_record(call: Call) -> dict[str, Any]:
    """Создать запись."""
    dataset_id = call.params["id"]
    _check_editable(call, dataset_id, call.body.values)
    return _shown(call, records.create(call.tx, dataset_id, call.body.values))


def get_record(call: Call) -> dict[str, Any]:
    """Запись, названия для её ID и записи, которые на неё ссылаются (DATA-13)."""
    record = _shown(call, records.get(call.conn, call.params["id"]))
    seq = call.conn.execute("SELECT seq FROM records WHERE id = ?", (record["id"],)).fetchone()[0]
    backlinks = [
        item
        for item in links.backlinks(call.conn, seq)
        if call.access.can(item["dataset_id"], "view")
        and item["field_key"] not in _hidden(call, item["dataset_id"])
    ]
    return {
        "record": record,
        "refs": query.refs(call.conn, record["dataset_id"], [record["doc"]]),
        "backlinks": backlinks,
        "backlink_refs": links.labels(call.conn, {item["record_id"] for item in backlinks}),
    }


def update_record(call: Call) -> dict[str, Any]:
    """Изменить значения записи с проверкой ожидаемой версии."""
    record = records.get(call.tx.conn, call.params["id"])
    _check_editable(call, record["dataset_id"], call.body.values)
    changed = records.update(
        call.tx, call.params["id"], call.body.values, call.body.expected_version
    )
    return _shown(call, changed)


def archive_record(call: Call) -> dict[str, Any]:
    """Отправить запись в архив."""
    changed = records.set_archived(call.tx, call.params["id"], True, call.body.expected_version)
    return _shown(call, changed)


def restore_record(call: Call) -> dict[str, Any]:
    """Вернуть запись из архива."""
    changed = records.set_archived(call.tx, call.params["id"], False, call.body.expected_version)
    return _shown(call, changed)


def history(call: Call) -> dict[str, Any]:
    """Версии записи от новой к старой."""
    record = records.get(call.conn, call.params["id"])
    hidden = _hidden(call, record["dataset_id"])
    items = records.history(call.conn, call.params["id"])
    for item in items:
        item["changes"] = _visible(item["changes"], hidden)
    return {"items": items}


def version(call: Call) -> dict[str, Any]:
    """Документ записи на выбранной версии."""
    try:
        number = int(call.params["n"])
    except ValueError:
        raise EngineError("version_unknown", "Номер версии — целое число", field="n") from None
    record = records.get(call.conn, call.params["id"])
    doc = _visible(
        records.doc_at(call.conn, call.params["id"], number), _hidden(call, record["dataset_id"])
    )
    return {"doc": doc, "refs": query.refs(call.conn, record["dataset_id"], [doc])}


def rollback(call: Call) -> dict[str, Any]:
    """Вернуть значения выбранной версии новой версией."""
    body = call.body
    record = records.get(call.tx.conn, call.params["id"])
    # Откат меняет все поля, которые различаются; среди них не должно быть
    # полей, закрытых для этой роли.
    target = records.doc_at(call.tx.conn, call.params["id"], body.to_version)
    changed = {
        key for key in set(target) | set(record["doc"]) if target.get(key) != record["doc"].get(key)
    }
    _check_editable(call, record["dataset_id"], changed)
    result = records.rollback(call.tx, call.params["id"], body.to_version, body.expected_version)
    return _shown(call, result)


def _shown(call: Call, record: dict[str, Any]) -> dict[str, Any]:
    hidden = _hidden(call, record["dataset_id"])
    return {**record, "doc": _visible(record["doc"], hidden)} if hidden else record


def _e(method: str, path: str, handler, permission: str, **extra: Any) -> Endpoint:  # type: ignore[no-untyped-def]
    return Endpoint(method, path, handler, permission, tag="Записи", **extra)


ON_DATASET = {"target": "dataset"}
ON_RECORD = {"target": "record"}

ENDPOINTS = [
    _e(
        "POST",
        "/datasets/{id}/records/query",
        query_records,
        "dataset.view",
        body=schemas.RecordQuery,
        read_only=True,
        **ON_DATASET,
    ),
    _e(
        "POST",
        "/datasets/{id}/records/count",
        count_records,
        "dataset.view",
        body=schemas.RecordCount,
        read_only=True,
        **ON_DATASET,
    ),
    _e(
        "POST",
        "/datasets/{id}/records/values",
        value_counts,
        "dataset.view",
        body=schemas.ValueCounts,
        read_only=True,
        **ON_DATASET,
    ),
    _e(
        "POST",
        "/datasets/{id}/export",
        export_records,
        "dataset.export",
        body=schemas.ExportRequest,
        read_only=True,
        **ON_DATASET,
    ),
    _e(
        "POST",
        "/datasets/{id}/records/batch-update",
        batch_update,
        "dataset.edit",
        body=schemas.BatchUpdate,
        **ON_DATASET,
    ),
    _e(
        "POST",
        "/datasets/{id}/records/purge",
        batch_purge,
        "dataset.purge",
        body=schemas.BatchPurge,
        **ON_DATASET,
    ),
    _e(
        "POST",
        "/datasets/{id}/records",
        create_record,
        "dataset.create",
        body=schemas.RecordCreate,
        status=201,
        **ON_DATASET,
    ),
    _e("GET", "/records/{id}", get_record, "dataset.view", **ON_RECORD),
    _e(
        "PATCH",
        "/records/{id}",
        update_record,
        "dataset.edit",
        body=schemas.RecordPatch,
        **ON_RECORD,
    ),
    _e(
        "POST",
        "/records/{id}/archive",
        archive_record,
        "dataset.archive",
        body=schemas.VersionBody,
        **ON_RECORD,
    ),
    _e(
        "POST",
        "/records/{id}/restore",
        restore_record,
        "dataset.archive",
        body=schemas.VersionBody,
        **ON_RECORD,
    ),
    _e("GET", "/records/{id}/history", history, "dataset.view", **ON_RECORD),
    _e("GET", "/records/{id}/versions/{n}", version, "dataset.view", **ON_RECORD),
    _e(
        "POST",
        "/records/{id}/rollback",
        rollback,
        "dataset.edit",
        body=schemas.RollbackBody,
        **ON_RECORD,
    ),
]
