"""Фоновый запуск: сервер запускает ИИ-службу, служба — Ollama, модели скачиваются и
прогреваются сами. Процессы настоящие; Ollama поддельный (tests/ai/fake_ollama.py).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from ai.process import Supervisor
from ai.runtime import OllamaRuntime
from server import ai_link

from .test_service import Running, _port

FAKE = str(Path(__file__).with_name("fake_ollama.py"))


def _until(condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("не дождались")
        time.sleep(0.05)


def _log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fake_log(tmp_path, monkeypatch):
    path = tmp_path / "fake.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(path))
    monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)
    return path


def test_ollama_starts_downloads_and_warms_by_itself(tmp_path, fake_log):
    url = f"http://127.0.0.1:{_port()}"
    runtime = OllamaRuntime(
        url, command=[sys.executable, FAKE], pull=("qwen3.5:9b", "qwen3.5:4b"),
        choose=lambda: "qwen3.5:9b", log=tmp_path / "logs" / "ollama.log", check_every=0.2,
    )  # fmt: skip
    assert runtime.status()["ollama"] == "stopped"
    runtime.start()
    try:
        _until(lambda: runtime.warm == "qwen3.5:9b")
        status = runtime.status()
        assert status["ollama"] == "running" and status["warm"] == "qwen3.5:9b"
        assert {name: item["status"] for name, item in status["pulls"].items()} == {
            "qwen3.5:9b": "done", "qwen3.5:4b": "done"
        }  # fmt: skip
        started = next(item for item in _log(fake_log) if item["call"] == "start")
        env = started["env"]
        assert env["OLLAMA_FLASH_ATTENTION"] == "1" and env["OLLAMA_KV_CACHE_TYPE"] == "q8_0"
        assert env["OLLAMA_CONTEXT_LENGTH"] == "32768" and env["OLLAMA_KEEP_ALIVE"] == "30m"
        warmed = [item for item in _log(fake_log) if item["call"] == "generate"]
        assert warmed == [{"call": "generate", "model": "qwen3.5:9b", "keep_alive": "30m"}]

        os.kill(started["pid"], signal.SIGKILL)  # Ollama упал — служба поднимает его снова
        _until(lambda: runtime.supervisor.status()["restarts"] == 1
               and runtime.status()["ollama"] == "running")  # fmt: skip
        assert len([item for item in _log(fake_log) if item["call"] == "start"]) == 2

        runtime.prefer("qwen3.5:4b")  # администратор сменил основную модель
        _until(lambda: runtime.warm == "qwen3.5:4b")
    finally:
        runtime.stop()
    assert runtime.status()["ollama"] == "stopped" and not runtime.alive()
    assert (tmp_path / "logs" / "ollama.log").exists()


def test_running_ollama_is_used_as_is(tmp_path, fake_log, monkeypatch):
    port = _port()
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{port}")
    import subprocess

    outside = subprocess.Popen([sys.executable, FAKE])  # «приложение Ollama в трее»
    try:
        runtime = OllamaRuntime(f"http://127.0.0.1:{port}", command=[sys.executable, FAKE],
                                pull=(), check_every=0.2)  # fmt: skip
        _until(runtime.alive)
        runtime.start()
        _until(lambda: runtime.status()["ollama"] == "external")
        runtime.stop()
        assert outside.poll() is None  # чужой процесс не остановлен
        assert len([item for item in _log(fake_log) if item["call"] == "start"]) == 1
    finally:
        outside.terminate()
        outside.wait(10)


def test_missing_ollama_and_crashing_program_are_reported(tmp_path):
    url = f"http://127.0.0.1:{_port()}"
    missing = OllamaRuntime(url, binary=str(tmp_path / "нет-ollama"), pull=())
    assert missing.supervisor is None and missing.status()["ollama"] == "not_installed"
    remote = OllamaRuntime("http://10.0.0.7:11434", pull=())
    assert remote.supervisor is None and remote.status()["ollama"] == "remote"

    broken = Supervisor("сломанная", [sys.executable, "-c", "import sys; sys.exit(3)"],
                        ready=lambda: False, check_every=0.05, max_restarts=2,
                        start_timeout=1)  # fmt: skip
    broken.start()
    _until(lambda: broken.state == "failed")
    broken.stop()
    assert "после 2 попыток" in broken.error


def test_server_starts_ai_service_in_background(tmp_path):
    from server.app import create_app

    port = _port()
    app = create_app(tmp_path / "bg.sqlite3", ai_url=f"http://127.0.0.1:{port}")
    link = app.state.ai
    assert link.health() is None
    supervisor = ai_link.start_service(
        link, key_file=tmp_path / ai_link.KEY_FILE, server_url="http://127.0.0.1:1",
        log=tmp_path / "logs" / "ai.log",
        command=[sys.executable, "-m", "ai", "--key-file", str(tmp_path / ai_link.KEY_FILE),
                 "--port", str(port), "--model", "none", "--no-ollama-start", "--pull", ""],
    )  # fmt: skip
    try:
        assert supervisor.wait(30), supervisor.status()
        health = link.health()
        assert health["service"] == "ai" and health["model"]["provider"] == "none"
        assert supervisor.status()["state"] == "running"
    finally:
        supervisor.stop()
    assert link.health() is None


def test_ai_service_reports_runtime(tmp_path, fake_log):
    from ai import service
    from ai.model import Config, build

    ollama = f"http://127.0.0.1:{_port()}"
    runtime = OllamaRuntime(ollama, command=[sys.executable, FAKE], pull=("qwen3.5:4b",),
                            choose=lambda: None, check_every=0.2)  # fmt: skip
    app = service.create_app(server_url="http://127.0.0.1:1", key="k",
                             model=build(Config(url=ollama)), runtime=runtime)  # fmt: skip
    port = _port()
    running = Running(app, port)  # lifespan службы запускает Ollama
    try:
        link = ai_link.AiLink(f"http://127.0.0.1:{port}", "k")
        _until(lambda: (link.health() or {}).get("model", {}).get("available") is True)
        health = link.health()
        assert health["runtime"]["ollama"] == "running"
        assert health["model"]["name"] == "qwen3.5:4b"  # скачалась — выбрана сама
        _until(lambda: runtime.warm == "qwen3.5:4b")  # служба прогрела основную модель
    finally:
        running.stop()
    assert not runtime.alive()  # остановлен вместе со службой
