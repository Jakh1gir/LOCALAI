"""Перенос базы Local_DB из командной строки.

Пробный прогон на копии рабочей базы, ничего не записывая::

    python -m server.localdb_import "%LOCALAPPDATA%\\Local_DB\\data"

Перенос в базу LOCALAI::

    python -m server.localdb_import ИСТОЧНИК --target localai.sqlite3 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from engine import EngineError, open_database
from server.localdb_import.importer import Decisions, run
from server.localdb_import.report import as_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m server.localdb_import",
        description="Перенос базы Local_DB в LOCALAI. Без --apply выполняется пробный прогон.",
    )
    parser.add_argument("source", help="каталог данных Local_DB или файл local_db.sqlite3")
    parser.add_argument("--target", help="база LOCALAI; без неё пробный прогон идёт на пустой базе")
    parser.add_argument("--apply", action="store_true", help="выполнить перенос, а не пробный прогон")
    parser.add_argument("--site-name", help="площадка LOCALAI, на которую переносить")
    parser.add_argument("--site-id", help="ID существующей площадки LOCALAI")
    parser.add_argument("--no-audit", action="store_true", help="не переносить журнал действий")
    parser.add_argument("--report", help="сохранить полный отчёт в файл JSON")
    args = parser.parse_args(argv)
    if args.apply and not args.target:
        parser.error("для --apply нужна база LOCALAI: --target")
    decisions = Decisions(
        site_id=args.site_id, site_name=args.site_name, include_audit=not args.no_audit
    )
    with tempfile.TemporaryDirectory(prefix="localai-dry-") as scratch:
        target = args.target or str(Path(scratch) / "localai.sqlite3")
        try:
            db = open_database(target)
            try:
                report = run(db, args.source, decisions, apply=args.apply)
            finally:
                db.close()
        except EngineError as error:
            print(f"Ошибка: {error.message}", file=sys.stderr)
            return 2
    print(as_text(report))
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nПолный отчёт: {args.report}")
    return 0 if report["mode"] != "refused" else 1


if __name__ == "__main__":
    raise SystemExit(main())
