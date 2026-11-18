"""Встроенный читатель простых форматов: txt, md, csv, xlsx, docx (KNOW-01).

PDF, чертежи и сканы читает процесс схем (ТЗ 10, 18.2): сервер ставит им
задачу с исполнителем ``vision``. Здесь — только форматы, которые читаются
стандартной библиотекой и openpyxl без внешних программ.

Результат — страницы ``[{page, text}]``: у текста страница одна (или по
разрывам страницы ``\\f``), у книги Excel страница — лист.
"""

from __future__ import annotations

import zipfile
from pathlib import Path, PurePath
from typing import Any
from xml.etree import ElementTree

from openpyxl import load_workbook

from engine import EngineError

_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_UNZIPPED = 200 * 1024 * 1024  # защита от «zip-бомбы»


def read(path: Path, file_name: str) -> list[dict[str, Any]]:
    suffix = PurePath(file_name).suffix.lower()
    if suffix in (".txt", ".md", ".csv"):
        return _plain(path.read_bytes())
    if suffix == ".xlsx":
        return _xlsx(path)
    if suffix == ".docx":
        return _docx(path)
    raise EngineError("document_format", f"Сервер не читает формат {suffix}")


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _plain(data: bytes) -> list[dict[str, Any]]:
    body = _decode(data)
    parts = body.split("\f")
    return [{"page": str(number), "text": part} for number, part in enumerate(parts, start=1)]


def _xlsx(path: Path) -> list[dict[str, Any]]:
    _check_zip(path)
    pages = []
    # Файл в хранилище без расширения: openpyxl получает открытый файл, а не путь.
    with path.open("rb") as handle:
        try:
            book = load_workbook(handle, read_only=True, data_only=True)
        except Exception as error:  # noqa: BLE001 — openpyxl бросает разные ошибки формата
            raise EngineError("document_unreadable", f"Книга не читается: {error}") from None
        for sheet in book.worksheets:
            lines = []
            for row in sheet.iter_rows(values_only=True):
                cells = [str(value).strip() for value in row if value not in (None, "")]
                if cells:
                    lines.append(" | ".join(cells))
            pages.append({"page": sheet.title, "text": "\n".join(lines)})
        book.close()
    return pages


def _docx(path: Path) -> list[dict[str, Any]]:
    _check_zip(path)
    try:
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as error:
        raise EngineError("document_unreadable", f"Документ Word не читается: {error}") from None
    pages, lines = [], []
    for paragraph in root.iter(f"{_WORD}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{_WORD}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_WORD}tab":
                parts.append("\t")
            elif node.tag == f"{_WORD}br" and node.get(f"{_WORD}type") == "page":
                pages.append("\n\n".join(lines))
                lines = []
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    pages.append("\n\n".join(lines))
    return [{"page": str(number), "text": body} for number, body in enumerate(pages, start=1)]


def _check_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            total = sum(item.file_size for item in archive.infolist())
    except zipfile.BadZipFile:
        raise EngineError("document_unreadable", "Файл повреждён: это не архив Office") from None
    if total > MAX_UNZIPPED:
        raise EngineError("document_unreadable", "Распакованный файл больше 200 МиБ")
