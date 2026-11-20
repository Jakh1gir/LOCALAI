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
import urllib.parse
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
from ai.model import Config, Ollama, build
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

    installed = {
        "small-model:latest": ("3.2B", ["completion", "tools"]),
        "test-model:latest": ("27.4B", ["completion", "tools", "thinking"]),
        "embed:latest": ("335M", ["embedding"]),
    }

    async def embed(request: Request) -> JSONResponse:
        body = await request.json()
        seen.append(body)
        return JSONResponse({"embeddings": [[float(len(text))] + [0.5] * 1023
                                            for text in body["input"]]})  # fmt: skip

    async def tags(_request: Request) -> JSONResponse:
        return JSONResponse({"models": [
            {"name": name, "digest": name, "size": 2_000_000_000,
             "details": {"parameter_size": size, "family": "test", "quantization_level": "Q4_K_M"}}
            for name, (size, _) in installed.items()
        ]})  # fmt: skip

    async def show(request: Request) -> JSONResponse:
        name = (await request.json())["model"]
        return JSONResponse({"capabilities": installed[name][1],
                             "model_info": {"test.context_length": 262144}})  # fmt: skip

    async def chat(request: Request) -> StreamingResponse:
        body = await request.json()
        seen.append(body)
        results = [
            json.loads(item["content"]) for item in body["messages"] if item["role"] == "tool"
        ]

        def lines() -> Iterator[bytes]:
            if body.get("think"):
                yield b'{"message": {"role": "assistant", "content": "", "thinking": "..."}}\n'
            if [item["function"]["name"] for item in body["tools"]] == ["kks_card"]:  # проверка
                yield from _probe_answer(results)
                return
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

    return Starlette(
        routes=[
            Route("/api/tags", tags),
            Route("/api/show", show, methods=["POST"]),
            Route("/api/embed", embed, methods=["POST"]),
            Route("/api/chat", chat, methods=["POST"]),
        ]
    )


def _probe_answer(results: list[dict[str, Any]]) -> Iterator[bytes]:
    if not results:
        call = {"function": {"name": "kks_card", "arguments": {"code": "10LAC10AP001"}}}
        message = {"role": "assistant", "content": "", "tool_calls": [call]}
    else:
        message = {
            "role": "assistant",
            "content": "Это питательный электронасос 1 [1], давление 16,4 кгс/см² [2].",
        }
    yield (json.dumps({"message": message, "done": True}, ensure_ascii=False) + "\n").encode()


@pytest.fixture
def stack(tmp_path):
    ports = {name: _port() for name in ("server", "ai", "ollama")}
    url = {name: f"http://127.0.0.1:{port}" for name, port in ports.items()}
    server_app = create_app(tmp_path / "e2e.sqlite3", ai_url=url["ai"])
    build_plant(Client(server_app))  # данные — в ту же базу, до запуска
    key = ai_link.load_key(tmp_path)
    seen: list[dict[str, Any]] = []
    model = build(Config(url=url["ollama"]))  # модель выбирается сама
    ai_app = service.create_app(server_url=url["server"], key=key, model=model)
    running = [
        Running(fake_ollama(seen), ports["ollama"]),
        Running(server_app, ports["server"]),
        Running(ai_app, ports["ai"]),
    ]
    yield {"url": url, "key": key, "seen": seen, "app": server_app}
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
    assert status["model"] == {"provider": "ollama", "name": "test-model:latest",
                               "configured": "auto", "available": True, "remote": False}  # fmt: skip
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
    assert answer["model"] == "test-model:latest"  # самая крупная модель с инструментами
    assert answer["sources"][0]["kind"] == "dataset"
    first = stack["seen"][0]
    assert first["model"] == "test-model:latest"
    assert first["options"] == {"temperature": 0.2, "num_ctx": 32768} and "think" not in first
    assert "aggregate" in [item["function"]["name"] for item in first["tools"]]
    second = stack["seen"][1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "aggregate"
    assert second[-1]["role"] == "tool" and second[-1]["tool_name"] == "aggregate"
    stored = browser.call("GET", f"/ai/dialogs/{dialog}")["messages"]
    assert [item["path"] for item in stored if item["role"] == "assistant"] == ["fast", "model"]


def test_admin_lists_checks_and_switches_models_without_restart(stack):
    browser = Browser(stack["url"]["server"])
    ollama = stack["url"]["ollama"]
    listed = browser.call("GET", f"/ai/models?url={urllib.parse.quote(ollama)}")
    assert listed["auto"] == "test-model:latest"
    shown = {item["name"]: item for item in listed["items"]}
    assert shown["small-model:latest"]["capabilities"] == ["completion", "tools"]
    assert shown["test-model:latest"]["parameters"] == "27.4B"
    assert shown["test-model:latest"]["context"] == 262144

    checked = browser.call("POST", "/ai/models/check",
                           {"config": {"url": ollama, "name": "small-model"}})  # fmt: skip
    assert checked["score"] == 4 and all(checked["checks"].values()), checked
    assert checked["model"] == "small-model:latest" and checked["error"] is None

    saved = browser.call("PUT", "/ai/settings", {
        "model": {"url": ollama, "name": "small-model", "think": "on", "context": 16384},
        "fallback": {"url": ollama, "name": "test-model"},
    })  # fmt: skip
    assert saved["model"]["name"] == "small-model" and saved["model"]["api_key_set"] is False
    status = browser.call("GET", "/ai/status")
    assert status["model"]["name"] == "small-model:latest"
    assert status["fallback"]["name"] == "test-model:latest"
    dialog = browser.call("POST", "/ai/dialogs", {})["id"]
    events = browser.ask(dialog, "Как распределены насосы по состоянию и почему?")
    assert "thinking" in [item["type"] for item in events]
    assert events[-1]["message"]["model"] == "small-model:latest"
    body = stack["seen"][-1]
    assert body["model"] == "small-model:latest" and body["think"] is True
    assert body["options"]["num_ctx"] == 16384

    browser.call("PUT", "/ai/settings",
                 {"model": {"url": ollama, "name": "нет-такой"}})  # fmt: skip
    status = browser.call("GET", "/ai/status")
    assert status["model"]["available"] is False and status["fallback"]["available"] is True
    events = browser.ask(dialog, "Как распределены насосы по состоянию и почему?")
    assert events[-1]["message"]["model"] == "test-model:latest"  # ответила запасная

    with pytest.raises(urllib.error.HTTPError) as refused:
        browser.call("PUT", "/ai/settings", {"model": {"url": ollama, "context": 10}})
    assert refused.value.code == 400


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
        assert json.loads(reply.read())["model"]["name"] == "test-model:latest"
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


def fake_openai(seen: list[dict[str, Any]]) -> Starlette:
    """OpenAI-совместимый сервер (llama.cpp, vLLM): аргументы вызова приходят кусками."""

    async def listed(_request: Request) -> JSONResponse:
        return JSONResponse({"data": [{"id": "local-gguf", "owned_by": "llamacpp"}]})

    async def chat(request: Request) -> StreamingResponse:
        body = await request.json()
        seen.append(body)
        answered = any(item["role"] == "tool" for item in body["messages"])

        def lines() -> Iterator[bytes]:
            if answered:
                deltas = [{"reasoning_content": "…"}, {"content": "Питательный электронасос 1 [1], "},
                          {"content": "давление 16,4 кгс/см² [2]."}]  # fmt: skip
            else:
                deltas = [
                    {"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                     "function": {"name": "kks_card", "arguments": '{"code": '}}]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": '"10LAC10AP001"}'}}]},
                ]  # fmt: skip
            for delta in deltas:
                part = {"choices": [{"index": 0, "delta": delta}]}
                yield f"data: {json.dumps(part, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(lines(), media_type="text/event-stream")

    return Starlette(
        routes=[
            Route("/v1/models", listed),
            Route("/v1/chat/completions", chat, methods=["POST"]),
        ]
    )


def test_openai_compatible_server_works_as_model():
    from ai import probe

    seen: list[dict[str, Any]] = []
    port = _port()
    running = Running(fake_openai(seen), port)
    try:
        model = build(Config(provider="openai", url=f"http://127.0.0.1:{port}", think="high"))
        assert model.status() == {"provider": "openai", "name": "local-gguf", "configured": "auto",
                                  "available": True, "remote": False}  # fmt: skip
        result = probe.run(model)
    finally:
        running.stop()
    assert result["score"] == 4, result
    assert seen[0]["reasoning_effort"] == "high" and seen[0]["model"] == "local-gguf"
    assistant, tool_reply = seen[1]["messages"][-2:]
    assert assistant["tool_calls"][0] == {
        "id": "call_a", "type": "function",
        "function": {"name": "kks_card", "arguments": '{"code": "10LAC10AP001"}'},
    }  # fmt: skip
    assert tool_reply == {
        "role": "tool",
        "content": tool_reply["content"],
        "tool_call_id": "call_a",
    }


def test_model_config_is_checked():
    for bad in ({"provider": "gpt"}, {"think": "очень"}, {"context": 100}, {"url": "ftp://x"},
                {"temperature": 5}, {"секрет": 1}):  # fmt: skip
        with pytest.raises(ValueError):
            Config.parse(bad)
    assert Config.parse({"url": "http://10.1.2.3:11434"}).remote is True
    assert Config.parse(None).provider == "none"


def test_ai_service_embeds_with_ollama_on_cpu(stack):
    link = ai_link.AiLink(stack["url"]["ai"], stack["key"])
    config = {"url": stack["url"]["ollama"], "name": "embed", "gpu": False}
    probe = link.embed([], config)
    assert probe == {"model": "embed:latest", "dim": None, "vectors": []}
    found = link.embed(["напор", "давление насоса"], config)
    assert found["model"] == "embed:latest" and found["dim"] == 1024
    assert [vector[0] for vector in found["vectors"]] == [5.0, 15.0]
    sent = stack["seen"][-1]
    assert sent == {"model": "embed:latest", "input": ["напор", "давление насоса"],
                    "truncate": True, "options": {"num_gpu": 0}}  # fmt: skip
    missing = link.embed(["x"], {"url": stack["url"]["ollama"], "name": "нет-модели"})
    assert missing == {"model": None, "dim": None, "vectors": []}
    status = Browser(stack["url"]["server"]).call("GET", "/ai/status")
    assert status["embedding"]["name"] == "qwen3-embedding:0.6b"  # по умолчанию, не скачана
    assert status["embedding"]["available"] is False
    assert status["vectors"] == {"model": None, "done": 0, "total": 0}
