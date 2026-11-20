"""Документы, их файлы, привязки и упоминания ККС; очередь задач для процесса схем.

ТЗ 8 (KNOW-01…04), 10 (VIS-01, VIS-02), DATA-09.
"""

from __future__ import annotations

import mimetypes
from typing import Any

from starlette.responses import FileResponse

from engine import EngineError, documents, jobs, sheets
from engine.errors import FORBIDDEN, not_found
from server import schemas
from server.api import Call, Endpoint
from server.files import MAX_FILE

DOCUMENTS, WORKER = "section.documents", "section.worker"


def _media(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _name(call: Call) -> str:
    name = (call.query.get("name") or "").strip()
    if not name or "/" in name or "\\" in name or len(name) > 255:
        raise EngineError("document_file_name", "Укажите имя файла", field="name")
    documents.reader_for(name)  # формат проверяется до записи файла
    return name


def upload(call: Call) -> dict[str, Any]:
    """Загрузить документ: тело — файл, имя — параметр name. Чтение ставится в очередь."""
    assert call.db is not None and call.files is not None
    name = _name(call)
    sha, _ = call.files.put(call.body)  # вне транзакции записи (API-07)
    with call.db.write(call.access.actor) as tx:
        return documents.create(
            tx,
            call.access,
            title=call.query.get("title"),
            file_name=name,
            sha256=sha,
            size=len(call.body),
            media_type=_media(name),
            type_id=call.query.get("type_id") or None,
        )


def replace(call: Call) -> dict[str, Any]:
    """Заменить файл документа: новая версия, прежние фрагменты уходят из поиска."""
    assert call.db is not None and call.files is not None
    name = _name(call)
    sha, _ = call.files.put(call.body)
    with call.db.write(call.access.actor) as tx:
        return documents.replace(
            tx,
            call.access,
            call.params["id"],
            expected_version=call.query_int("version", 0),
            file_name=name,
            sha256=sha,
            size=len(call.body),
            media_type=_media(name),
        )


def listing(call: Call) -> dict[str, Any]:
    """Документы, видимые роли; с target_kind и target_id — привязанные к записи или ККС."""
    return documents.listing(
        call.conn,
        call.access,
        target_kind=call.query.get("target_kind") or None,
        target_id=call.query.get("target_id") or None,
        state=call.query.get("state") or None,
        archived=call.query.get("archived") == "true",
        limit=min(max(call.query_int("limit", 100), 1), 500),
    )


def get(call: Call) -> dict[str, Any]:
    """Карточка документа: состояние чтения, файл, привязки."""
    return documents.get(call.conn, call.access, call.params["id"])


def download(call: Call) -> FileResponse:
    """Файл документа текущей версии."""
    assert call.files is not None
    row = call.conn.execute(
        "SELECT d.type_id, d.file_name, d.file_sha256, f.media_type FROM documents d"
        " JOIN files f ON f.sha256 = d.file_sha256 WHERE d.id = ?",
        (call.params["id"],),
    ).fetchone()
    if row is None or not (call.access.sees_documents(row["type_id"]) or call.access.has("worker")):
        raise not_found("document", call.params["id"])
    return FileResponse(
        call.files.existing(row["file_sha256"]),
        media_type=row["media_type"],
        filename=row["file_name"],
    )


def text(call: Call) -> dict[str, Any]:
    """Прочитанный текст по фрагментам; page — одна страница."""
    return documents.chunks(call.conn, call.access, call.params["id"], call.query.get("page"))


def archive(call: Call) -> dict[str, Any]:
    """Убрать документ в архив: из поиска он исчезает."""
    return documents.archive(call.tx, call.access, call.params["id"])


def link(call: Call) -> dict[str, Any]:
    """Привязать документ к записи или объекту ККС."""
    return documents.link(call.tx, call.access, call.params["id"], call.body.kind, call.body.id)


def unlink(call: Call) -> dict[str, Any]:
    """Снять привязку документа."""
    kind = call.params["kind"]
    if kind not in ("record", "kks"):
        raise EngineError("document_link_kind", "Привязка — record или kks", field="kind")
    return documents.unlink(call.tx, call.access, call.params["id"], kind, call.params["target"])


def mentions(call: Call) -> dict[str, Any]:
    """Коды ККС, найденные в тексте: предложения связей и решения по ним."""
    return documents.mentions(call.conn, call.access, call.params["id"])


def decide(call: Call) -> dict[str, Any]:
    """Подтвердить (создать связь с ККС) или отклонить найденный код."""
    return documents.decide_mention(
        call.tx,
        call.access,
        call.params["id"],
        call.params["kks_id"],
        call.body.decision == "confirm",
    )


def types(call: Call) -> dict[str, Any]:
    """Типы документов, которые роль видит."""
    rows = call.conn.execute(
        "SELECT id, name FROM document_types WHERE archived_at IS NULL ORDER BY name_norm"
    ).fetchall()
    return {"items": [dict(row) for row in rows if call.access.sees_documents(row["id"])]}


# Очередь задач -----------------------------------------------------------------


def job_list(call: Call) -> dict[str, Any]:
    """Фоновые задачи: администратору и исполнителю."""
    if not (call.access.has("admin") or call.access.has("worker")):
        raise EngineError("forbidden", "Недостаточно прав: фоновые задачи", status=FORBIDDEN)
    return {"items": jobs.listing(call.conn, state=call.query.get("state") or None)}


def claim(call: Call) -> dict[str, Any]:
    """Взять задачу процесса схем: документ и адрес его файла."""
    job = jobs.claim(call.tx, call.body.reader, call.access.login, call.body.kinds)
    if job is None:
        return {"job": None}
    if job["kind"] == documents.JOB_KIND:
        started = documents.start_reading(call.tx, job["target_id"], job["target_version"])
        if not started:
            jobs.finish(call.tx, job["id"], call.access.login)
            return {"job": None}
        row = call.tx.conn.execute(
            "SELECT d.id, d.version, d.file_name, d.file_sha256, f.media_type FROM documents d"
            " JOIN files f ON f.sha256 = d.file_sha256 WHERE d.id = ?",
            (job["target_id"],),
        ).fetchone()
        return {
            "job": _job(job),
            "document": {
                "id": row["id"],
                "version": row["version"],
                "file_name": row["file_name"],
                "sha256": row["file_sha256"],
                "media_type": row["media_type"],
                "file_url": f"/api/v1/documents/{row['id']}/file",
            },
        }
    return {"job": _job(job)}


def complete(call: Call) -> dict[str, Any]:
    """Результат чтения: страницы текста. Сервер делит их на фрагменты и индексирует."""
    job = jobs.held(call.tx.conn, call.params["id"], call.access.login)
    pages = [page.model_dump(exclude_none=True) for page in call.body.pages]
    try:
        stored = documents.store_text(
            call.tx, job["target_id"], job["target_version"], pages, "vision"
        )
        if call.body.sheets:
            stored["sheets"] = _store_sheets(call, job)
    except EngineError as error:
        if error.code != "document_version_gone":
            raise
        stored = {"state": "stale"}  # документ заменили во время чтения: текст не нужен
    done = jobs.finish(call.tx, job["id"], call.access.login)
    return {"job": _job(done), "result": stored}


def _store_sheets(call: Call, job: dict[str, Any]) -> int:
    try:
        return sheets.store(call.tx, job["target_id"], job["target_version"], call.body.sheets)
    except (KeyError, TypeError, ValueError) as error:
        raise EngineError(
            "sheet_invalid", f"Граф листа не разобран: {error}", field="sheets"
        ) from None


def fail(call: Call) -> dict[str, Any]:
    """Неудача чтения: повтор, пока есть попытки, иначе ошибка у документа."""
    after = jobs.fail(
        call.tx, call.params["id"], call.access.login, call.body.error, retry=call.body.retry
    )
    documents.reading_failed(
        call.tx,
        after["target_id"],
        after["target_version"],
        call.body.error,
        final=after["state"] == "error",
    )
    return {"job": _job(after)}


def _job(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "kind",
        "reader",
        "target_id",
        "target_version",
        "state",
        "attempts",
        "max_attempts",
        "error",
    )
    return {key: job[key] for key in keys} | {
        "created_at": job["created_at"],
        "lease_until": job["lease_until"],
    }


def _d(method: str, path: str, handler: Any, permission: str, **extra: Any) -> Endpoint:
    return Endpoint(method, path, handler, permission, tag="Документы", **extra)


def _j(method: str, path: str, handler: Any, permission: str, **extra: Any) -> Endpoint:
    return Endpoint(method, path, handler, permission, tag="Фоновые задачи", **extra)


_FILE = {"raw_body": True, "manual": True, "idempotent": False, "max_body": MAX_FILE}

ENDPOINTS = [
    _d("GET", "/document-types", types, "session"),
    _d(
        "POST",
        "/documents",
        upload,
        DOCUMENTS,
        query=("name", "title", "type_id"),
        status=201,
        **_FILE,
    ),
    _d(
        "GET",
        "/documents",
        listing,
        "session",
        query=("target_kind", "target_id", "state", "archived", "limit"),
    ),
    _d("GET", "/documents/{id}", get, "session"),
    _d("GET", "/documents/{id}/file", download, "session"),
    _d("PUT", "/documents/{id}/file", replace, DOCUMENTS, query=("name", "version"), **_FILE),
    _d("GET", "/documents/{id}/text", text, "session", query=("page",)),
    _d("POST", "/documents/{id}/archive", archive, DOCUMENTS),
    _d("POST", "/documents/{id}/links", link, DOCUMENTS, body=schemas.DocumentLink),
    _d("DELETE", "/documents/{id}/links/{kind}/{target}", unlink, DOCUMENTS),
    _d("GET", "/documents/{id}/mentions", mentions, "session"),
    _d(
        "POST", "/documents/{id}/mentions/{kks_id}", decide, DOCUMENTS, body=schemas.MentionDecision
    ),
    _j("GET", "/jobs", job_list, "session", query=("state",)),
    _j("POST", "/jobs/claim", claim, WORKER, body=schemas.JobClaim, idempotent=False),
    _j(
        "POST",
        "/jobs/{id}/complete",
        complete,
        WORKER,
        body=schemas.JobComplete,
        idempotent=False,
        max_body=64 * 1024 * 1024,
    ),
    _j("POST", "/jobs/{id}/fail", fail, WORKER, body=schemas.JobFail, idempotent=False),
]
