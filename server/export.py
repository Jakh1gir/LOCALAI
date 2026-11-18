"""Выгрузка реестра в Excel, CSV и JSON (ТЗ 6.3, EXP-01…05, EXP-08, UI-14).

- Отбор, поиск и сортировка — тем же языком запросов, что у таблицы
  (API-04): выгружается то, что пользователь видит с текущими фильтрами.
- Выгрузка для правки (``template``, по умолчанию) начинается скрытыми
  столбцами «UUID» и «Версия». Мастер импорта узнаёт их и обновляет те же
  записи, а правку после выгрузки показывает спорной строкой (EXP-02, IMP-01).
- Значения выгружаются так, как их понимает импорт: справочник — названием,
  ККС — кодом, ссылка на запись — ключом цели (или UUID, если ключа у типа
  нет), несколько значений — через «; », дата — датой Excel.
- Скрытые от роли поля не выгружаются (EXP-04).
- Текст, который Excel мог бы исполнить как формулу, остаётся текстом:
  в книге — ячейкой-строкой, в CSV — с апострофом впереди (EXP-05).
- Группировка (UI-14): строки идут по полю группы, общие для группы
  значения выводятся объединёнными ячейками, как в рабочих таблицах станции.
  Мастер импорта заполняет объединённые ячейки обратно (IMP-17).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from typing import Any
from urllib.parse import quote

from openpyxl import Workbook
from openpyxl.cell import MergedCell, WriteOnlyCell
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.merge import MergedCellRange

from engine import EngineError, query, structure, text
from engine.access import Access

MAX_ROWS = 100_000  # выгрузка без группировки пишется потоком
MAX_GROUPED = 20_000  # объединённые ячейки требуют книгу целиком в памяти
PAGE = 200
ID_HEADER, VERSION_HEADER = "UUID", "Версия"
SEPARATOR = "; "
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
MEDIA = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
}


def export(
    conn: Any, access: Access, dataset_id: str, request: dict[str, Any]
) -> tuple[bytes, str, str]:
    """Файл выгрузки: содержимое, имя файла, тип."""
    dataset = structure.get_dataset(conn, dataset_id)
    schema = structure.load_schema(conn, dataset["type_id"])
    hidden = access.hidden_keys(schema)
    fmt = request.get("format", "xlsx")
    columns = _columns(schema, hidden, request.get("columns"))
    titles = {item.key: item.name for item in columns} | dict(request.get("headers") or {})
    group = _group(schema, hidden, request.get("group_by"), columns)
    template = request.get("template", True)
    limit = MAX_GROUPED if group and fmt == "xlsx" else MAX_ROWS
    asked = dict(request.get("query") or {})
    if request.get("sort"):
        asked["sort"] = request["sort"]
    rows = list(_rows(conn, dataset_id, schema, hidden, columns, asked, group, limit))
    stamp = text.now()[:10]
    name = f"{dataset['name']} {stamp}"
    if fmt == "json":
        body = _json(dataset, columns, titles, rows)
    elif fmt == "csv":
        body = _csv(columns, titles, rows, template)
    else:
        body = _xlsx(dataset["name"], columns, titles, rows, template, group)
    return body, f"{name}.{fmt}", MEDIA[fmt]


def disposition(filename: str) -> str:
    """Имя файла по-русски для браузера (RFC 6266) и запасное латиницей."""
    plain = "".join(ch if ch.isascii() and ch.isalnum() or ch in "._-" else "_" for ch in filename)
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(filename)}"


# Отбор строк -----------------------------------------------------------------------


def _columns(
    schema: structure.TypeSchema, hidden: frozenset[str], wanted: list[str] | None
) -> list[structure.Field]:
    active = [item for item in schema.active() if item.key not in hidden]
    if not wanted:
        return active
    by_key = {item.key: item for item in active}
    unknown = [key for key in wanted if key not in by_key]
    if unknown:
        raise EngineError(
            "export_columns",
            f"Таких полей нет или они скрыты: {', '.join(unknown)}",
            field="columns",
        )
    if len(set(wanted)) != len(wanted):
        raise EngineError("export_columns", "Поле указано дважды", field="columns")
    return [by_key[key] for key in wanted]


def _group(
    schema: structure.TypeSchema,
    hidden: frozenset[str],
    key: str | None,
    columns: list[structure.Field],
) -> structure.Field | None:
    if not key:
        return None
    found = schema.by_key(key)
    if found is None or found.archived_at or key in hidden:
        raise EngineError("export_group", f"Поля «{key}» нет или оно скрыто", field="group_by")
    if found.multiple:
        raise EngineError(
            "export_group", "Группировать по полю с несколькими значениями нельзя", field="group_by"
        )
    if found not in columns:
        raise EngineError(
            "export_group", "Поле группы должно быть среди столбцов", field="group_by"
        )
    return found


def _rows(
    conn: Any,
    dataset_id: str,
    schema: structure.TypeSchema,
    hidden: frozenset[str],
    columns: list[structure.Field],
    request: dict[str, Any],
    group: structure.Field | None,
    limit: int,
) -> Any:
    """Строки выгрузки в порядке таблицы; при группировке — по полю группы."""
    asked = {key: value for key, value in request.items() if key not in ("limit", "cursor")}
    if group is not None:
        asked["sort"] = {"field": group.key, "dir": "asc"}
    targets = _record_keys(conn, columns)
    cursor, count = None, 0
    while True:
        page = query.list_records(
            conn,
            dataset_id,
            {**asked, "limit": PAGE, **({"cursor": cursor} if cursor else {})},
            hidden=hidden,
        )
        for item in page["items"]:
            count += 1
            if count > limit:
                raise EngineError(
                    "export_too_large",
                    f"В выгрузке больше {limit} строк; сузьте отбор"
                    + (" или выгрузите без группировки" if group is not None else ""),
                )
            yield {
                "id": item["id"],
                "version": item["version"],
                "values": {
                    field.key: _shown(field, item["doc"].get(field.key), page["refs"], targets)
                    for field in columns
                },
            }
        cursor = page["next_cursor"]
        if cursor is None:
            return


def _record_keys(conn: Any, columns: list[structure.Field]) -> dict[str, bool]:
    """Есть ли у типа-цели ссылки уникальное поле: тогда ссылка выгружается ключом."""
    found = {}
    for field in columns:
        if field.kind == "record" and field.target_type_id:
            target = structure.load_schema(conn, field.target_type_id)
            found[field.key] = any(item.unique_values for item in target.active())
    return found


def _shown(
    field: structure.Field, value: Any, refs: dict[str, Any], targets: dict[str, bool]
) -> Any:
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    if field.kind == "choice":
        items = [refs["choices"].get(item, item) for item in items]
    elif field.kind == "kks":
        items = [refs["kks"].get(item, item) for item in items]
    elif field.kind == "record":
        items = [
            refs["records"].get(item, item) if targets.get(field.key) else item for item in items
        ]
    elif field.kind == "date":
        items = [date.fromisoformat(item) for item in items]
    if field.multiple:
        return SEPARATOR.join(str(item) for item in items)
    return items[0]


# Форматы ------------------------------------------------------------------------


def _xlsx(
    title: str,
    columns: list[structure.Field],
    titles: dict[str, str],
    rows: list[dict[str, Any]],
    template: bool,
    group: structure.Field | None,
) -> bytes:
    """Книга: заголовок жирным, закреплённая шапка, фильтр; служебные столбцы скрыты."""
    book = Workbook(write_only=group is None)
    page = book.create_sheet(_sheet_title(title)) if group is None else book.active
    if group is not None:
        page.title = _sheet_title(title)
    lead = [ID_HEADER, VERSION_HEADER] if template else []
    headers = [*lead, *(titles[item.key] for item in columns)]
    for index, header in enumerate(headers, start=1):
        letter = get_column_letter(index)
        page.column_dimensions[letter].width = min(max(len(str(header)) + 4, 12), 50)
        if index <= len(lead):
            page.column_dimensions[letter].hidden = True
    page.freeze_panes = f"{get_column_letter(len(lead) + 1)}2"
    bold = Font(bold=True)
    if group is None:
        page.append([_cell(page, header, bold) for header in headers])
        for row in rows:
            page.append([_cell(page, value) for value in _line(row, columns, template)])
    else:
        for index, header in enumerate(headers, start=1):
            page.cell(1, index, header).font = bold
        # Номер строки считается здесь: page.max_row перебирает все ячейки листа.
        for number, row in enumerate(rows, start=2):
            for index, value in enumerate(_line(row, columns, template), start=1):
                _set(page.cell(number, index), value)
        _merge_groups(page, rows, columns, group, len(lead))
    page.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _line(row: dict[str, Any], columns: list[structure.Field], template: bool) -> list[Any]:
    lead = [row["id"], row["version"]] if template else []
    return [*lead, *(row["values"][item.key] for item in columns)]


def _cell(page: Any, value: Any, font: Font | None = None) -> Any:
    cell = WriteOnlyCell(page, value=value)
    if isinstance(value, str):
        cell.data_type = "s"  # «=…» остаётся текстом, а не формулой (EXP-05)
    if font is not None:
        cell.font = font
    return cell


def _set(cell: Any, value: Any) -> None:
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"


def _merge_groups(
    page: Any,
    rows: list[dict[str, Any]],
    columns: list[structure.Field],
    group: structure.Field,
    offset: int,
) -> None:
    """Значения, общие для всех строк группы, — одна объединённая ячейка (UI-14)."""
    start = 0
    top = Alignment(vertical="top")
    while start < len(rows):
        value = rows[start]["values"][group.key]
        end = start
        while end + 1 < len(rows) and rows[end + 1]["values"][group.key] == value:
            end += 1
        if end > start and value is not None:
            for index, field in enumerate(columns, start=offset + 1):
                shared = {repr(rows[i]["values"][field.key]) for i in range(start, end + 1)}
                if len(shared) == 1 and rows[start]["values"][field.key] is not None:
                    _merge(page, start + 2, end + 2, index)
                    page.cell(start + 2, index).alignment = top
        start = end + 1


def _merge(page: Any, first: int, last: int, column: int) -> None:
    """Объединение без проверки пересечений: диапазоны групп не пересекаются.

    ``merge_cells`` openpyxl сверяет новый диапазон со всеми прежними, и на
    тысячах групп это квадратичное время.
    """
    letter = get_column_letter(column)
    merged = MergedCellRange(page, f"{letter}{first}:{letter}{last}")
    page.merged_cells.ranges.add(merged)
    for row in range(first + 1, last + 1):
        page._cells[row, column] = MergedCell(page, row, column)  # как в merge_cells openpyxl


def _csv(
    columns: list[structure.Field],
    titles: dict[str, str],
    rows: list[dict[str, Any]],
    template: bool,
) -> bytes:
    """CSV для Excel по-русски: «;» между столбцами, UTF-8 с BOM."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    lead = [ID_HEADER, VERSION_HEADER] if template else []
    writer.writerow([_safe(item) for item in [*lead, *(titles[field.key] for field in columns)]])
    for row in rows:
        writer.writerow(
            [_safe(_plain(value, words=True)) for value in _line(row, columns, template)]
        )
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def _json(
    dataset: dict[str, Any],
    columns: list[structure.Field],
    titles: dict[str, str],
    rows: list[dict[str, Any]],
) -> bytes:
    """Открытая выгрузка для других систем (EXP-08): значения в том виде, что видит человек."""
    data = {
        "dataset": {"id": dataset["id"], "name": dataset["name"]},
        "exported_at": text.now(),
        "fields": [
            {
                "key": item.key,
                "name": titles[item.key],
                "kind": item.kind,
                "multiple": item.multiple,
            }
            for item in columns
        ],
        "records": [
            {
                "id": row["id"],
                "version": row["version"],
                "values": {
                    key: _plain(value) for key, value in row["values"].items() if value is not None
                },
            }
            for row in rows
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def _plain(value: Any, *, words: bool = False) -> Any:
    """Дата — строкой ISO; «да»/«нет» вместо логического значения — для CSV."""
    if isinstance(value, date):
        return value.isoformat()
    if words and isinstance(value, bool):
        return "да" if value else "нет"
    return value


def _safe(value: Any) -> Any:
    """Апостроф перед текстом, который Excel принял бы за формулу (EXP-05)."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _sheet_title(name: str) -> str:
    cleaned = "".join("_" if ch in "[]:*?/\\" else ch for ch in name).strip() or "Реестр"
    return cleaned[:31]
