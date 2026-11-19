"""Связь сервера с ИИ-службой (ARCH-02, API-10, SEC-05).

Интерфейс разговаривает только с сервером. Сервер передаёт вопрос ИИ-службе
и выдаёт ей **делегированный доступ** — короткоживущий токен от имени
задавшего вопрос пользователя. С ним служба читает данные через тот же API
и получает ровно то, что разрешено этому пользователю. Делегированный
доступ:

- только читает (AI-09): изменяющий маршрут отвечает 403, кроме создания
  черновика изменения, который потом принимает человек;
- ограничен числом обращений (AI-10) и временем одного ответа, а по его
  окончании отзывается;
- живёт в памяти процесса сервера: в базу не пишется и копией не уносится.

ИИ-служба слушает только этот компьютер и принимает вопросы только с ключом
сервера (SEC-05). Ключ — случайная строка в файле ``ai.key`` рядом с базой;
служба получает путь к нему при запуске.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.errors import TOO_MANY, UNAUTHORIZED, EngineError

PREFIX = "ai."  # делегированный токен отличим от токена сессии по началу
LIFETIME = 300.0  # секунд на один ответ
MAX_CALLS = 24  # обращений к серверу за один ответ (AI-10)
DEFAULT_URL = "http://127.0.0.1:8731"
KEY_FILE = "ai.key"
HEADER = "X-LocalAI-Key"
CONNECT_TIMEOUT = 2.0
ANSWER_TIMEOUT = 180.0


@dataclass
class Grant:
    user_id: str
    dialog_id: str
    message_id: str
    expires: float
    calls: int = 0


class Grants:
    """Делегированные доступы ИИ, по хэшу токена."""

    def __init__(self, lifetime: float = LIFETIME, max_calls: int = MAX_CALLS) -> None:
        self.lifetime = lifetime
        self.max_calls = max_calls
        self._items: dict[str, Grant] = {}
        self._lock = threading.Lock()

    def issue(self, user_id: str, dialog_id: str, message_id: str) -> str:
        token = PREFIX + secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            for key in [key for key, item in self._items.items() if item.expires <= now]:
                del self._items[key]
            self._items[_hash(token)] = Grant(user_id, dialog_id, message_id, now + self.lifetime)
        return token

    def use(self, token: str) -> Grant:
        """Каждое обращение службы ИИ считается; сверх предела — отказ (AI-10)."""
        with self._lock:
            grant = self._items.get(_hash(token))
            if grant is None or grant.expires <= time.monotonic():
                raise EngineError(
                    "ai_grant_invalid", "Доступ ИИ закрыт: ответ завершён", status=UNAUTHORIZED
                )
            if grant.calls >= self.max_calls:
                raise EngineError(
                    "ai_call_limit",
                    f"ИИ превысил предел обращений к серверу за один ответ ({self.max_calls})",
                    status=TOO_MANY,
                )
            grant.calls += 1
            return grant

    def revoke(self, token: str) -> None:
        with self._lock:
            self._items.pop(_hash(token), None)

    def __len__(self) -> int:
        return len(self._items)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_key(folder: Path) -> str:
    """Ключ сервера для ИИ-службы; создаётся при первом запуске, читать может только владелец."""
    path = folder / KEY_FILE
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_urlsafe(32))
    return path.read_text(encoding="ascii").strip()


class AiUnavailable(Exception):
    """ИИ-служба не отвечает: остальной продукт работает без неё (NFR-07)."""


class AiLink:
    """HTTP к ИИ-службе на этом компьютере. Ответ — поток строк JSON (NDJSON)."""

    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.key = key

    def health(self) -> dict[str, Any] | None:
        request = urllib.request.Request(self.url + "/health", headers={HEADER: self.key})
        try:
            with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as response:
                return json.loads(response.read())
        except (OSError, ValueError):
            return None

    def ask(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        request = urllib.request.Request(
            self.url + "/ask",
            data=json.dumps(payload).encode(),
            headers={HEADER: self.key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=ANSWER_TIMEOUT)
        except urllib.error.HTTPError as error:
            raise AiUnavailable(f"ИИ-служба ответила {error.code}") from None
        except OSError as error:
            raise AiUnavailable(str(error)) from None
        try:
            with response:
                for line in response:
                    if line.strip():
                        yield json.loads(line)
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise AiUnavailable(f"связь со службой ИИ прервалась: {error}") from None
