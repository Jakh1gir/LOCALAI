"""Хранилище файлов по SHA-256 (DATA-09): один файл — одна копия на диске.

Файл лежит в ``<папка>/<первые 2 знака хэша>/<хэш>``. Запись атомарна:
сначала временный файл, затем переименование; повторная запись того же
содержимого ничего не меняет.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from engine import EngineError
from engine.errors import CONFLICT

MAX_FILE = 100 * 1024 * 1024  # документ до 100 МиБ


class FileStore:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def put(self, data: bytes) -> tuple[str, Path]:
        if not data:
            raise EngineError("file_empty", "Файл пуст")
        if len(data) > MAX_FILE:
            raise EngineError("file_too_large", f"Файл больше {MAX_FILE // (1024 * 1024)} МиБ")
        sha = hashlib.sha256(data).hexdigest()
        path = self.path(sha)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(f"{sha}.{os.getpid()}.part")
            part.write_bytes(data)
            os.replace(part, path)
        return sha, path

    def path(self, sha: str) -> Path:
        return self.folder / sha[:2] / sha

    def existing(self, sha: str) -> Path:
        path = self.path(sha)
        if not path.is_file():
            raise EngineError(
                "file_missing", "Файл документа не найден в хранилище", status=CONFLICT
            )
        return path
