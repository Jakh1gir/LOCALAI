"""Тестовый клиент держит соединение, пока ответ не отправлен (запрос 004)."""

from __future__ import annotations

import anyio.lowlevel
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from .asgi import Client


async def _chunks():
    for number in range(5):
        # Между кусками сервер успевает спросить о разрыве соединения.
        await anyio.lowlevel.checkpoint()
        yield f"кусок {number};".encode()


async def _stream(request):
    return StreamingResponse(_chunks(), media_type="text/plain")


def test_streamed_body_is_not_cut_by_an_early_disconnect():
    app = Starlette(routes=[Route("/api/v1/stream", _stream)])
    reply = Client(app, login=None).get("/stream")
    assert reply.status == 200
    assert reply.body.decode() == "".join(f"кусок {number};" for number in range(5))
