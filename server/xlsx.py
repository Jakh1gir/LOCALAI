"""Чтение листа Excel в строки ``{заголовок: значение}`` (IMP-11, IMP-17).

Объединённые ячейки заполняются значением верхней левой ячейки на весь
диапазон: в рабочих таблицах станции ККС и шкаф часто объединены на все
строки сигналов аппарата, а у остальных строк ячейка пуста.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from engine import EngineError

MAX_BYTES = 25 * 1024 * 1024
MAX_SHEETS = 20
MAX_COLUMNS = 200


@dataclass
class Sheet:
    name: str
    headers: list[str]
    rows: list[tuple[int, dict[str, Any]]]  # номер строки листа, значения


PREVIEW_ROWS = 30  # строк листа для выбора строки заголовков
SAMPLE_ROWS = 20  # строк примера после заголовков (ТЗ 6.1, шаг 1)


def read_sheet(
    path: Path, sheet: str | None = None, *, max_rows: int = 5000, header_row: int = 1
) -> Sheet:
    """Лист книги: строка ``header_row`` — заголовки, ниже — данные; пустые строки пропускаются."""
    book = _open(path)
    if sheet is not None and sheet not in book.sheetnames:
        raise EngineError("xlsx_sheet", f"Листа «{sheet}» нет; есть: {', '.join(book.sheetnames)}")
    page = book[sheet] if sheet else book.worksheets[0]
    grid = _grid(page)
    if not grid:
        raise EngineError("xlsx_empty", "Лист пуст")
    if not 1 <= header_row <= min(len(grid), PREVIEW_ROWS):
        raise EngineError(
            "xlsx_header_row",
            f"Строка заголовков — от 1 до {min(len(grid), PREVIEW_ROWS)}",
            field="header_row",
        )
    headers = _headers(grid[header_row - 1])
    rows = []
    for number, values in enumerate(grid[header_row:], start=header_row + 1):
        row = {name: _plain(value) for name, value in zip(headers, values, strict=False) if name}
        if any(value not in (None, "") for value in row.values()):
            rows.append((number, row))
    if len(rows) > max_rows:
        raise EngineError(
            "xlsx_too_many_rows", f"На листе больше {max_rows} непустых строк ({len(rows)})"
        )
    return Sheet(page.title, [name for name in headers if name], rows)


def outline(path: Path) -> list[dict[str, Any]]:
    """Листы книги для первого шага мастера: размер, начало листа, догадка о заголовках."""
    found = []
    for page in _open(path).worksheets:
        grid = _grid(page)
        top = [[_shown(value) for value in row] for row in grid[:PREVIEW_ROWS]]
        width = max((len(row) for row in top), default=0)
        found.append(
            {
                "name": page.title,
                "rows": len(grid),
                "columns": page.max_column if grid else 0,
                "top": [row + [None] * (width - len(row)) for row in top],
                "header_row": guess_header_row(grid),
            }
        )
    return found


def guess_header_row(grid: list[list[Any]]) -> int:
    """Строка, где больше всего непустых текстовых ячеек среди первых десяти."""
    best, score = 1, -1
    for number, row in enumerate(grid[:10], start=1):
        texts = sum(1 for value in row if isinstance(value, str) and value.strip())
        if texts > score:
            best, score = number, texts
    return best


def _open(path: Path) -> Any:
    if not path.is_file():
        raise EngineError("xlsx_missing", f"Файл {path} не найден")
    if path.stat().st_size > MAX_BYTES:
        raise EngineError("xlsx_too_large", "Книга больше 25 МиБ")
    try:
        book = load_workbook(path, data_only=True)
    except Exception as error:  # noqa: BLE001 — openpyxl бросает разные ошибки формата
        raise EngineError("xlsx_invalid", f"Файл не читается как книга Excel: {error}") from None
    if len(book.worksheets) > MAX_SHEETS:
        raise EngineError("xlsx_too_many_sheets", f"В книге больше {MAX_SHEETS} листов")
    for page in book.worksheets:
        if page.max_column > MAX_COLUMNS:
            raise EngineError(
                "xlsx_too_wide", f"На листе «{page.title}» больше {MAX_COLUMNS} столбцов"
            )
    return book


def _grid(page: Any) -> list[list[Any]]:
    filled = _merged_values(page)
    grid = [
        [filled.get((number, column), cell) for column, cell in enumerate(row, start=1)]
        for number, row in enumerate(page.iter_rows(values_only=True), start=1)
    ]
    while grid and all(value in (None, "") for value in grid[-1]):
        grid.pop()
    return grid


def _shown(value: Any) -> Any:
    shown = _plain(value)
    return shown if shown is None or isinstance(shown, (int, float, bool)) else str(shown)


def _merged_values(page: Any) -> dict[tuple[int, int], Any]:
    values: dict[tuple[int, int], Any] = {}
    for area in page.merged_cells.ranges:
        top = page.cell(area.min_row, area.min_col).value
        for row in range(area.min_row, area.max_row + 1):
            for column in range(area.min_col, area.max_col + 1):
                values[(row, column)] = top
    return values


def _headers(raw: list[Any]) -> list[str]:
    headers: list[str] = []
    seen: set[str] = set()
    for value in raw:
        name = str(value).strip() if value is not None else ""
        if name in seen:
            raise EngineError("xlsx_header_duplicate", f"Заголовок «{name}» повторяется")
        if name:
            seen.add(name)
        headers.append(name)
    return headers


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return (
            value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
