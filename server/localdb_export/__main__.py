"""Перенос выгрузки Local_DB из Excel в LOCALAI с магнитом ККС.

Пробный прогон — ничего не записывает, считает всё как при переносе::

    python -m server.localdb_export выгрузка.xlsx --target localai.sqlite3

Перенос — после защитного снимка базы, одной транзакцией::

    python -m server.localdb_export выгрузка.xlsx --target localai.sqlite3 --apply

Отчёт (``--report``) содержит коды ККС и номера строк — храните его там же,
где и саму выгрузку.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from engine import EngineError, open_database, snapshot
from server.localdb_export.importer import run
from server.xlsx import read_sheet

MAX_ROWS = 100_000  # перенос — работа администратора; у мастера Excel предел 5000 (IMP-11)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.localdb_export", description=__doc__)
    parser.add_argument("source", help="книга Excel — выгрузка Local_DB")
    parser.add_argument("--sheet", help="лист; по умолчанию первый")
    parser.add_argument("--target", help="база LOCALAI; без неё пробный прогон идёт на пустой базе")
    parser.add_argument("--site", help="площадка; нужна, если в базе их несколько")
    parser.add_argument("--apply", action="store_true", help="выполнить перенос")
    parser.add_argument("--report", help="записать подробный отчёт JSON в файл")
    args = parser.parse_args(argv)
    if args.apply and not args.target:
        parser.error("для --apply нужен --target")
    try:
        sheet = read_sheet(Path(args.source), args.sheet, max_rows=MAX_ROWS)
        with tempfile.TemporaryDirectory(prefix="localai-export-") as work:
            report = _run(sheet, args, Path(work))
    except EngineError as error:
        print(f"Ошибка: {error.message}", file=sys.stderr)
        return 1
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(_text(report))
    return 2 if report["mode"] == "failed" else 0


def _run(sheet, args, work: Path):  # type: ignore[no-untyped-def]
    if not args.apply:
        # Пробный прогон идёт на копии: целевая база не меняется даже миграциями.
        copy = work / "target.sqlite3"
        if args.target:
            snapshot.copy_untouched(Path(args.target), copy)
        db = open_database(str(copy))
        try:
            return run(db, sheet, apply=False, site_name=args.site)
        finally:
            db.close()
    db = open_database(args.target)
    try:
        protective = snapshot.take(db.connect(), db.path, "before-export-import", None)
        report = run(db, sheet, apply=True, site_name=args.site)
        report["protective_snapshot"] = str(protective)
        return report
    finally:
        db.close()


def _text(report: dict) -> str:  # type: ignore[type-arg]
    title = {
        "dry_run": "Пробный прогон",
        "applied": "Перенос выполнен",
        "failed": "Перенос отменён",
    }
    lines = [
        f"{title[report['mode']]}: лист «{report['sheet']}», {report['rows']} строк, площадка «{report['site']}»"
    ]
    for mode, part in report["modes"].items():
        plan = part["plan"]
        if part["kind"] == "devices":
            counts = part["records"]
            lines.append(
                f"• {mode}: аппаратов {plan['devices']}; создано {counts['created']},"
                f" изменено {counts['updated']}, без изменений {counts['unchanged']};"
                f" расхождений {len(part['issues'])}"
            )
        else:
            signals, cables = part["records"]["signals"], part["records"]["cables"]
            lines.append(
                f"• {mode}: сигналов {plan['signals']}, резервных каналов {plan['spares']},"
                f" кабелей {plan['cables']}, аппаратов {plan['devices']}"
                f" (по столбцу ККС {plan['device_from_kks_column']},"
                f" по коду сигнала {plan['device_from_signal_code']});"
                f" пустых строк пропущено {plan['skipped_empty_rows']}"
            )
            lines.append(
                f"  сигналы: создано {signals['created']}, изменено {signals['updated']},"
                f" без изменений {signals['unchanged']}; кабели: создано {cables['created']},"
                f" изменено {cables['updated']}, без изменений {cables['unchanged']}"
            )
            if plan["devices_not_kks_like"]:
                lines.append(f"  не похожи на ККС: {len(plan['devices_not_kks_like'])} объектов")
            if part["issues"]:
                lines.append(f"  расхождения: {plan['issues']}")
    if report["write_errors"]:
        lines.append(f"Ошибки записи: {len(report['write_errors'])}; перенос не выполнен")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
