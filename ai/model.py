"""Адаптер модели (AI-01): чат с инструментами за одним интерфейсом.

Модель выбирается настройкой ``--model``: ``ollama:<имя>`` — локальный Ollama
(по умолчанию), ``none`` — без модели. Смена модели не меняет код службы.
Модель на другом компьютере считается облачной: её данные уходят из сети,
поэтому она работает, только если администратор разрешил облачную модель
(AI-15); иначе служба отвечает так, будто модели нет.

Модель в установщик не входит (REL-01): если её нет или она не запущена,
служба работает без неё (AI-12).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT = "ollama:qwen2.5:7b-instruct"
CHECK_EVERY = 10.0  # секунд кэшируется ответ «модель доступна»
LOOPBACK = ("127.0.0.1", "::1", "localhost")


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Chunk:
    """Кусок ответа модели: текст и/или вызовы инструментов."""

    text: str = ""
    calls: tuple[ToolCall, ...] = field(default=())


class ModelError(Exception):
    """Модель не ответила или ответила не по протоколу."""


class Model:
    """Модель без реализации: «модели нет»."""

    name = "none"
    remote = False

    def available(self) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available(), "remote": self.remote}

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Iterator[Chunk]:
        raise ModelError("Модель не подключена")
        yield Chunk()  # pragma: no cover — генератор


class Ollama(Model):
    """Ollama: ``/api/chat`` с инструментами и потоковым ответом."""

    def __init__(self, name: str, url: str = OLLAMA_URL, *, timeout: float = 120.0) -> None:
        self.name = name
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.remote = urllib.parse.urlsplit(self.url).hostname not in LOOPBACK
        self._checked = (0.0, False)

    def available(self) -> bool:
        at, ok = self._checked
        if time.monotonic() - at < CHECK_EVERY:
            return ok
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=1.0) as response:
                names = {item.get("name") for item in json.loads(response.read()).get("models", [])}
            ok = self.name in names or f"{self.name}:latest" in names
        except (OSError, ValueError):
            ok = False
        self._checked = (time.monotonic(), ok)
        return ok

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Iterator[Chunk]:
        body = {
            "model": self.name,
            "messages": [_ollama_message(item) for item in messages],
            "tools": tools,
            "stream": True,
            "options": {"temperature": 0},
        }
        request = urllib.request.Request(
            self.url + "/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except (OSError, urllib.error.HTTPError) as error:
            self._checked = (0.0, False)
            raise ModelError(str(error)) from None
        with response:
            for line in response:
                if not line.strip():
                    continue
                try:
                    part = json.loads(line)
                except ValueError:
                    raise ModelError("Ответ модели не разобран") from None
                if part.get("error"):
                    raise ModelError(str(part["error"]))
                message = part.get("message") or {}
                calls = tuple(
                    ToolCall(
                        str(item.get("function", {}).get("name", "")),
                        _arguments(item.get("function", {}).get("arguments")),
                    )
                    for item in message.get("tool_calls") or []
                )
                if message.get("content") or calls:
                    yield Chunk(message.get("content") or "", calls)


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _ollama_message(item: dict[str, Any]) -> dict[str, Any]:
    shown = {"role": item["role"], "content": item.get("content", "")}
    if item.get("tool_calls"):
        shown["tool_calls"] = [
            {"function": {"name": call["name"], "arguments": call["args"]}}
            for call in item["tool_calls"]
        ]
    if item["role"] == "tool":
        shown["tool_name"] = item.get("name", "")
    return shown


def make(spec: str, ollama_url: str = OLLAMA_URL) -> Model:
    """Модель по настройке: ``none`` или ``ollama:<имя>``."""
    if spec in ("", "none"):
        return Model()
    kind, _, name = spec.partition(":")
    if kind == "ollama" and name:
        return Ollama(name, ollama_url)
    raise ValueError(f"Модель «{spec}»: ожидается none или ollama:<имя>")
