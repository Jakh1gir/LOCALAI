"""Исполнитель задач сервера: чтение документов простых форматов (KNOW-01).

Задача берётся в короткой транзакции, файл читается вне транзакции
(API-07), текст сохраняется в следующей короткой транзакции. В работающем
сервере исполнитель — фоновый поток; он просыпается после каждой записи в
базу и раз в несколько секунд, если записей нет.
"""

from __future__ import annotations

import logging
import threading

from engine import SYSTEM, Database, EngineError, documents, jobs
from server import readers
from server.files import FileStore

log = logging.getLogger("localai.worker")
WORKER = "server"
IDLE = 5.0


def run_pending(db: Database, files: FileStore, *, limit: int = 100) -> int:
    """Выполнить задачи сервера из очереди; число выполненных."""
    done = 0
    while done < limit:
        with db.write(SYSTEM) as tx:
            job = jobs.claim(tx, "server", WORKER, [documents.JOB_KIND])
            if job is None:
                return done
            document = tx.conn.execute(
                "SELECT file_name, file_sha256 FROM documents WHERE id = ? AND version = ?",
                (job["target_id"], job["target_version"]),
            ).fetchone()
            if document is None or not documents.start_reading(
                tx, job["target_id"], job["target_version"]
            ):
                jobs.finish(tx, job["id"], WORKER)  # документ заменён или в архиве
                done += 1
                continue
        _read(db, files, job, document["file_name"], document["file_sha256"])
        done += 1
    return done


def _read(db: Database, files: FileStore, job: dict, file_name: str, sha: str) -> None:
    try:
        pages = readers.read(files.existing(sha), file_name)
    except EngineError as error:
        _failed(db, job, error.message, retry=error.code == "file_missing")
        return
    except Exception as error:  # незнакомая ошибка читателя: повтор и запись в лог
        log.exception("Чтение документа %s упало", job["target_id"])
        _failed(db, job, f"Сбой чтения: {error}", retry=True)
        return
    try:
        with db.write(SYSTEM) as tx:
            documents.store_text(tx, job["target_id"], job["target_version"], pages, WORKER)
            jobs.finish(tx, job["id"], WORKER)
    except EngineError as error:
        _failed(db, job, error.message, retry=False)


def _failed(db: Database, job: dict, message: str, *, retry: bool) -> None:
    with db.write(SYSTEM) as tx:
        after = jobs.fail(tx, job["id"], WORKER, message, retry=retry)
        documents.reading_failed(
            tx, job["target_id"], job["target_version"], message, final=after["state"] == "error"
        )


class Worker:
    """Фоновый поток сервера: просыпается по записи в базу или по таймеру."""

    def __init__(self, db: Database, files: FileStore) -> None:
        self.db, self.files = db, files
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        db.on_commit(self._wake.set)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="localai-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                run_pending(self.db, self.files)
            except Exception:  # поток не должен умирать: ошибка — в лог, следующий круг
                log.exception("Исполнитель задач: сбой круга")
            self._wake.wait(IDLE)
