"""HTTP ИИ-службы: принимает вопросы только от сервера (SEC-05).

- слушает только этот компьютер: запрос не с локального адреса — 403;
- каждый запрос несёт ключ сервера ``X-LocalAI-Key`` (файл ``ai.key``);
- ``POST /ask`` — вопрос с делегированным токеном, ответ потоком NDJSON;
- ``GET /health`` — служба жива, какая модель и доступна ли она.

Служба ничего не хранит: диалоги и ответы сохраняет сервер.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Iterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ai import agent
from ai.api import Api, HttpTransport, Transport
from ai.model import Model

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

    async def health(_request: Request) -> Response:
        return JSONResponse({"service": "ai", "model": model.status()})

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
        return StreamingResponse(_lines(agent.answer(payload, api, model)),
                                 media_type="application/x-ndjson")  # fmt: skip

    return Starlette(
        routes=[
            Route("/health", guard(health), methods=["GET"]),
            Route("/ask", guard(ask), methods=["POST"]),
        ]
    )


def _lines(events: Iterator[dict[str, Any]]) -> Iterator[bytes]:
    for event in events:
        yield (json.dumps(event, ensure_ascii=False) + "\n").encode()


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
