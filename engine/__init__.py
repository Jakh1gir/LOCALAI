"""Хранение LOCALAI: схема базы, миграции, слой запросов, транзакции.

Пакет не зависит от ``server``, ``ai`` и ``vision`` (ARCH-01) и использует
только стандартную библиотеку Python.
"""

from engine.database import SYSTEM, Actor, Database, Tx
from engine.errors import EngineError
from engine.migrator import migrate

__all__ = ["SYSTEM", "Actor", "Database", "EngineError", "Tx", "migrate", "open_database"]

__version__ = "0.1.0"


def open_database(path: str) -> Database:
    """Открывает базу и доводит её схему до версии программы."""
    db = Database(path)
    migrate(db)
    return db
