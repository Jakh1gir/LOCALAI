"""Резервные копии: только для роли с разделом «Копии»."""

from __future__ import annotations

from typing import Any

from starlette.responses import FileResponse

from server import backups, schemas
from server.api import Call, Endpoint

SECTION = "section.backups"


def list_backups(call: Call) -> dict[str, Any]:
    """Копии в папке копий сервера, от новых к старым."""
    assert call.backups is not None
    return {"items": backups.list_backups(call.backups)}


def create_backup(call: Call) -> dict[str, Any]:
    """Сделать зашифрованную копию базы; пароль копии в базе не хранится."""
    assert call.db is not None and call.backups is not None
    body = call.body
    return backups.create(call.db, call.backups, body.password, body.label, call.access.actor)


def download(call: Call) -> FileResponse:
    """Скачать файл копии; он зашифрован, как и лежит на сервере."""
    assert call.backups is not None
    path = backups.file_path(call.backups, call.params["name"])
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


def restore(call: Call) -> dict[str, Any]:
    """Восстановить базу из копии; текущее состояние сначала уходит в защитную копию."""
    assert call.db is not None and call.backups is not None
    return backups.restore(
        call.db, call.backups, call.params["name"], call.body.password, call.access.actor
    )


def _e(method: str, path: str, handler, **extra: Any) -> Endpoint:  # type: ignore[no-untyped-def]
    return Endpoint(
        method, path, handler, SECTION, tag="Копии", manual=True, idempotent=False, **extra
    )


ENDPOINTS = [
    _e("GET", "/backups", list_backups),
    _e("POST", "/backups", create_backup, body=schemas.BackupCreate, status=201),
    _e("GET", "/backups/{name}/file", download),
    _e("POST", "/backups/{name}/restore", restore, body=schemas.BackupRestore),
]
