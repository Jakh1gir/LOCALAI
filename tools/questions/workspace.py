"""Настоящий HTTP-сервер на согласованной временной копии исходной базы."""

from __future__ import annotations

import json
import secrets
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from engine import SYSTEM, access, open_database
from server.app import create_app

from .format import QuestionError, Role


class Client:
    def __init__(self, url: str) -> None:
        self.url = url
        self.token = ""
        self.opener = build_opener(ProxyHandler({}))

    def request(
        self, method: str, path: str, body: dict | None = None, query: dict | None = None
    ) -> tuple[int, dict]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Cookie"] = self.token
        if method != "GET" and not path.startswith("/api/v1/auth/"):
            headers["Idempotency-Key"] = secrets.token_hex(16)
        if body is not None:
            headers["Content-Type"] = "application/json"
        params = {
            key: str(value).lower() if isinstance(value, bool) else value
            for key, value in (query or {}).items()
        }
        url = self.url + path + ("?" + urlencode(params) if params else "")
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = Request(url, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=30)
        except HTTPError as error:
            response = error
        except URLError as error:
            raise QuestionError(f"Локальный сервер не отвечает: {error.reason}") from error
        with response:
            try:
                reply = json.load(response)
            except (ValueError, UnicodeError) as error:
                raise QuestionError(f"{method} {path}: ответ не является JSON") from error
            cookie = response.headers.get("Set-Cookie")
            if cookie:
                self.token = cookie.split(";", 1)[0]
            return response.status, reply

    def ok(self, method: str, path: str, body: dict | None = None) -> dict:
        status, reply = self.request(method, path, body)
        if status not in (200, 201, 204):
            raise QuestionError(
                f"{method} {path}: HTTP {status}, {reply.get('error', {}).get('message', '')}"
            )
        return reply

    def login(self, login: str, password: str) -> None:
        self.ok("POST", "/api/v1/auth/login", {"login": login, "password": password})


def role_client(admin: Client, policy: Role, bindings: dict) -> Client:
    """Настоящая роль и пользователь только во временной копии; права задаются API."""
    suffix = secrets.token_hex(8)
    role = admin.ok("POST", "/api/v1/roles", {"name": f"Вопросы {suffix}"})
    targets = (
        ["*"] if "*" in policy.datasets else [bindings[name]["id"] for name in policy.datasets]
    )
    admin.ok(
        "PUT",
        f"/api/v1/roles/{role['id']}/permissions",
        {
            "permissions": [
                {"scope": "dataset", "target": target, "action": "view"} for target in targets
            ],
        },
    )
    login = f"questions-{suffix}"
    user = admin.ok(
        "POST",
        "/api/v1/users",
        {"login": login, "name": "Проверочные вопросы", "role_id": role["id"]},
    )
    client = Client(admin.url)
    client.login(login, user["temporary_password"])
    client.ok(
        "POST",
        "/api/v1/auth/password",
        {
            "current_password": user["temporary_password"],
            "new_password": secrets.token_urlsafe(24),
        },
    )
    return client


def snapshot(source: Path, target: Path) -> None:
    # SQLite backup видит WAL и даёт согласованный снимок, не копируя живой файл побайтно.
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(target)) as copy,
    ):
        original.backup(copy)


@contextmanager
def serve(source: Path) -> Iterator[Client]:
    from uvicorn import Config, Server

    with TemporaryDirectory(prefix="localai-questions-") as folder:
        target = Path(folder) / "questions.sqlite3"
        snapshot(source.resolve(), target)
        with closing(open_database(str(target))) as db, socket.socket() as listener:
            login = "questions-admin-" + secrets.token_hex(8)
            password = secrets.token_urlsafe(24)
            with db.write(SYSTEM) as tx:
                access.create_admin(tx, login, "Стенд проверочных вопросов", password)
            listener.bind(("127.0.0.1", 0))
            # Фоновое чтение документов не должно менять снимок во время прогона.
            server = Server(
                Config(create_app(db), log_level="error", access_log=False, lifespan="off")
            )
            worker = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            worker.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started and worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not server.started:
                    raise QuestionError("Локальный сервер не запустился")
                client = Client(f"http://127.0.0.1:{listener.getsockname()[1]}")
                client.login(login, password)
                yield client
            finally:
                server.should_exit = True
                worker.join(timeout=10)
                if worker.is_alive():
                    raise QuestionError("Локальный сервер не завершился")
