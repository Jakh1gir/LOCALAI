"""Ollama в фоне: запуск, скачивание моделей, загрузка модели в память.

Пользователю не нужно включать Ollama самому. При запуске ИИ-службы:

1. **Запуск.** Если Ollama на этом компьютере не отвечает, служба запускает
   ``ollama serve`` в фоне — без окна — с настройками для 8 ГБ видеопамяти
   (flash attention, сжатый кэш контекста) и держит его под присмотром:
   упал — запускается снова. Уже запущенный Ollama (приложение в трее)
   используется как есть.
2. **Скачивание.** Модели из списка ``pull``, которых нет, скачиваются в
   фоне (нужен интернет; без него — ошибка в состоянии, остальное работает).
3. **Прогрев.** Основная модель загружается в память заранее и держится там
   ``keep_alive``: первый вопрос не ждёт загрузки. Если администратор сменил
   основную модель, новая прогревается при первом обращении.

Ollama на другом компьютере не запускается и не трогается: служба только
обращается к нему.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai.model import EMBED, LOOPBACK, OLLAMA_URL
from ai.process import Supervisor

KEEP_ALIVE = "30m"
RECOMMENDED = ("qwen3.5:9b", "qwen3.5:4b", EMBED)  # 8 ГБ (19.11.2026) и модель векторов
SETTINGS = {
    "OLLAMA_FLASH_ATTENTION": "1",  # меньше памяти на окно контекста
    "OLLAMA_KV_CACHE_TYPE": "q8_0",  # кэш контекста вдвое меньше — 32K помещаются в 8 ГБ
    "OLLAMA_CONTEXT_LENGTH": "32768",
    # Модель чата и модель векторов (она на процессоре) — вместе, без вытеснения друг друга.
    "OLLAMA_MAX_LOADED_MODELS": "2",
}


def find_binary(explicit: str | None = None) -> str | None:
    """Где установлен Ollama: явный путь, PATH или обычные места установки."""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    found = shutil.which("ollama")
    if found:
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe",
        Path("/Applications/Ollama.app/Contents/Resources/ollama"),
        Path("/usr/local/bin/ollama"),
        Path("/usr/bin/ollama"),
    ]
    return next((str(item) for item in candidates if item.is_file()), None)


class OllamaRuntime:
    def __init__(
        self,
        url: str = OLLAMA_URL,
        *,
        autostart: bool = True,
        binary: str | None = None,
        command: list[str] | None = None,
        pull: tuple[str, ...] = RECOMMENDED,
        keep_alive: str = KEEP_ALIVE,
        log: Path | None = None,
        choose: Callable[[], str | None] | None = None,
        start_timeout: float = 60.0,
        check_every: float = 5.0,
    ) -> None:
        """``choose`` — какую модель прогреть первой (обычно та, что служба выберет сама)."""
        self.url = url.rstrip("/")
        parts = urllib.parse.urlsplit(self.url)
        self.local = parts.hostname in LOOPBACK
        self.pull_list = tuple(item for item in pull if item)
        self.keep_alive = keep_alive
        self.choose = choose
        self.pulls: dict[str, dict[str, Any]] = {}
        self.warm: str | None = None
        self.warming: str | None = None
        self.supervisor: Supervisor | None = None
        self.missing = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        if autostart and self.local:
            program = command or ([found, "serve"] if (found := find_binary(binary)) else None)
            if program is None:
                self.missing = True
            else:
                env = {key: os.environ.get(key, value) for key, value in SETTINGS.items()}
                env["OLLAMA_HOST"] = f"{parts.hostname}:{parts.port or 11434}"
                env["OLLAMA_KEEP_ALIVE"] = os.environ.get("OLLAMA_KEEP_ALIVE", keep_alive)
                self.supervisor = Supervisor(
                    "ollama", program, ready=self.alive, env=env, log=log,
                    start_timeout=start_timeout, check_every=check_every,
                )  # fmt: skip

    # Жизненный цикл ------------------------------------------------------------------

    def start(self) -> None:
        if self.supervisor is not None:
            self.supervisor.start()
        self._thread = threading.Thread(target=self._prepare, name="ollama-prepare", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.supervisor is not None:
            self.supervisor.stop()

    def status(self) -> dict[str, Any]:
        if not self.local:
            state = "remote"
        elif self.supervisor is not None:
            state = self.supervisor.state
        elif self.missing:
            state = "not_installed" if not self.alive() else "external"
        else:
            state = "external" if self.alive() else "stopped"
        shown: dict[str, Any] = {
            "ollama": state,
            "pulls": {name: dict(item) for name, item in self.pulls.items()},
            "warm": self.warm,
        }
        if self.supervisor is not None and self.supervisor.error:
            shown["error"] = self.supervisor.error
        return shown

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/api/version", timeout=1.0) as response:
                return response.status == 200
        except (OSError, ValueError):
            return False

    # Модели ---------------------------------------------------------------------------

    def prefer(self, name: str | None) -> None:
        """Основная модель сменилась — прогреть её в фоне, не задерживая ответ."""
        if not name or name in (self.warm, self.warming):
            return
        threading.Thread(target=self.load, args=(name,), name="ollama-warm", daemon=True).start()

    def load(self, name: str) -> bool:
        """Загрузить модель в память и держать ``keep_alive``."""
        with self._lock:
            if name == self.warm:
                return True
            self.warming = name
        try:
            body = {"model": name, "keep_alive": self.keep_alive, "stream": False}
            self._post("/api/generate", body, 600.0)
            self.warm = name
            return True
        except (OSError, ValueError):
            return False
        finally:
            self.warming = None

    def download(self, name: str) -> bool:
        """Скачать модель; ход скачивания — в ``pulls[name]``."""
        state = self.pulls.setdefault(name, {})
        state.update({"status": "downloading", "completed": 0, "total": None, "error": None})
        try:
            request = self._request("/api/pull", {"model": name, "stream": True})
            with urllib.request.urlopen(request, timeout=3600) as response:
                for line in response:
                    if self._stop.is_set():
                        state["status"] = "stopped"
                        return False
                    if not line.strip():
                        continue
                    part = json.loads(line)
                    if part.get("error"):
                        raise ValueError(part["error"])
                    if part.get("total"):
                        state["total"] = part["total"]
                        state["completed"] = part.get("completed", 0)
            state["status"] = "done"
            return True
        except (OSError, ValueError) as error:
            state.update({"status": "failed", "error": str(error)[:300]})
            return False

    def installed(self) -> set[str]:
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=2.0) as response:
                items = json.loads(response.read()).get("models", [])
        except (OSError, ValueError):
            return set()
        return {item.get("name", "") for item in items}

    # Внутреннее ---------------------------------------------------------------------

    def _prepare(self) -> None:
        if self.supervisor is not None:
            self.supervisor.wait(self.supervisor.start_timeout + 5)
        while not self._stop.is_set() and not self.alive():
            if self._stop.wait(2.0):
                return
        have = self.installed()
        for name in self.pull_list:
            if self._stop.is_set():
                return
            if name not in have and f"{name}:latest" not in have:
                self.download(name)
        chosen = self.choose() if self.choose else None
        if chosen and not self._stop.is_set():
            self.load(chosen)

    def _request(self, path: str, body: dict[str, Any]) -> urllib.request.Request:
        return urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> Any:
        with urllib.request.urlopen(self._request(path, body), timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
