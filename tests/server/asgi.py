"""Небольшой клиент ASGI для тестов: запрос в приложение без сети и без httpx."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import anyio

from engine import SYSTEM, access

ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "пароль-администратора"


@dataclass
class Reply:
    status: int
    body: bytes
    cookies: dict[str, str] | None = None
    headers: list[tuple[str, str]] | None = None

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


class Client:
    """Клиент от имени пользователя. По умолчанию — администратор: его создаёт
    сам клиент, как это сделал бы установщик (``python -m server.admin``)."""

    def __init__(self, app: Any, *, login: str | None = ADMIN_LOGIN, password: str = "") -> None:
        self.app = app
        self.cookie: str | None = None
        if login == ADMIN_LOGIN and not password:
            ensure_admin(app.state.db)
            password = ADMIN_PASSWORD
        if login is not None:
            reply = self.post("/auth/login", {"login": login, "password": password})
            assert reply.status == 200, reply.json()

    def put(self, path: str, body: Any = None, *, key: str | None = None) -> Reply:
        return self.request("PUT", path, body, key=key)

    def get(self, path: str, **query: Any) -> Reply:
        return self.request("GET", path, query=query)

    def post(
        self, path: str, body: Any = None, *, key: str | None = None, raw: bytes | None = None
    ) -> Reply:
        return self.request("POST", path, body, key=key, raw=raw)

    def patch(self, path: str, body: Any = None, *, key: str | None = None) -> Reply:
        return self.request("PATCH", path, body, key=key)

    def delete(self, path: str) -> Reply:
        return self.request("DELETE", path)

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: dict[str, Any] | None = None,
        key: str | None = None,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        extra = headers or {}
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            *((name.encode(), value.encode()) for name, value in extra.items()),
        ]
        if key is None and method in ("POST", "PATCH", "PUT", "DELETE"):
            key = str(uuid.uuid4())  # каждый изменяющий запрос несёт свой ключ (API-06)
        if key:  # пустая строка — запрос без ключа
            headers.append((b"idempotency-key", key.encode()))
        if self.cookie:
            headers.append((b"cookie", f"localai_session={self.cookie}".encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/api/v1" + path,
            "raw_path": ("/api/v1" + path).encode(),
            "query_string": urlencode(query or {}).encode(),
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "state": {},
        }
        result: dict[str, Any] = {"status": 0, "body": b"", "cookies": {}, "headers": []}

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
                    for name, value in message.get("headers", []):
                        result["headers"].append((name.decode(), value.decode()))
                        if name.lower() == b"set-cookie":
                            pair = value.decode().split(";", 1)[0]
                            cookie, _, token = pair.partition("=")
                            result["cookies"][cookie] = token.strip('"')
                elif message["type"] == "http.response.body":
                    result["body"] += message.get("body", b"")

            try:
                await self.app(scope, receive, send)
            except Exception:  # Starlette пробрасывает ошибку дальше после ответа 500
                if not result["status"]:
                    raise

        anyio.run(call)
        if "localai_session" in result["cookies"]:
            self.cookie = result["cookies"]["localai_session"] or None
        return Reply(result["status"], result["body"], result["cookies"], result["headers"])


def ensure_admin(db: Any) -> None:
    with db.read() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE login_norm = ?", (ADMIN_LOGIN,)).fetchone()
    if not exists:
        with db.write(SYSTEM) as tx:
            access.create_admin(tx, ADMIN_LOGIN, "Тестовый администратор", ADMIN_PASSWORD)
