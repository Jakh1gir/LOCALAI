"""Исполнитель задач сервера: чтение документов (KNOW-01) и векторы фрагментов (KNOW-02).

Задача берётся в короткой транзакции, файл читается вне транзакции
(API-07), текст сохраняется в следующей короткой транзакции. В работающем
сервере исполнитель — фоновый поток; он просыпается после каждой записи в
базу и раз в несколько секунд, если записей нет.

Векторы фрагментов считает ИИ-служба (модель векторов); исполнитель
отправляет ей фрагменты пачками по ``vectors.BATCH`` вне транзакции и
сохраняет ответ. Служба недоступна — векторы досчитаются, когда она
появится; поиск по словам работает и без них.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from engine import SYSTEM, Database, EngineError, ai_memory, documents, jobs, vectors
from server import ai_link, readers
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


class Embedder:
    """Векторы фрагментов без вектора текущей модели — через ИИ-службу."""

    RECHECK = 60.0  # секунд между вопросами службе «какая модель векторов», если работы нет

    def __init__(self, db: Database, link: Any) -> None:
        self.db, self.link = db, link
        self.model: str | None = None
        self._checked = 0.0

    def run(self, batches: int = 20) -> int:
        """Посчитать до ``batches`` пачек; число сохранённых векторов."""
        if self.link is None:
            return 0
        with self.db.read() as conn:
            config = ai_memory.settings(conn, secrets=True).get("embedding")
            idle = self.model is not None and not vectors.pending(conn, self.model, 1)
        if idle and time.monotonic() - self._checked < self.RECHECK:
            return 0
        try:
            self.model = self.link.embed([], config, timeout=5.0).get("model")
            self._checked = time.monotonic()
            if not self.model:
                return 0
            saved = 0
            for _ in range(batches):
                with self.db.read() as conn:
                    items = vectors.pending(conn, self.model)
                if not items:
                    break
                found = self.link.embed([item["text"] for item in items], config)
                if found.get("model") != self.model:
                    self.model = found.get("model")  # модель сменилась на ходу
                    break
                with self.db.write(SYSTEM) as tx:
                    saved += vectors.store(
                        tx,
                        self.model,
                        [(item["seq"], vector) for item, vector in zip(items, found["vectors"],
                                                                       strict=True)],
                    )  # fmt: skip
            return saved
        except (ai_link.AiUnavailable, EngineError, KeyError, TypeError, ValueError) as error:
            log.info("Векторы фрагментов отложены: %s", error)
            self._checked = time.monotonic()
            return 0


class Worker:
    """Фоновый поток сервера: просыпается по записи в базу или по таймеру."""

    def __init__(self, db: Database, files: FileStore, ai: Any = None) -> None:
        self.db, self.files = db, files
        self.embedder = Embedder(db, ai)
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
            busy = False
            try:
                run_pending(self.db, self.files)
                busy = self.embedder.run() > 0
            except Exception:  # поток не должен умирать: ошибка — в лог, следующий круг
                log.exception("Исполнитель задач: сбой круга")
            if not busy:
                self._wake.wait(IDLE)
