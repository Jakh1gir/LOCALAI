"""Поддельный ``ollama serve`` для тестов: тот же HTTP, модели — в памяти процесса.

Адрес — из ``OLLAMA_HOST``, журнал вызовов и окружения — строки JSON в ``FAKE_LOG``.
"""

from __future__ import annotations

import json
import os
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

LOG = os.environ["FAKE_LOG"]
INSTALLED: dict[str, list[str]] = {"embed:latest": ["embedding"]}
SIZES = {"qwen3.5:9b": "9.7B", "qwen3.5:4b": "4.7B", "embed:latest": "335M"}


def note(**item: object) -> None:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


async def version(_request: Request) -> JSONResponse:
    return JSONResponse({"version": "0.0-test"})


async def tags(_request: Request) -> JSONResponse:
    return JSONResponse({"models": [
        {"name": name, "digest": name, "size": 1, "details": {"parameter_size": SIZES[name]}}
        for name in INSTALLED
    ]})  # fmt: skip


async def show(request: Request) -> JSONResponse:
    name = (await request.json())["model"]
    return JSONResponse({"capabilities": INSTALLED[name], "model_info": {}})


async def pull(request: Request) -> StreamingResponse:
    name = (await request.json())["model"]
    note(call="pull", model=name)

    def lines():
        for done in (0, 50, 100):
            yield (
                json.dumps({"status": "downloading", "total": 100, "completed": done}) + "\n"
            ).encode()
        INSTALLED[name] = ["completion", "tools", "thinking"]
        yield b'{"status": "success"}\n'

    return StreamingResponse(lines(), media_type="application/x-ndjson")


async def generate(request: Request) -> JSONResponse:
    body = await request.json()
    note(call="generate", model=body["model"], keep_alive=body.get("keep_alive"))
    return JSONResponse({"model": body["model"], "done": True, "done_reason": "load"})


def main() -> None:
    host, _, port = os.environ["OLLAMA_HOST"].rpartition(":")
    note(call="start", pid=os.getpid(), env={key: value for key, value in os.environ.items()
                                             if key.startswith("OLLAMA_")})  # fmt: skip
    app = Starlette(routes=[
        Route("/api/version", version), Route("/api/tags", tags),
        Route("/api/show", show, methods=["POST"]), Route("/api/pull", pull, methods=["POST"]),
        Route("/api/generate", generate, methods=["POST"]),
    ])  # fmt: skip
    uvicorn.run(app, host=host, port=int(port), log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
