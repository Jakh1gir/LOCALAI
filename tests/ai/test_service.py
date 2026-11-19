"""Сквозной путь по HTTP: интерфейс → сервер → ИИ-служба → API сервера → модель Ollama.

Три настоящих процесса заменены тремя серверами uvicorn в потоках этого
теста: сервер LOCALAI, ИИ-служба и поддельный Ollama с тем же протоколом
``/api/chat``. Проверяются ключ сервера (SEC-05), адаптер модели (AI-01),
поток ответа (AI-11) и быстрый путь (AI-03). Данные вымышленные.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from ai import service
from ai.model import Ollama
from server import ai_link
from server.app import create_app
from tests.server.asgi import ADMIN_PASSWORD, Client

from .conftest import build_plant


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Running:
    def __init__(self, app: Any, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("сервер не запустился")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


def fake_ollama(seen: list[dict[str, Any]]) -> Starlette:
    """Ollama с одной моделью: первый шаг — вызов aggregate, второй — ответ по его итогу."""

    async def tags(_request: Request) -> JSONResponse:
        return JSONResponse({"models": [{"name": "test-model:latest"}]})

    async def chat(request: Request) -> StreamingResponse:
        body = await request.json()
        seen.append(body)
        results = [
            json.loads(item["content"]) for item in body["messages"] if item["role"] == "tool"
        ]

        def lines() -> Iterator[bytes]:
            if not results:
                call = {
                    "function": {
                        "name": "aggregate",
                        "arguments": {"dataset": "Насосы", "group_by": "Состояние"},
                    }
                }
                parts = [{"message": {"role": "assistant", "content": "", "tool_calls": [call]},
                          "done": False}]  # fmt: skip
            else:
                groups = {item["value"]: item["count"] for item in results[0]["groups"]}
                parts = [
                    {"message": {"role": "assistant", "content": "В работе "}, "done": False},
                    {"message": {"role": "assistant",
                                 "content": f"{groups['В работе']} [{results[0]['ref']}]."},
                     "done": False},
                ]  # fmt: skip
            parts.append({"message": {"role": "assistant", "content": ""}, "done": True})
            for part in parts:
                yield (json.dumps(part, ensure_ascii=False) + "\n").encode()

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    return Starlette(routes=[Route("/api/tags", tags), Route("/api/chat", chat, methods=["POST"])])


@pytest.fixture
def stack(tmp_path):
    ports = {name: _port() for name in ("server", "ai", "ollama")}
    url = {name: f"http://127.0.0.1:{port}" for name, port in ports.items()}
    server_app = create_app(tmp_path / "e2e.sqlite3", ai_url=url["ai"])
    build_plant(Client(server_app))  # данные — в ту же базу, до запуска
    key = ai_link.load_key(tmp_path)
    seen: list[dict[str, Any]] = []
    model = Ollama("test-model", url["ollama"])
    ai_app = service.create_app(server_url=url["server"], key=key, model=model)
    running = [
        Running(fake_ollama(seen), ports["ollama"]),
        Running(server_app, ports["server"]),
        Running(ai_app, ports["ai"]),
    ]
    yield {"url": url, "key": key, "seen": seen}
    for item in reversed(running):
        item.stop()


class Browser:
    """Рабочее место: только сервер, сессия в cookie."""

    def __init__(self, base: str) -> None:
        self.base = base + "/api/v1"
        reply = self._open("POST", "/auth/login", {"login": "admin", "password": ADMIN_PASSWORD})
        cookie = reply.headers["set-cookie"].split(";", 1)[0]
        self.cookie = cookie
        reply.read()

    def _open(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ):
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        return urllib.request.urlopen(request, timeout=30)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Cookie": self.cookie, "Idempotency-Key": str(time.monotonic_ns())}
        with self._open(method, path, body, headers) as reply:
            raw = reply.read()
        return json.loads(raw) if raw else None

    def ask(self, dialog: str, text: str) -> list[dict[str, Any]]:
        with self._open(
            "POST", f"/ai/dialogs/{dialog}/messages", {"text": text}, {"Cookie": self.cookie}
        ) as reply:
            assert reply.headers["content-type"].startswith("application/x-ndjson")
            return [json.loads(line) for line in reply if line.strip()]


def test_question_travels_through_real_http(stack):
    browser = Browser(stack["url"]["server"])
    status = browser.call("GET", "/ai/status")
    assert status["service"] is True
    assert status["model"] == {"name": "test-model", "available": True, "remote": False}
    dialog = browser.call("POST", "/ai/dialogs", {})["id"]

    events = browser.ask(dialog, "куда заведён 10LAC10AP001XB01")
    answer = events[-1]["message"]
    assert answer["path"] == "fast" and "Клеммы: 12, 13" in answer["text"]
    assert answer["took_ms"] < 300 and stack["seen"] == []

    events = browser.ask(dialog, "Как распределены насосы по состоянию и почему?")
    kinds = [item["type"] for item in events]
    assert kinds[0] == "question" and kinds[-1] == "message" and "tool" in kinds
    answer = events[-1]["message"]
    assert answer["path"] == "model" and answer["text"] == "В работе 2 [1]."
    assert answer["sources"][0]["kind"] == "dataset"
    first = stack["seen"][0]
    assert first["model"] == "test-model" and first["options"] == {"temperature": 0}
    assert "aggregate" in [item["function"]["name"] for item in first["tools"]]
    second = stack["seen"][1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "aggregate"
    assert second[-1]["role"] == "tool" and second[-1]["tool_name"] == "aggregate"
    stored = browser.call("GET", f"/ai/dialogs/{dialog}")["messages"]
    assert [item["path"] for item in stored if item["role"] == "assistant"] == ["fast", "model"]


def test_ai_service_accepts_only_the_server_key(stack):
    base = stack["url"]["ai"]
    for headers in ({}, {"X-LocalAI-Key": "чужой"}):
        request = urllib.request.Request(
            base + "/ask", data=b'{"token": "ai.x", "question": "?"}', method="POST",
            headers={key: value.encode().decode("latin-1") for key, value in headers.items()},
        )  # fmt: skip
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
        assert refused.value.code == 403
    request = urllib.request.Request(base + "/health", headers={"X-LocalAI-Key": stack["key"]})
    with urllib.request.urlopen(request, timeout=5) as reply:
        assert json.loads(reply.read())["model"]["name"] == "test-model"
    bad = urllib.request.Request(
        base + "/ask", data=json.dumps({"token": "ai.чужой", "question": "?"}).encode(),
        method="POST", headers={"X-LocalAI-Key": stack["key"]},
    )  # fmt: skip
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(bad, timeout=5)
    assert refused.value.code == 400
    forged = urllib.request.Request(
        base + "/ask", data=json.dumps({"token": "ai.forged", "question": "10LAC10AP001"})
        .encode(), method="POST", headers={"X-LocalAI-Key": stack["key"]},
    )  # fmt: skip
    with urllib.request.urlopen(forged, timeout=10) as reply:
        events = [json.loads(line) for line in reply if line.strip()]
    assert events[-1]["status"] == "error"  # без выданного сервером доступа данных нет
    assert "схему" in events[0]["delta"]


def test_service_rejects_remote_clients():
    """SEC-05: с ключом, но не с этого компьютера — отказ."""
    app = service.create_app(server_url="http://127.0.0.1:1", key="k", model=Ollama("m"))
    assert _get(app, "/health", "127.0.0.1") == 200
    assert _get(app, "/health", "10.0.0.5") == 403


def _get(app: Any, path: str, host: str) -> int:
    status = {}

    async def call() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [(b"x-localai-key", b"k")], "client": (host, 50000),
            "server": ("127.0.0.1", 8731),
        }  # fmt: skip
        await app(scope, receive, send)

    anyio.run(call)
    return status["code"]
