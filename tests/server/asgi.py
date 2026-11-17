"""Небольшой клиент ASGI для тестов: запрос в приложение без сети и без httpx."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import anyio


@dataclass
class Reply:
    status: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


class Client:
    def __init__(self, app: Any) -> None:
        self.app = app

    def get(self, path: str, **query: Any) -> Reply:
        return self.request("GET", path, query=query)

    def post(self, path: str, body: Any = None, *, key: str | None = None, raw: bytes | None = None) -> Reply:
        return self.request("POST", path, body, key=key, raw=raw)

    def patch(self, path: str, body: Any = None, *, key: str | None = None) -> Reply:
        return self.request("PATCH", path, body, key=key)

    def delete(self, path: str) -> Reply:
        return self.request("DELETE", path)

    def request(self, method: str, path: str, body: Any = None, *, query: dict[str, Any] | None = None,
                key: str | None = None, raw: bytes | None = None) -> Reply:  # fmt: skip
        payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
        if key is not None:
            headers.append((b"idempotency-key", key.encode()))
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": "http", "path": "/api/v1" + path, "raw_path": ("/api/v1" + path).encode(),
            "query_string": urlencode(query or {}).encode(), "root_path": "", "headers": headers,
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "state": {},
        }  # fmt: skip
        result = {"status": 0, "body": b""}

        async def call() -> None:
            sent = False

            async def receive() -> dict[str, Any]:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    result["status"] = message["status"]
                elif message["type"] == "http.response.body":
                    result["body"] += message.get("body", b"")

            try:
                await self.app(scope, receive, send)
            except Exception:  # Starlette пробрасывает ошибку дальше после ответа 500
                if not result["status"]:
                    raise

        anyio.run(call)
        return Reply(result["status"], result["body"])
