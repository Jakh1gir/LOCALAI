"""Каркас API версии 1: объявление маршрутов, права, транзакции, ошибки.

Каждый маршрут объявляется один раз объектом ``Endpoint``. Из того же
объявления строятся маршрутизация, проверка прав, описание API (API-11) и
тесты на дубли (API-01) и на отказ по умолчанию (API-02).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from engine import Actor, Database, EngineError, idempotency
from engine.errors import CONFLICT, INVALID, NOT_FOUND, TOO_LARGE

PREFIX = "/api/v1"
log = logging.getLogger("localai.server")

# Права этапа 1. С этапа 2 каждое из них выдаётся ролью; маршрут без права
# недоступен (API-02). «public» — открытые маршруты, их список виден в одном месте.
PERMISSIONS = {
    "public": "Без входа",
    "structure.read": "Просмотр структуры: площадки, типы, поля, реестры, справочники",
    "structure.write": "Изменение структуры",
    "records.read": "Просмотр записей",
    "records.write": "Изменение записей",
    "kks.read": "Просмотр каталога ККС",
    "kks.write": "Изменение каталога ККС",
    "audit.read": "Просмотр журнала",
}
_STATUS = {NOT_FOUND: 404, INVALID: 400, CONFLICT: 409, TOO_LARGE: 413}
MAX_BODY = 10 * 1024 * 1024


@dataclass
class Call:
    """Всё, что получает обработчик: параметры пути и строки, тело, база и автор."""

    params: dict[str, str]
    query: dict[str, str]
    body: Any
    actor: Actor
    conn: Any = None  # соединение для чтения или транзакция записи (``tx``)

    @property
    def tx(self) -> Any:
        return self.conn

    def query_int(self, name: str, default: int) -> int:
        raw = self.query.get(name)
        if raw in (None, ""):
            return default
        try:
            return int(raw)
        except ValueError:
            raise EngineError(
                "request_invalid", f"Параметр «{name}»: ожидается целое число", field=name
            ) from None

    def query_bool(self, name: str) -> bool:
        return self.query.get(name, "").lower() in ("1", "true", "да", "yes")


@dataclass(frozen=True)
class Endpoint:
    """Объявление маршрута API."""

    method: str
    path: str
    handler: Callable[[Call], Any]
    permission: str
    body: type[BaseModel] | None = None
    status: int = 200
    query: tuple[str, ...] = ()
    tag: str = ""
    writes: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "writes",
            self.method in ("POST", "PATCH", "PUT", "DELETE")
            and self.permission.endswith(".write"),
        )

    @property
    def summary(self) -> str:
        return (self.handler.__doc__ or "").strip().split("\n")[0]


class Server:
    """Связывает объявления маршрутов с базой."""

    def __init__(self, db: Database, actor: Actor) -> None:
        self.db = db
        self.actor = actor

    def route(self, endpoint: Endpoint) -> Route:
        async def handle(request: Request) -> Response:
            # Лимит тела проверяется у каждого маршрута до его выполнения, а не
            # только у тех, что ждут тело (API-08).
            raw = await _read_limited(request)
            body = _body(raw, endpoint)
            call = Call(
                params=dict(request.path_params),
                query=dict(request.query_params),
                body=body,
                actor=self.actor,
            )
            key = request.headers.get("idempotency-key")
            if endpoint.writes and not key:
                raise EngineError(
                    "idempotency_key_required",
                    "Изменяющий запрос должен нести заголовок Idempotency-Key",
                    field="Idempotency-Key",
                )
            result = await run_in_threadpool(self._run, endpoint, call, key)
            if endpoint.status == 204:
                return Response(status_code=204)
            return JSONResponse(result, status_code=endpoint.status)

        return Route(
            PREFIX + endpoint.path,
            handle,
            methods=[endpoint.method],
            name=endpoint.handler.__name__,
        )

    def _run(self, endpoint: Endpoint, call: Call, key: str | None) -> Any:
        if not endpoint.writes:
            with self.db.read() as conn:
                call.conn = conn
                return endpoint.handler(call)
        request = {"params": call.params, "query": call.query, "body": _plain(call.body)}
        with self.db.write(call.actor) as tx:
            call.conn = tx
            scope = f"{endpoint.method} {endpoint.path}"
            return idempotency.run(tx, scope, key, request, lambda: endpoint.handler(call))


def _body(raw: bytes, endpoint: Endpoint) -> Any:
    if endpoint.body is None:
        return None
    try:
        # Дробные числа читаются как Decimal: иначе точность теряется ещё до
        # проверки значения, и 9007199254740993.0 молча станет ...992.
        data = json.loads(raw or b"{}", parse_float=Decimal)
    except ValueError:
        raise EngineError("request_json", "Тело запроса не является JSON") from None
    try:
        return endpoint.body.model_validate(data)
    except ValidationError as error:
        raise _validation_error(error) from None


async def _read_limited(request: Request) -> bytes:
    """Тело запроса не больше ``MAX_BODY``; лишнее не читается в память."""
    too_large = EngineError(
        "request_too_large",
        f"Тело запроса больше {MAX_BODY // (1024 * 1024)} МиБ",
        status=TOO_LARGE,
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise too_large
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


def _plain(body: Any) -> Any:
    return body.model_dump(exclude_unset=True) if isinstance(body, BaseModel) else body


_PYDANTIC_TEXT = {
    "missing": "обязательное поле",
    "extra_forbidden": "лишнее поле",
    "string_type": "ожидается текст",
    "int_type": "ожидается целое число",
    "int_parsing": "ожидается целое число",
    "bool_type": "ожидается «да» или «нет»",
    "bool_parsing": "ожидается «да» или «нет»",
    "dict_type": "ожидается объект",
    "list_type": "ожидается список",
    "literal_error": "недопустимое значение",
    "model_type": "ожидается объект",
}


def _validation_error(error: ValidationError) -> EngineError:
    """Ошибка проверки тела — по-русски и с указанием поля (API-08)."""
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    text = _PYDANTIC_TEXT.get(first["type"], "недопустимое значение")
    message = f"Поле «{where}»: {text}" if where else f"Тело запроса: {text}"
    return EngineError("request_invalid", message, field=where or None)


def error_response(error: EngineError) -> JSONResponse:
    return JSONResponse({"error": error.to_dict()}, status_code=_STATUS.get(error.status, 400))


async def on_engine_error(_request: Request, error: Exception) -> Response:
    assert isinstance(error, EngineError)
    return error_response(error)


async def on_http_error(_request: Request, error: Exception) -> Response:
    assert isinstance(error, HTTPException)
    known = {
        404: ("route_not_found", "Нет такого адреса API"),
        405: ("method_not_allowed", "Этот метод для адреса не поддерживается"),
        413: ("request_too_large", "Тело запроса слишком большое"),
    }
    code, message = known.get(error.status_code, ("http_error", str(error.detail)))
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=error.status_code
    )


async def on_unexpected(_request: Request, error: Exception) -> Response:
    log.exception("Необработанная ошибка сервера", exc_info=error)
    return JSONResponse(
        {
            "error": {
                "code": "internal",
                "message": "Внутренняя ошибка сервера; подробности в журнале",
            }
        },
        status_code=500,
    )
