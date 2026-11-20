"""Обращения ИИ-службы к API сервера от имени пользователя (ARCH-02, API-10).

Служба не открывает файл базы: всё, что она знает, она читает через API с
делегированным токеном, который сервер выдал на один ответ. Сервер отвечает
ровно тем, что разрешено спросившему, и отклоняет изменения (AI-09).

``Api`` считает обращения: за один ответ — не больше ``max_calls`` (AI-10).
Транспорт сменный: HTTP в работе, вызов приложения в памяти — в тестах.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PREFIX = "/api/v1"
TIMEOUT = 15.0


@dataclass(frozen=True)
class Reply:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def error(self) -> str | None:
        if self.ok:
            return None
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), dict):
            return str(self.body["error"].get("code"))
        return f"http_{self.status}"


Transport = Callable[[str, str, dict[str, Any], Any, dict[str, str]], Reply]


class Limit(Exception):
    """Предел обращений к серверу за один ответ исчерпан (AI-10)."""


class HttpTransport:
    """HTTP(S) к серверу на этом компьютере. ``cafile`` — сертификат сервера в сетевом режиме."""

    def __init__(self, base_url: str, *, cafile: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.context = None
        if cafile:
            # Сертификат сервера — самоподписанный и выдан на сетевое имя, а служба ходит
            # на 127.0.0.1: проверяется сам сертификат (он единственный доверенный), не имя.
            self.context = ssl.create_default_context(cafile=cafile)
            self.context.check_hostname = False

    def __call__(
        self, method: str, path: str, query: dict[str, Any], body: Any, headers: dict[str, str]
    ) -> Reply:
        url = self.base_url + PREFIX + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json", **headers}
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT, context=self.context) as response:
                raw = response.read()
                return Reply(response.status, json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = None
            return Reply(error.code, parsed)
        except (OSError, ValueError) as error:  # сервер недоступен или ответил не JSON
            return Reply(503, {"error": {"code": "server_unavailable", "message": str(error)}})


class Api:
    """Клиент API с делегированным токеном и счётчиком обращений."""

    def __init__(self, transport: Transport, token: str, *, max_calls: int = 24) -> None:
        self.transport = transport
        self.token = token
        self.max_calls = max_calls
        self.calls = 0

    @property
    def left(self) -> int:
        return self.max_calls - self.calls

    def get(self, path: str, **query: Any) -> Reply:
        return self.request("GET", path, query={k: v for k, v in query.items() if v is not None})

    def post(self, path: str, body: Any = None) -> Reply:
        return self.request("POST", path, body=body if body is not None else {})

    def request(
        self, method: str, path: str, *, query: dict[str, Any] | None = None, body: Any = None
    ) -> Reply:
        if self.calls >= self.max_calls:
            raise Limit(self.max_calls)
        self.calls += 1
        headers = {"Authorization": f"Bearer {self.token}"}
        if method != "GET":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        return self.transport(method, path, query or {}, body, headers)
