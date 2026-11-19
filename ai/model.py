"""Адаптер модели (AI-01): чат с инструментами за одним интерфейсом.

Модель задаётся настройкой, а не кодом:

- **поставщик** — ``ollama`` (по умолчанию) или ``openai`` — любой сервер с
  OpenAI-совместимым ``/v1/chat/completions``: llama.cpp, vLLM, LM Studio;
- **имя** — конкретная модель или пусто: тогда служба сама берёт самую
  крупную установленную модель, умеющую вызывать инструменты;
- **размышление** (``think``) — выключено, включено или уровень
  ``low``/``medium``/``high`` у моделей, которые это умеют;
- **окно контекста** — сколько токенов модель держит в голове. У Ollama оно
  по умолчанию маленькое, и схема реестров с результатами инструментов в него
  не помещается: модель «глупеет». Здесь по умолчанию 32 768.

Администратор меняет модель в настройках ИИ без перезапуска службы; есть
основная и запасная модель. Модель на другом компьютере считается облачной
и работает, только если администратор разрешил облачную модель (AI-15).

Модель в установщик не входит (REL-01): если её нет или она не запущена,
служба работает без неё (AI-12).
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT = "ollama"  # Ollama, модель выбирается сама
CHECK_EVERY = 10.0  # секунд кэшируется список установленных моделей
LOOPBACK = ("127.0.0.1", "::1", "localhost")
PROVIDERS = ("ollama", "openai", "none")
THINK = ("auto", "off", "on", "low", "medium", "high")
CONTEXT = (2048, 262144)


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass(frozen=True)
class Chunk:
    """Кусок ответа модели: текст, вызовы инструментов или признак «размышляет»."""

    text: str = ""
    calls: tuple[ToolCall, ...] = field(default=())
    thinking: bool = False


class ModelError(Exception):
    """Модель не ответила или ответила не по протоколу."""


@dataclass(frozen=True)
class Config:
    """Настройка модели: из настроек ИИ на сервере или из командной строки службы."""

    provider: str = "ollama"
    url: str = OLLAMA_URL
    name: str = ""
    think: str = "auto"
    context: int = 32768
    temperature: float = 0.2
    api_key: str = ""

    @classmethod
    def parse(cls, raw: Any) -> Config:
        if raw is None:
            return cls(provider="none")
        if not isinstance(raw, dict):
            raise TypeError("Настройка модели — объект")
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Неизвестный параметр модели «{min(unknown)}»")
        config = cls(**{key: value for key, value in raw.items() if value is not None})
        if config.provider not in PROVIDERS:
            raise ValueError("Поставщик модели: ollama, openai или none")
        if config.think not in THINK:
            raise ValueError("Размышление: auto, off, on, low, medium, high")
        if not isinstance(config.context, int) or not CONTEXT[0] <= config.context <= CONTEXT[1]:
            raise ValueError(f"Окно контекста — от {CONTEXT[0]} до {CONTEXT[1]} токенов")
        if not isinstance(config.temperature, int | float) or not 0 <= config.temperature <= 2:
            raise ValueError("Температура — от 0 до 2")
        parts = urllib.parse.urlsplit(config.url)
        if config.provider != "none" and (
            parts.scheme not in ("http", "https") or not parts.hostname
        ):
            raise ValueError("Адрес модели — http(s)://хост:порт")
        return config

    @property
    def remote(self) -> bool:
        return urllib.parse.urlsplit(self.url).hostname not in LOOPBACK

    def key(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class Model:
    """Модель без реализации: «модели нет»."""

    provider = "none"
    configured = ""
    remote = False

    @property
    def name(self) -> str:
        return "none"

    def available(self) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "name": self.name,
            "configured": self.configured or "auto",
            "available": self.available(),
            "remote": self.remote,
        }

    def installed(self) -> list[dict[str, Any]]:
        return []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Iterator[Chunk]:
        raise ModelError("Модель не подключена")
        yield Chunk()  # pragma: no cover — генератор


class _Http(Model):
    """Общее для моделей по HTTP: запросы, кэш списка моделей, выбор модели «сам»."""

    def __init__(self, config: Config, *, timeout: float = 180.0) -> None:
        self.config = config
        self.provider = config.provider
        self.configured = config.name
        self.url = config.url.rstrip("/")
        self.remote = config.remote
        self.timeout = timeout
        self._cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._resolve() or self.configured or "auto"

    def available(self) -> bool:
        return self._resolve() is not None

    def installed(self) -> list[dict[str, Any]]:
        with self._lock:
            at, items = self._cache
            if time.monotonic() - at < CHECK_EVERY and at:
                return items
            try:
                items = self._list()
            except (OSError, ValueError, KeyError, TypeError, ModelError):
                items = []
            self._cache = (time.monotonic(), items)
            return items

    def _resolve(self) -> str | None:
        items = self.installed()
        names = {item["name"] for item in items}
        if self.configured:
            if self.configured in names:
                return self.configured
            if f"{self.configured}:latest" in names:
                return f"{self.configured}:latest"
            return None
        return best(items)

    def _list(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> Any:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            headers=self._headers(),
            method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            detail = error.read()[:300].decode("utf-8", "replace")
            self._cache = (0.0, [])
            raise ModelError(f"{error.code}: {detail}") from None
        except OSError as error:
            self._cache = (0.0, [])
            raise ModelError(str(error)) from None

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(self.url + path, headers=self._headers())
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return json.loads(response.read())

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers


class Ollama(_Http):
    """Ollama: ``/api/chat`` с инструментами, размышлением и потоковым ответом."""

    def __init__(self, config: Config | str, url: str = OLLAMA_URL, **extra: Any) -> None:
        if isinstance(config, str):  # Ollama("имя", "адрес") — коротко
            config = Config(provider="ollama", url=url, name=config)
        super().__init__(config, **extra)
        self._shown: dict[str, dict[str, Any]] = {}

    def _list(self) -> list[dict[str, Any]]:
        items = []
        for item in self._get("/api/tags").get("models", []):
            name = item.get("name") or item.get("model")
            details = item.get("details") or {}
            shown = self._show(name, item.get("digest", ""))
            items.append(
                {
                    "name": name,
                    "size_gb": round((item.get("size") or 0) / 1e9, 1),
                    "parameters": details.get("parameter_size"),
                    "quantization": details.get("quantization_level"),
                    "family": details.get("family"),
                    "capabilities": shown["capabilities"],
                    "context": shown["context"],
                }
            )
        return items

    def _show(self, name: str, digest: str) -> dict[str, Any]:
        """Что модель умеет — из ``/api/show``; ответ кэшируется по отпечатку модели."""
        key = f"{name}@{digest}"
        if key not in self._shown:
            try:
                with self._post("/api/show", {"model": name}, 5.0) as response:
                    shown = json.loads(response.read())
            except (ModelError, ValueError):  # старый Ollama: умения неизвестны
                return {"capabilities": [], "context": None}
            info = shown.get("model_info") or {}
            context = next(
                (value for item, value in info.items() if item.endswith(".context_length")), None
            )
            self._shown[key] = {
                "capabilities": list(shown.get("capabilities") or []),
                "context": context,
            }
        return self._shown[key]

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Iterator[Chunk]:
        name = self._resolve()
        if name is None:
            raise ModelError(f"Модель «{self.name}» не установлена в Ollama")
        body: dict[str, Any] = {
            "model": name,
            "messages": [_ollama_message(item) for item in messages],
            "tools": tools,
            "stream": True,
            "options": {"temperature": self.config.temperature, "num_ctx": self.config.context},
        }
        think = _think(self.config.think)
        if think is not None:
            body["think"] = think
        with self._post("/api/chat", body, self.timeout) as response:
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
                        str(item.get("id") or ""),
                    )
                    for item in message.get("tool_calls") or []
                )
                if message.get("content") or calls:
                    yield Chunk(message.get("content") or "", calls)
                elif message.get("thinking"):
                    yield Chunk(thinking=True)


class OpenAICompatible(_Http):
    """Сервер с OpenAI-совместимым API: ``/v1/models`` и ``/v1/chat/completions``."""

    def __init__(self, config: Config, **extra: Any) -> None:
        super().__init__(config, **extra)
        if not self.url.endswith("/v1"):
            self.url += "/v1"

    def _list(self) -> list[dict[str, Any]]:
        return [
            {"name": item["id"], "size_gb": None, "parameters": None, "quantization": None,
             "family": item.get("owned_by"), "capabilities": [], "context": None}
            for item in self._get("/models").get("data", [])
        ]  # fmt: skip

    def _resolve(self) -> str | None:
        items = self.installed()
        if self.configured:
            return self.configured if any(i["name"] == self.configured for i in items) else None
        return items[0]["name"] if items else None  # сервер обычно держит одну модель

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Iterator[Chunk]:
        name = self._resolve()
        if name is None:
            raise ModelError(f"Модель «{self.name}» недоступна на {self.url}")
        body: dict[str, Any] = {
            "model": name,
            "messages": [_openai_message(item) for item in messages],
            "tools": tools,
            "stream": True,
            "temperature": self.config.temperature,
        }
        if self.config.think in ("low", "medium", "high"):
            body["reasoning_effort"] = self.config.think
        pending: dict[int, dict[str, Any]] = {}
        with self._post("/chat/completions", body, self.timeout) as response:
            for line in response:
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                data = text[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    part = json.loads(data)
                except ValueError:
                    raise ModelError("Ответ модели не разобран") from None
                if part.get("error"):
                    raise ModelError(str(part["error"]))
                for choice in part.get("choices") or []:
                    delta = choice.get("delta") or {}
                    for item in delta.get("tool_calls") or []:
                        slot = pending.setdefault(int(item.get("index", 0)),
                                                  {"id": "", "name": "", "args": ""})  # fmt: skip
                        slot["id"] = item.get("id") or slot["id"]
                        function = item.get("function") or {}
                        slot["name"] += function.get("name") or ""
                        slot["args"] += function.get("arguments") or ""
                    if delta.get("content"):
                        yield Chunk(delta["content"])
                    elif delta.get("reasoning_content") or delta.get("reasoning"):
                        yield Chunk(thinking=True)
        if pending:
            yield Chunk(calls=tuple(ToolCall(slot["name"], _arguments(slot["args"]), slot["id"])
                                    for _, slot in sorted(pending.items())))  # fmt: skip


# Выбор модели «сам» ---------------------------------------------------------------


def best(items: list[dict[str, Any]]) -> str | None:
    """Самая крупная установленная модель, которая умеет вызывать инструменты."""
    fit = [item for item in items if "tools" in item.get("capabilities", [])]
    if not fit and items and not any(item.get("capabilities") for item in items):
        fit = items  # Ollama не сообщает умений — берём самую крупную
    if not fit:
        return None
    return max(fit, key=lambda item: (_billions(item.get("parameters")), item["name"]))["name"]


def _billions(value: Any) -> float:
    match = re.match(r"([\d.]+)\s*([BMK]?)", str(value or ""), re.IGNORECASE)
    if not match:
        return 0.0
    number = float(match.group(1))
    return number * {"B": 1.0, "M": 1e-3, "K": 1e-6}.get(match.group(2).upper(), 1.0)


# Сборка модели по настройке ---------------------------------------------------------


_BUILT: dict[str, Model] = {}
_BUILT_LOCK = threading.Lock()


def build(config: Config) -> Model:
    """Модель по настройке; одна и та же настройка — один объект (кэш списка моделей)."""
    with _BUILT_LOCK:
        key = config.key()
        if key not in _BUILT:
            if config.provider == "ollama":
                _BUILT[key] = Ollama(config)
            elif config.provider == "openai":
                _BUILT[key] = OpenAICompatible(config)
            else:
                _BUILT[key] = Model()
        return _BUILT[key]


def make(spec: str, ollama_url: str = OLLAMA_URL, openai_url: str | None = None) -> Model:
    """Модель из командной строки: ``none``, ``ollama``, ``ollama:<имя>``, ``openai[:<имя>]``."""
    if spec in ("", "none"):
        return Model()
    kind, _, name = spec.partition(":")
    if kind == "ollama":
        return build(Config(provider="ollama", url=ollama_url, name=name))
    if kind == "openai":
        return build(
            Config(provider="openai", url=openai_url or "http://127.0.0.1:8080", name=name)
        )
    raise ValueError(f"Модель «{spec}»: ожидается none, ollama[:имя] или openai[:имя]")


# Сообщения ----------------------------------------------------------------------------


def _think(value: str) -> bool | str | None:
    return {"auto": None, "off": False, "on": True}.get(value, value)


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else {}
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


def _openai_message(item: dict[str, Any]) -> dict[str, Any]:
    shown: dict[str, Any] = {"role": item["role"], "content": item.get("content", "")}
    if item.get("tool_calls"):
        shown["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
            }
            for call in item["tool_calls"]
        ]
    if item["role"] == "tool":
        shown["tool_call_id"] = item.get("id", "")
    return shown
