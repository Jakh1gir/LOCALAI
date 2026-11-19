"""Фоновый процесс под присмотром: запуск, проверка, перезапуск, остановка.

Используется дважды:

- сервер запускает ИИ-службу (``python -m ai``);
- ИИ-служба запускает Ollama (``ollama serve``).

Если нужный процесс уже работает (его запустил кто-то другой — например,
приложение Ollama при входе в Windows), он не трогается: состояние
``external``. Свой процесс перезапускается, если упал, — но не чаще
``max_restarts`` раз за ``window`` секунд, чтобы падающая программа не
крутилась вечно. При остановке свой процесс завершается, чужой — нет.
Окна консоли в Windows не появляются.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: процесс без окна консоли в Windows


class Supervisor:
    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        ready: Callable[[], bool],
        env: dict[str, str] | None = None,
        log: Path | None = None,
        check_every: float = 5.0,
        start_timeout: float = 60.0,
        max_restarts: int = 5,
        window: float = 600.0,
    ) -> None:
        self.name = name
        self.command = command
        self.ready = ready
        self.env = env
        self.log = log
        self.check_every = check_every
        self.start_timeout = start_timeout
        self.max_restarts = max_restarts
        self.window = window
        self.state = "stopped"
        self.error: str | None = None
        self.restarts: list[float] = []
        self._child: subprocess.Popen[bytes] | None = None
        self._output: IO[bytes] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # Управление ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name=f"watch-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._terminate(timeout)
        if self.state != "failed":
            self.state = "stopped"

    def wait(self, timeout: float) -> bool:
        """Дождаться состояния «работает» (своего или чужого процесса)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state in ("running", "external"):
                return True
            if self.state == "failed":
                return False
            time.sleep(0.05)
        return False

    def status(self) -> dict[str, Any]:
        child = self._child
        return {
            "state": self.state,
            "pid": child.pid if child is not None and child.poll() is None else None,
            "restarts": len(self.restarts),
            "error": self.error,
        }

    # Внутреннее ------------------------------------------------------------------

    def _watch(self) -> None:
        while not self._stop.is_set():
            child = self._child
            if child is not None and child.poll() is not None:
                self._closed(child)  # свой процесс упал — запускаем снова
                if not self._launch():
                    return
            elif child is None:
                if self._safe_ready():
                    self.state = "external"  # уже работает чужой — не трогаем
                elif not self._launch():
                    return
            else:
                self.state = "running" if self._safe_ready() else "starting"
            self._stop.wait(self.check_every)

    def _closed(self, child: subprocess.Popen[bytes]) -> None:
        self.error = f"{self.name} завершился с кодом {child.returncode}"
        self._child = None
        self.restarts = [at for at in self.restarts if time.monotonic() - at < self.window]
        self.restarts.append(time.monotonic())

    def _launch(self) -> bool:
        if len(self.restarts) > self.max_restarts:
            self.state = "failed"
            self.error = (
                f"{self.name} падает при запуске: остановлено после {self.max_restarts} попыток"
            )
            return False
        self.state = "starting"
        try:
            with self._lock:
                self._open_log()
                self._child = subprocess.Popen(
                    self.command,
                    env={**os.environ, **(self.env or {})},
                    stdin=subprocess.DEVNULL,
                    stdout=self._output or subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    creationflags=NO_WINDOW if os.name == "nt" else 0,
                )
        except OSError as error:
            self.state = "failed"
            self.error = f"{self.name} не запускается: {error}"
            return False
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._child.poll() is not None:
                return True  # упал сразу: следующий круг перезапустит или остановит
            if self._safe_ready():
                self.state = "running"
                self.error = None
                return True
            time.sleep(0.1)
        if not self._stop.is_set():
            self.error = f"{self.name} не ответил за {self.start_timeout:.0f} с"
        return True

    def _safe_ready(self) -> bool:
        try:
            return bool(self.ready())
        except Exception:  # noqa: BLE001 — проверка готовности не должна ронять присмотр
            return False

    def _open_log(self) -> None:
        if self.log is None or self._output is not None:
            return
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._output = self.log.open("ab")

    def _terminate(self, timeout: float) -> None:
        with self._lock:
            child = self._child
            self._child = None
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout)
            if self._output is not None:
                self._output.close()
                self._output = None
