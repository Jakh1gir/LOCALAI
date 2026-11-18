"""Перенос выгрузки Local_DB из Excel по модели «один ККС — много строк» (K-302)."""

from server.localdb_export.importer import run

__all__ = ["run"]
