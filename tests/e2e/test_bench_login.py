"""Стенд замеров входит в API (запрос 003): без сессии API отвечает 401."""

from __future__ import annotations

import pytest

from tools.bench import BenchError, HttpAccess, _serve_copy
from tools.generate import generate_database


def test_bench_copy_server_logs_in_before_measuring(tmp_path):
    path = tmp_path / "c101.sqlite3"
    generate_database(path, 60, 1)
    with _serve_copy(path) as client:
        assert client.token  # вход выполнен до замеров
        sites = client.request("GET", "/sites")["items"]
        assert [site["name"] for site in sites]
        anonymous = HttpAccess(client.base)
        with pytest.raises(BenchError, match="HTTP 401"):
            anonymous.request("GET", "/sites")
        with pytest.raises(BenchError, match="Неверный логин или пароль"):
            anonymous.login("bench", "не-тот-пароль")
