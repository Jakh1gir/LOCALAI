"""Резервные копии: зашифрованный пакет базы, список, восстановление (F-022, F-023, SEC-09).

Пакет ``*.localai-backup``:

- строка ``LOCALAI-BACKUP``, длина заголовка (4 байта) и заголовок JSON —
  время, версия схемы, ID базы, подпись, параметры ключа; заголовок читается
  без пароля, чтобы показать список копий;
- кадры по 1 МиБ исходной базы: сжатие zlib, затем AES-256-GCM. Номер кадра и
  признак последнего кадра входят в одноразовое число, заголовок — в
  проверяемые данные. Подмена заголовка, перестановка, обрезка или
  дописывание кадров обнаруживаются при восстановлении.

Ключ получается из пароля через Argon2id. Перед восстановлением текущее
состояние упаковывается тем же паролем в защитную копию: ошибочное
восстановление отменяется восстановлением из неё.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
import zlib
from pathlib import Path
from typing import Any, BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

import engine
from engine import Actor, Database, EngineError, migrator, passwords, snapshot, text
from engine.errors import NOT_FOUND

MAGIC = b"LOCALAI-BACKUP\n"
SUFFIX = ".localai-backup"
FORMAT = 1
CHUNK = 1024 * 1024
# 64 МиБ памяти и три прохода на каждую попытку подобрать пароль.
KDF = {"name": "argon2id", "memory_kib": 64 * 1024, "iterations": 3, "lanes": 4}
PROTECTIVE_LABEL = "Перед восстановлением"
_NAME_RE = re.compile(r"^localai-[0-9A-Za-z-]{1,80}\.localai-backup$")
_MAX_HEADER = 64 * 1024


def create(
    db: Database, folder: Path, password: str, label: str | None, actor: Actor
) -> dict[str, Any]:
    """Новая зашифрованная копия базы. Чтение базы не мешает работе других."""
    passwords.check_new(password)
    shown = _label(label)
    conn = db.connect()
    path = _pack(conn, db.path, folder, password, shown)
    with db.write(actor) as tx:
        tx.audit(
            "backup.create",
            "backup",
            None,
            details={"file": path.name, "label": shown, "size": path.stat().st_size},
        )
    return describe(path)


def list_backups(folder: Path) -> list[dict[str, Any]]:
    """Копии в папке, от новых к старым. Нечитаемый файл показывается с ошибкой."""
    if not folder.exists():
        return []
    found = []
    for path in folder.glob(f"*{SUFFIX}"):
        try:
            found.append(describe(path))
        except EngineError as error:
            found.append({"name": path.name, "size": path.stat().st_size, "error": error.message})
    return sorted(found, key=lambda item: item.get("created_at", ""), reverse=True)


def describe(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        _, header = _read_header(handle)
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "created_at": header["created_at"],
        "label": header.get("label"),
        "schema_version": header["schema_version"],
        "database_id": header.get("database_id"),
        "app_version": header.get("app_version"),
    }


def file_path(folder: Path, name: str) -> Path:
    """Путь к копии по имени; имя не может вывести за пределы папки копий."""
    if not _NAME_RE.match(name) or not (folder / name).is_file():
        raise EngineError(
            "backup_not_found", f"Копия «{name}» не найдена", status=NOT_FOUND, field="name"
        )
    return folder / name


def restore(db: Database, folder: Path, name: str, password: str, actor: Actor) -> dict[str, Any]:
    """Заменяет базу содержимым копии (SEC-09).

    1. Копия расшифровывается во временный файл и проверяется: целостность,
       это база LOCALAI, её схема не новее программы. Пока проверка не
       пройдена, рабочая база не тронута.
    2. Текущее состояние упаковывается тем же паролем в защитную копию.
    3. Содержимое переносится в рабочую базу через API резервного
       копирования SQLite: другие соединения сразу видят новую базу.
    4. Схема доводится до текущей версии, все сессии закрываются: входить
       нужно заново, по учётным записям из копии.
    """
    source = file_path(folder, name)
    work = _work(folder)
    plain = work / f"restore-{_stamp()}.sqlite3"
    try:
        header = _decrypt(source, plain, password)
        restored_schema = _check_restorable(plain)
        with db.exclusive() as conn:
            protective = _pack(conn, db.path, folder, password, PROTECTIVE_LABEL)
            copy = sqlite3.connect(plain)
            try:
                copy.backup(conn)
            finally:
                copy.close()
        migrator.migrate(db)
        with db.write(actor) as tx:
            tx.conn.execute("DELETE FROM sessions")
            tx.audit(
                "backup.restore",
                "backup",
                None,
                details={
                    "file": name,
                    "created_at": header["created_at"],
                    "schema_version": restored_schema,
                    "protective_backup": protective.name,
                },
            )
    finally:
        plain.unlink(missing_ok=True)
    return {
        "restored": describe(source),
        "protective_backup": describe(protective),
        "schema_version": migrator.current_version(db.connect()),
    }


# Упаковка -----------------------------------------------------------------------


def _pack(
    conn: sqlite3.Connection, db_path: str, folder: Path, password: str, label: str | None
) -> Path:
    work = _work(folder)
    plain = snapshot.take(conn, db_path, "backup", work)  # проверенная копия
    target = _free_name(folder)
    part = target.with_name(target.name + ".part")
    try:
        meta = _meta(plain)
        header = {
            "format": FORMAT,
            "created_at": text.now(),
            "app_version": engine.__version__,
            "label": label,
            **meta,
            "size": plain.stat().st_size,
            "kdf": {**KDF, "salt": _b64(secrets.token_bytes(16))},
            "nonce": _b64(secrets.token_bytes(7)),
        }
        _encrypt(plain, part, header, password)
        os.replace(part, target)
    finally:
        plain.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    return target


def _encrypt(plain: Path, target: Path, header: dict[str, Any], password: str) -> None:
    raw_header = json.dumps(header, ensure_ascii=False, sort_keys=True).encode()
    cipher = AESGCM(_key(password, header["kdf"]))
    prefix = _unb64(header["nonce"])
    with plain.open("rb") as source, target.open("wb") as out:
        out.write(MAGIC + struct.pack(">I", len(raw_header)) + raw_header)
        number = 0
        chunk = source.read(CHUNK)
        while True:
            following = source.read(CHUNK)
            last = not following
            sealed = cipher.encrypt(
                _nonce(prefix, number, last), zlib.compress(chunk, 6), raw_header
            )
            out.write(struct.pack(">BI", int(last), len(sealed)) + sealed)
            if last:
                break
            chunk, number = following, number + 1
        out.flush()
        os.fsync(out.fileno())


def _decrypt(source: Path, target: Path, password: str) -> dict[str, Any]:
    with source.open("rb") as handle, target.open("wb") as out:
        raw_header, header = _read_header(handle)
        cipher = AESGCM(_key(password, header["kdf"]))
        prefix = _unb64(header["nonce"])
        number, finished = 0, False
        while frame := handle.read(5):
            if finished or len(frame) != 5:
                raise _corrupt()
            last, size = struct.unpack(">BI", frame)
            sealed = handle.read(size)
            if len(sealed) != size or last not in (0, 1) or size > 2 * CHUNK:
                raise _corrupt()
            try:
                packed = cipher.decrypt(_nonce(prefix, number, bool(last)), sealed, raw_header)
            except InvalidTag:
                if number == 0:
                    raise EngineError(
                        "backup_password",
                        "Неверный пароль копии или файл копии повреждён",
                        field="password",
                    ) from None
                raise _corrupt() from None
            out.write(zlib.decompress(packed))
            number, finished = number + 1, bool(last)
        if not finished:
            raise _corrupt()
    return header


def _read_header(handle: BinaryIO) -> tuple[bytes, dict[str, Any]]:
    if handle.read(len(MAGIC)) != MAGIC:
        raise EngineError("backup_format", "Файл не является копией LOCALAI")
    (size,) = struct.unpack(">I", handle.read(4) or b"\0\0\0\0")
    raw = handle.read(size)
    if not 0 < size <= _MAX_HEADER or len(raw) != size:
        raise _corrupt()
    try:
        header = json.loads(raw)
    except ValueError:
        raise _corrupt() from None
    if header.get("format") != FORMAT:
        raise EngineError(
            "backup_format", "Копия сделана другой версией формата; обновите программу"
        )
    return raw, header


def _check_restorable(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise _corrupt()
        try:
            version = migrator.current_version(conn)
            conn.execute("SELECT 1 FROM app_meta").fetchone()
        except sqlite3.Error:
            version = 0
    finally:
        conn.close()
    if version == 0:
        raise EngineError("backup_format", "В копии нет базы LOCALAI")
    latest = migrator.available()[-1][0]
    if version > latest:
        raise EngineError(
            "schema_newer",
            f"Копия сделана более новой версией программы (схема {version},"
            f" программа знает {latest}). Обновите программу.",
        )
    return version


def _meta(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key = 'database_id'").fetchone()
        return {
            "schema_version": migrator.current_version(conn),
            "database_id": row[0] if row else None,
        }
    finally:
        conn.close()


def _key(password: str, kdf: dict[str, Any]) -> bytes:
    if kdf.get("name") != "argon2id":
        raise EngineError("backup_format", "Неизвестный способ получения ключа копии")
    return Argon2id(
        salt=_unb64(kdf["salt"]),
        length=32,
        iterations=int(kdf["iterations"]),
        lanes=int(kdf["lanes"]),
        memory_cost=int(kdf["memory_kib"]),
    ).derive(password.encode())


def _nonce(prefix: bytes, number: int, last: bool) -> bytes:
    return prefix + struct.pack(">IB", number, int(last))


def _free_name(folder: Path) -> Path:
    stamp = _stamp()
    target = folder / f"localai-{stamp}{SUFFIX}"
    number = 2
    while target.exists():
        target = folder / f"localai-{stamp}-{number}{SUFFIX}"
        number += 1
    return target


def _work(folder: Path) -> Path:
    """Рабочая папка для открытых копий базы: рядом с копиями, на том же диске."""
    work = folder / ".work"
    work.mkdir(parents=True, exist_ok=True)
    return work


def clean_work(folder: Path) -> None:
    """Открытые копии, оставшиеся после сбоя, удаляются при запуске сервера."""
    shutil.rmtree(folder / ".work", ignore_errors=True)


def _label(label: str | None) -> str | None:
    if label is None or not label.strip():
        return None
    return text.clean_name(label, "Подпись", limit=200)


def _stamp() -> str:
    return text.now().replace(":", "").replace("-", "").replace(".", "")


def _corrupt() -> EngineError:
    return EngineError("backup_corrupt", "Файл копии повреждён или обрезан")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(value: str) -> bytes:
    return base64.b64decode(value)
