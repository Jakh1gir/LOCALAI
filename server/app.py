"""Приложение сервера LOCALAI."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException

from engine import Actor, Database, EngineError, open_database
from server import api
from server.routes import ENDPOINTS

# До этапа 2 входа нет: все действия записываются от имени локального пользователя,
# а сервер по умолчанию слушает только этот компьютер.
LOCAL_USER = Actor(None, "Локальный пользователь", "user")


def create_app(database: str | Path | Database, *, actor: Actor = LOCAL_USER) -> Starlette:
    """Приложение поверх базы; база открывается и доводится до текущей схемы сразу."""
    db = database if isinstance(database, Database) else open_database(str(database))
    server = api.Server(db, actor)
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
