"""Каркас API версии 1: объявление маршрутов, права, транзакции, ошибки.

Каждый маршрут объявляется один раз объектом ``Endpoint``. Из того же
объявления строятся маршрутизация, проверка прав, описание API (API-11) и
тесты на дубли (API-01) и на отказ по умолчанию (API-02).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from engine import SYSTEM, Database, EngineError, access, idempotency, network, sessions, text
from engine.access import Access
from engine.database import Tx
from engine.errors import (
    CONFLICT,
    FORBIDDEN,
    INVALID,
    NOT_FOUND,
    TOO_LARGE,
    TOO_MANY,
    UNAUTHORIZED,
    not_found,
)
from engine.sessions import Session

PREFIX = "/api/v1"
COOKIE = "localai_session"
log = logging.getLogger("localai.server")

# Право маршрута (API-02). Маршрут без права из этого списка не собирается;
# «public» — открытые маршруты, их список виден в одном месте. Разделы и
# действия над реестром выдаёт роль (ROLE-03); для «dataset.*» реестр берётся
# из пути запроса — сам реестр или реестр записи (``Endpoint.target``).
PERMISSIONS = {
    "public": "Без входа",
    "self": "Вошедший пользователь, в том числе обязанный сменить пароль",
    "session": "Вошедший пользователь",
    **{f"section.{name}": label for name, label in access.SECTIONS.items()},
    **{f"dataset.{name}": label for name, label in access.DATASET_ACTIONS.items()},
}
_STATUS = {
    NOT_FOUND: 404,
    INVALID: 400,
    CONFLICT: 409,
    TOO_LARGE: 413,
    UNAUTHORIZED: 401,
    FORBIDDEN: 403,
    TOO_MANY: 429,
}
_WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")
MAX_BODY = 10 * 1024 * 1024
MAX_WAIT = 25.0  # секунд держит ответ маршрут ожидания событий
RECHECK = 1.0  # проверка базы без сигнала: запись из другого процесса (перенос, CLI)


@dataclass
class Call:
    """Всё, что получает обработчик: параметры пути и строки, тело, база, права."""

    params: dict[str, str]
    query: dict[str, str]
    body: Any
    client: str | None = None
    throttle: Throttle | None = None
    # Для маршрутов, которые сами управляют транзакциями (``Endpoint.manual``).
    db: Database | None = None
    backups: Path | None = None
    # Папка рабочих файлов мастера импорта: загруженные книги.
    imports: Path | None = None
    conn: Any = None  # соединение для чтения или транзакция записи (``tx``)
    session: Session | None = None
    # Ответ на вход и выход: новый токен для cookie или "" — стереть cookie.
    cookie: str | None = None
    # Поля ответа, которые не сохраняются вместе с ключом идемпотентности:
    # временный пароль показывается один раз и не лежит в базе открытым.
    once: dict[str, Any] = field(default_factory=dict)
    # Отказ, который сообщается после записи транзакции: неудачный вход
    # должен оставить след в журнале и в счётчике попыток.
    deferred: EngineError | None = None

    @property
    def tx(self) -> Any:
        return self.conn

    @property
    def reader(self) -> Any:
        """Соединение для чтения и в маршруте записи — внутри его транзакции."""
        return self.conn.conn if isinstance(self.conn, Tx) else self.conn

    @property
    def access(self) -> Access:
        assert self.session is not None, "маршрут без входа не читает права"
        return self.session.access

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
    # Для права «dataset.*»: что лежит в {id} пути — реестр или запись.
    target: str | None = None
    # POST, который только читает (запрос страницы записей).
    read_only: bool = False
    # Вход, выход и смена пароля не повторяются по ключу: в отпечатке
    # запроса оказался бы пароль, а в ответе — токен сессии.
    idempotent: bool = True
    # Долгое действие с файлами вне транзакции (копия базы, восстановление):
    # права проверяются в коротком чтении, дальше обработчик сам открывает
    # транзакции и не держит очередь записи. Повтора по ключу у него нет: его
    # результат — файл, а не строки в той же транзакции.
    manual: bool = False
    # Ожидание событий (API-09): если ответ пуст, маршрут повторяется после
    # каждой записи в базу, пока не появятся события или не выйдет ``wait``.
    poll: bool = False
    # Тело — файл как есть (книга Excel), а не JSON; ``max_body`` — его предел.
    raw_body: bool = False
    max_body: int = MAX_BODY
    writes: bool = field(init=False)

    def __post_init__(self) -> None:
        if self.permission not in PERMISSIONS:
            raise ValueError(f"{self.method} {self.path}: неизвестное право {self.permission}")
        if self.permission.startswith("dataset.") and self.target not in ("dataset", "record"):
            raise ValueError(f"{self.method} {self.path}: для права на реестр нужен target")
        if self.manual and self.idempotent:
            raise ValueError(f"{self.method} {self.path}: ручной маршрут не повторяется по ключу")
        if self.raw_body and (self.body is not None or self.idempotent):
            raise ValueError(f"{self.method} {self.path}: файл в теле — без схемы и без ключа")
        writes = self.method in _WRITE_METHODS and not self.read_only and not self.manual
        object.__setattr__(self, "writes", writes)

    @property
    def summary(self) -> str:
        return (self.handler.__doc__ or "").strip().split("\n")[0]


class Throttle:
    """Ограничение неудачных входов с одного адреса (SEC-02).

    Счётчик по учётной записи хранится в базе; этот — в памяти процесса и
    сдерживает перебор логинов, которых нет в базе.
    """

    LIMIT = 20
    WINDOW = 15 * 60

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, client: str | None) -> None:
        with self._lock:
            recent = self._recent(client or "")
            if len(recent) >= self.LIMIT:
                wait = int(recent[0] + self.WINDOW - time.monotonic()) // 60 + 1
                raise EngineError(
                    "login_throttled",
                    f"Слишком много неудачных входов с этого компьютера. Повторите через {wait} мин",
                    status=TOO_MANY,
                )

    def fail(self, client: str | None) -> None:
        with self._lock:
            self._recent(client or "").append(time.monotonic())

    def _recent(self, client: str) -> deque[float]:
        found = self._failures.setdefault(client, deque())
        while found and found[0] < time.monotonic() - self.WINDOW:
            found.popleft()
        return found


class Changes:
    """Будит ожидающих событий, когда в базу записана транзакция (API-09).

    Запись идёт в потоках, ожидание — в цикле asyncio: сигнал передаётся через
    ``call_soon_threadsafe``. Ожидающий не держит поток и соединение с базой.
    """

    def __init__(self) -> None:
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = set()
        self._lock = threading.Lock()

    def notify(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for loop, future in waiters:
            loop.call_soon_threadsafe(_wake, future)

    async def wait(self, timeout: float) -> bool:
        """``True`` — была запись; ``False`` — вышло время."""
        loop = asyncio.get_running_loop()
        item = (loop, loop.create_future())
        with self._lock:
            self._waiters.add(item)
        try:
            await asyncio.wait_for(item[1], timeout)
            return True
        except TimeoutError:
            return False
        finally:
            with self._lock:
                self._waiters.discard(item)


def _wake(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class Server:
    """Связывает объявления маршрутов с базой и правами."""

    def __init__(self, db: Database, backups: Path, imports: Path | None = None) -> None:
        self.db = db
        self.backups = backups
        self.imports = imports or backups.parent / "imports"
        self.throttle = Throttle()
        self.changes = Changes()
        db.on_commit(self.changes.notify)

    def route(self, endpoint: Endpoint) -> Route:
        async def handle(request: Request) -> Response:
            # Лимит тела проверяется у каждого маршрута до его выполнения, а не
            # только у тех, что ждут тело (API-08).
            raw = await _read_limited(request, endpoint.max_body)
            call = Call(
                params=dict(request.path_params),
                query=dict(request.query_params),
                body=raw,
                client=request.client.host if request.client else None,
                throttle=self.throttle,
                db=self.db if endpoint.manual else None,
                backups=self.backups if endpoint.manual else None,
                imports=self.imports,
            )
            key = request.headers.get("idempotency-key")
            token = _token(request)
            if endpoint.poll:
                result = await self._poll(endpoint, call, key, token)
            else:
                result = await run_in_threadpool(self._run, endpoint, call, key, token)
            if call.deferred is not None:
                raise call.deferred
            if isinstance(result, Response):  # файл копии и подобное
                return result
            if endpoint.status == 204:
                response: Response = Response(status_code=204)
            else:
                response = JSONResponse(result, status_code=endpoint.status)
            if call.cookie is not None:
                _set_cookie(response, call.cookie, secure=request.url.scheme == "https")
            return response

        return Route(
            PREFIX + endpoint.path,
            handle,
            methods=[endpoint.method],
            name=endpoint.handler.__name__,
        )

    async def _poll(
        self, endpoint: Endpoint, call: Call, key: str | None, token: str | None
    ) -> Any:
        """Маршрут ожидания: права проверяются заново на каждом шаге (ROLE-09)."""
        wait = _wait(call.query.get("wait"))
        deadline = time.monotonic() + wait
        while True:
            step = dataclasses.replace(call, query=dict(call.query))
            result = await run_in_threadpool(self._run, endpoint, step, key, token)
            left = deadline - time.monotonic()
            if result.get("events") or left <= 0:
                return result
            call.query["after"] = str(result["cursor"])
            await self.changes.wait(min(left, RECHECK))

    def _run(self, endpoint: Endpoint, call: Call, key: str | None, token: str | None) -> Any:
        """Сначала права, потом разбор запроса: без входа маршрут не раскрывает,
        какое тело он ждёт."""
        if endpoint.manual:
            with self.db.read() as conn:
                self._authorize(endpoint, call, conn, token)
                call.body = _body(call.body, endpoint)
            return endpoint.handler(call)
        if not endpoint.writes:
            with self.db.read() as conn:
                call.conn = conn
                self._authorize(endpoint, call, conn, token)
                call.body = _body(call.body, endpoint)
                result = endpoint.handler(call)
            if call.session is not None and call.session.stale:
                with self.db.write(call.access.actor) as tx:
                    sessions.touch(tx, call.session)
            return result
        with self.db.write(SYSTEM) as tx:
            call.conn = tx
            self._authorize(endpoint, call, tx.conn, token)
            if endpoint.idempotent and not key:
                raise EngineError(
                    "idempotency_key_required",
                    "Изменяющий запрос должен нести заголовок Idempotency-Key",
                    field="Idempotency-Key",
                )
            call.body = _body(call.body, endpoint)
            if call.session is not None:
                tx.actor = call.access.actor  # в журнал пишется учётная запись (ROLE-04)
                if call.session.stale:
                    sessions.touch(tx, call.session)
            if not endpoint.idempotent:
                return endpoint.handler(call)
            request = {"params": call.params, "query": call.query, "body": _plain(call.body)}
            # Ключ принадлежит пользователю: чужой ключ не вернёт чужой ответ.
            owner = call.access.user_id if call.session else "-"
            scope = f"{owner} {endpoint.method} {endpoint.path}"
            result = idempotency.run(tx, scope, key, request, lambda: endpoint.handler(call))
        return {**result, **call.once} if call.once else result

    def _authorize(self, endpoint: Endpoint, call: Call, conn: Any, token: str | None) -> None:
        """Отказ по умолчанию (API-02): права читаются из базы на каждый запрос (ROLE-09)."""
        network.check(conn, call.client)  # список адресов действует и на открытые маршруты
        if endpoint.permission == "public":
            return
        call.session = sessions.resolve(conn, token, text.now())
        if endpoint.permission == "self":
            return
        if call.access.must_change_password:
            raise EngineError(
                "password_change_required",
                "Сначала смените временный пароль",
                status=FORBIDDEN,
            )
        kind, _, name = endpoint.permission.partition(".")
        if kind == "section" and not call.access.has(name):
            raise _forbidden(endpoint.permission)
        if kind == "dataset" and not call.access.can(_dataset_of(endpoint, call, conn), name):
            raise _forbidden(endpoint.permission)


def _dataset_of(endpoint: Endpoint, call: Call, conn: Any) -> str:
    if endpoint.target == "dataset":
        return call.params["id"]
    row = conn.execute(
        "SELECT d.id FROM records r JOIN datasets d ON d.seq = r.dataset_seq WHERE r.id = ?",
        (call.params["id"],),
    ).fetchone()
    if row is None:
        raise not_found("record", call.params["id"])
    return row[0]


def _wait(raw: str | None) -> float:
    if raw in (None, ""):
        return 0.0
    try:
        seconds = float(raw)
    except ValueError:
        seconds = -1.0
    if not 0 <= seconds <= MAX_WAIT:
        raise EngineError(
            "request_invalid", f"Параметр «wait»: от 0 до {MAX_WAIT:.0f} секунд", field="wait"
        )
    return seconds


def _forbidden(permission: str) -> EngineError:
    return EngineError(
        "forbidden",
        f"Недостаточно прав: {PERMISSIONS[permission].lower()}",
        status=FORBIDDEN,
        details={"permission": permission},
    )


def _token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(COOKIE) or None


def _set_cookie(response: Response, token: str, *, secure: bool) -> None:
    """Сессия в cookie, недоступной скриптам страницы; другой сайт её не отправит."""
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(sessions.LIFETIME.total_seconds()) if token else 0,
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure,
    )


def _body(raw: bytes, endpoint: Endpoint) -> Any:
    if endpoint.raw_body:
        return raw
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


async def _read_limited(request: Request, limit: int = MAX_BODY) -> bytes:
    """Тело запроса не больше ``limit``; лишнее не читается в память."""
    too_large = EngineError(
        "request_too_large",
        f"Тело запроса больше {limit // (1024 * 1024)} МиБ",
        status=TOO_LARGE,
    )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
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
