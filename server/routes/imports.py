"""Мастер импорта Excel (ТЗ 6.1, IMP-01…17)."""

from __future__ import annotations

from typing import Any

from engine import EngineError
from engine.errors import FORBIDDEN
from server import schemas
from server.api import Call, Endpoint
from server.import_wizard import batches, plan
from server.xlsx import MAX_BYTES

# Права на реестр проверяются по назначению пакета (IMP-12): маршрут открыт
# вошедшему, а план и применение требуют права «импорт» на реестр или раздел
# «Структура» для нового типа.
SESSION = "session"


def upload(call: Call) -> dict[str, Any]:
    """Шаг 1: загрузить книгу .xlsx (тело — файл, имя — параметр name)."""
    assert call.imports is not None and call.db is not None
    return batches.upload(call.db, call.imports, call.access, call.query.get("name"), call.body)


def get(call: Call) -> dict[str, Any]:
    """Состояние импорта: листы книги, настройки, сводка плана, отчёт."""
    return batches.view(call.conn, call.access, call.params["id"])


def preview(call: Call) -> dict[str, Any]:
    """Лист с выбранной строкой заголовков: пример строк, подсказки, подходящие профили."""
    assert call.imports is not None
    sheet = call.query.get("sheet", "")
    if not sheet:
        raise EngineError("request_invalid", "Нужен параметр sheet", field="sheet")
    return plan.preview(
        call.conn,
        call.imports,
        call.access,
        call.params["id"],
        sheet,
        call.query_int("header_row", 1),
        call.query.get("dataset_id") or None,
    )


def make_plan(call: Call) -> dict[str, Any]:
    """Шаги 2–4: назначение, сопоставление, ключ — и план строк на сервере."""
    assert call.imports is not None and call.db is not None
    settings = call.body.model_dump(exclude_none=True)
    return plan.plan(call.db, call.imports, call.access, call.params["id"], settings)


def rows(call: Call) -> dict[str, Any]:
    """Строки плана: действие, найденная запись, изменения, причина."""
    return batches.rows_page(
        call.conn,
        call.access,
        call.params["id"],
        action=call.query.get("action") or None,
        after=call.query_int("after", 0),
        limit=call.query_int("limit", batches.PAGE),
    )


def decide(call: Call) -> dict[str, Any]:
    """Решение по спорной строке: пропустить или применить."""
    row_no = _row_no(call.params["row"])
    return batches.decide(call.tx, call.access, call.params["id"], row_no, call.body.decision)


def apply(call: Call) -> dict[str, Any]:
    """Шаги 5–6: применить план одной транзакцией и вернуть отчёт."""
    assert call.imports is not None and call.db is not None
    return plan.apply(call.db, call.imports, call.access, call.params["id"], call.body.save_profile)


def list_profiles(call: Call) -> dict[str, Any]:
    """Сохранённые профили импорта."""
    if not batches.can_import(call.access):
        raise EngineError("forbidden", "Недостаточно прав: импорт в реестры", status=FORBIDDEN)
    return {"items": batches.profiles(call.conn)}


def delete_profile(call: Call) -> None:
    """Удалить профиль: автору или администратору."""
    batches.delete_profile(call.tx, call.access, call.params["id"])


def _row_no(raw: str) -> int:
    if not raw.isdigit():
        raise EngineError("request_invalid", "Номер строки — целое число", field="row")
    return int(raw)


def _e(method: str, path: str, handler: Any, **extra: Any) -> Endpoint:
    return Endpoint(method, path, handler, SESSION, tag="Импорт Excel", **extra)


ENDPOINTS = [
    _e(
        "POST",
        "/imports",
        upload,
        query=("name",),
        status=201,
        raw_body=True,
        manual=True,
        idempotent=False,
        max_body=MAX_BYTES,
    ),
    _e("GET", "/imports/{id}", get),
    _e("GET", "/imports/{id}/preview", preview, query=("sheet", "header_row", "dataset_id")),
    # Книга читается вне транзакции записи (API-07): маршруты сами открывают
    # короткие транзакции. План повторяем, применённый пакет отдаёт прежний
    # отчёт — ключ идемпотентности им не нужен (IMP-06).
    _e(
        "POST",
        "/imports/{id}/plan",
        make_plan,
        body=schemas.ImportPlan,
        manual=True,
        idempotent=False,
    ),
    _e("GET", "/imports/{id}/rows", rows, query=("action", "after", "limit")),
    _e("PUT", "/imports/{id}/rows/{row}/decision", decide, body=schemas.ImportDecision),
    _e(
        "POST",
        "/imports/{id}/apply",
        apply,
        body=schemas.ImportApply,
        manual=True,
        idempotent=False,
    ),
    _e("GET", "/import-profiles", list_profiles),
    _e("DELETE", "/import-profiles/{id}", delete_profile, status=204),
]
