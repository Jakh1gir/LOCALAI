"""Импорт базы Local_DB в LOCALAI (ТЗ, раздел 14; MIG-01…MIG-15).

Запуск из командной строки: ``python -m server.localdb_import --help``.
"""

from server.localdb_import.importer import IMPORTER, Decisions, run

__all__ = ["IMPORTER", "Decisions", "run"]
