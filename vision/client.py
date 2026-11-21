"""Клиент API v1: сессия принадлежит отдельной учётной записи worker."""

from __future__ import annotations

import hashlib
import json
import ssl
import time
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)


class ApiError(RuntimeError):
    def __init__(self, status, error):
        self.status = status
        self.code = error.get("code")
        super().__init__(error.get("message") or f"HTTP {status}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Client:
    def __init__(self, url, login, password, ca=None):
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path.strip("/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Ожидается адрес сервера с протоколом и портом")
        self.url = url.rstrip("/")
        self.login = login
        self.password = password
        self.opener = build_opener(
            HTTPCookieProcessor(CookieJar()),
            NoRedirect(),
            HTTPSHandler(context=ssl.create_default_context(cafile=ca)),
        )

    def open(self, path, body=None, authenticate=True):
        if not path.startswith("/api/v1/"):
            raise ValueError("Ожидается путь к API v1")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        if data and len(data) > 64 * 1024**2:
            raise ValueError("Результат чтения больше 64 МиБ")
        request = Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        try:
            return self.opener.open(request, timeout=20)
        except HTTPError as exc:
            with exc:
                try:
                    error = json.load(exc)["error"]
                except (ValueError, KeyError):
                    error = {}
            if exc.code == 401 and authenticate:
                with self.open(
                    "/api/v1/auth/login",
                    {"login": self.login, "password": self.password},
                    authenticate=False,
                ):
                    pass
                return self.open(path, body, authenticate=False)
            raise ApiError(exc.code, error) from exc

    def post(self, path, body):
        with self.open("/api/v1" + path, body) as response:
            return json.load(response)

    def download(self, url, path, deadline):
        digest = hashlib.sha256()
        with self.open(url) as response, path.open("wb") as stream:
            while chunk := response.read(256 * 1024):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Истекло время скачивания документа")
                digest.update(chunk)
                stream.write(chunk)
        return digest.hexdigest()
