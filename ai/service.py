"""HTTP ИИ-службы: принимает вопросы только от сервера (SEC-05).

- слушает только этот компьютер: запрос не с локального адреса — 403;
- каждый запрос несёт ключ сервера ``X-LocalAI-Key`` (файл ``ai.key``);
- ``POST /ask`` — вопрос с делегированным токеном, ответ потоком NDJSON;
- ``GET|POST /health`` — служба жива, какие модели и доступны ли они;
- ``POST /models`` — установленные модели поставщика с умениями;
- ``POST /check`` — проверка модели на вопросе станции.

Служба ничего не хранит: диалоги и ответы сохраняет сервер.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Iterator
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ai import agent, probe
from ai.api import Api, HttpTransport, Transport
from ai.model import Config, Model, best, build

HEADER = "x-localai-key"
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_BODY = 256 * 1024


def create_app(
    *,
    server_url: str,
    key: str,
    model: Model,
    transport: Transport | None = None,
    cafile: str | None = None,
    loopback_only: bool = True,
) -> Starlette:
    """``transport`` подменяет HTTP к серверу (тесты); ``loopback_only`` выключают только тесты."""
    channel: Transport = transport or HttpTransport(server_url, cafile=cafile)

    def guard(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
        async def checked(request: Request) -> Response:
            host = request.client.host if request.client else ""
            if loopback_only and host not in LOOPBACK:
                return _error(
                    403, "ai_local_only", "ИИ-служба принимает запросы только с этого компьютера"
                )
            given = request.headers.get(HEADER, "").encode("utf-8", "replace")
            if not hmac.compare_digest(given, key.encode()):
                return _error(403, "ai_key_invalid", "Неверный ключ сервера")
            return await handler(request)

        return checked

    async def health(request: Request) -> Response:
        """Жива ли служба; с телом ``{models}`` — состояние моделей из настроек сервера."""
        chosen = _chosen(await _json(request), model)
        if isinstance(chosen, Response):
            return chosen
        shown = await run_in_threadpool(lambda: [item.status() for item in chosen])
        return JSONResponse(
            {"service": "ai", "model": shown[0], "fallback": shown[1] if len(shown) > 1 else None}
        )

    async def models(request: Request) -> Response:
        """Установленные модели поставщика с их умениями — для выбора в настройках."""
        try:
            config = Config.parse((await _json(request)).get("config") or {})
        except (ValueError, TypeError) as error:
            return _error(400, "ai_model_invalid", str(error))
        found = build(config)
        items = await run_in_threadpool(found.installed)
        return JSONResponse({"items": items, "auto": best(items) if config.provider == "ollama"
                             else (items[0]["name"] if items else None)})  # fmt: skip

    async def check(request: Request) -> Response:
        """Проверка модели на вопросе станции: вызывает ли инструмент, ссылается ли, по-русски ли."""
        try:
            config = Config.parse((await _json(request)).get("config") or {})
        except (ValueError, TypeError) as error:
            return _error(400, "ai_model_invalid", str(error))
        return JSONResponse(await run_in_threadpool(probe.run, build(config)))

    async def ask(request: Request) -> Response:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return _error(413, "ai_too_large", "Вопрос слишком велик")
        try:
            payload = json.loads(raw)
        except ValueError:
            return _error(400, "ai_bad_request", "Тело — JSON")
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.isascii() or not token.isprintable():
            return _error(400, "ai_bad_request", "Нужны вопрос и токен доступа")
        calls = int((payload.get("limits") or {}).get("calls", 24))
        api = Api(channel, payload["token"], max_calls=calls)
        chosen = _chosen(payload, model)
        if isinstance(chosen, Response):
            return chosen
        return StreamingResponse(_lines(agent.answer(payload, api, chosen)),
                                 media_type="application/x-ndjson")  # fmt: skip

    return Starlette(
        routes=[
            Route("/health", guard(health), methods=["GET", "POST"]),
            Route("/ask", guard(ask), methods=["POST"]),
            Route("/models", guard(models), methods=["POST"]),
            Route("/check", guard(check), methods=["POST"]),
        ]
    )


def _chosen(payload: dict[str, Any], default: Model) -> list[Model] | Response:
    """Основная и запасная модели из настроек сервера; без настроек — модель запуска службы."""
    wanted = payload.get("models") or {}
    try:
        primary = Config.parse(wanted["primary"]) if wanted.get("primary") else None
        fallback = Config.parse(wanted["fallback"]) if wanted.get("fallback") else None
    except (ValueError, TypeError) as error:
        return _error(400, "ai_model_invalid", str(error))
    chosen = [build(primary) if primary else default]
    if fallback:
        chosen.append(build(fallback))
    return chosen


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _lines(events: Iterator[dict[str, Any]]) -> Iterator[bytes]:
    for event in events:
        yield (json.dumps(event, ensure_ascii=False) + "\n").encode()


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
