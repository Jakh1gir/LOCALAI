"""Приложение сервера LOCALAI."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException

from engine import Database, EngineError, open_database
from server import api
from server.routes import ENDPOINTS


def create_app(database: str | Path | Database) -> Starlette:
    """Приложение поверх базы; база открывается и доводится до текущей схемы сразу.

    Каждый маршрут, кроме открытых, требует входа (API-02). Первого
    администратора создаёт ``python -m server.admin``.
    """
    db = database if isinstance(database, Database) else open_database(str(database))
    server = api.Server(db)
    app = Starlette(
        routes=[server.route(endpoint) for endpoint in ENDPOINTS],
        exception_handlers={
            EngineError: api.on_engine_error,
            HTTPException: api.on_http_error,
            Exception: api.on_unexpected,
        },
    )
    app.state.db = db
    return app
