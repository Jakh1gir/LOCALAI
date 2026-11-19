"""Приложение сервера LOCALAI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException

from engine import Database, EngineError, open_database
from server import api
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
) -> Starlette:
    """Приложение поверх базы; база открывается и доводится до текущей схемы сразу.

    Каждый маршрут, кроме открытых, требует входа (API-02). Первого
    администратора создаёт ``python -m server.admin``.
    """
    db = database if isinstance(database, Database) else open_database(str(database))
    folder = Path(backups) if backups else Path(db.path).resolve().parent / "backups"
    backup_files.clean_work(folder)
    work = Path(imports) if imports else Path(db.path).resolve().parent / "imports"
    store = FileStore(Path(files) if files else Path(db.path).resolve().parent / "files")
    server = api.Server(db, folder, work, store)
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
    return app
