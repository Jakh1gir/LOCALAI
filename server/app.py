"""Приложение сервера LOCALAI."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException

from engine import Database, EngineError, open_database
from server import ai_link, api
from server import backups as backup_files
from server.files import FileStore
from server.routes import ENDPOINTS
from server.worker import Worker


def create_app(
    database: str | Path | Database,
    *,
    backups: str | Path | None = None,
    imports: str | Path | None = None,
    files: str | Path | None = None,
    ai: Any = None,
    ai_url: str | None = None,
) -> Starlette:
    """Приложение поверх базы; база открывается и доводится до текущей схемы сразу.

    Каждый маршрут, кроме открытых, требует входа (API-02). Первого
    администратора создаёт ``python -m server.admin``.

    ИИ-служба — отдельный процесс на этом компьютере (``python -m ai``):
    адрес ``ai_url`` или переменная ``LOCALAI_AI_URL``, ключ — файл ``ai.key``
    рядом с базой. ``ai`` подменяет связь целиком (тесты).
    """
    db = database if isinstance(database, Database) else open_database(str(database))
    folder = Path(backups) if backups else Path(db.path).resolve().parent / "backups"
    backup_files.clean_work(folder)
    work = Path(imports) if imports else Path(db.path).resolve().parent / "imports"
    store = FileStore(Path(files) if files else Path(db.path).resolve().parent / "files")
    if ai is None:
        url = ai_url or os.environ.get("LOCALAI_AI_URL") or ai_link.DEFAULT_URL
        ai = ai_link.AiLink(url, ai_link.load_key(Path(db.path).resolve().parent))
    server = api.Server(db, folder, work, store, ai)
    worker = Worker(db, store)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        # Чтение документов простых форматов — фоновым потоком сервера (KNOW-01).
        worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = Starlette(
        lifespan=lifespan,
        routes=[server.route(endpoint) for endpoint in ENDPOINTS],
        exception_handlers={
            EngineError: api.on_engine_error,
            HTTPException: api.on_http_error,
            Exception: api.on_unexpected,
        },
    )
    app.state.db = db
    app.state.files = store
    app.state.worker = worker
    app.state.ai = ai
    app.state.grants = server.grants
    return app
