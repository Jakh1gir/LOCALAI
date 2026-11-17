"""Перенос базы Local_DB из командной строки.

Пробный прогон на копии рабочей базы, ничего не записывая::

    python -m server.localdb_import "%LOCALAPPDATA%\\Local_DB\\data"

Перенос в базу LOCALAI::

    python -m server.localdb_import ИСТОЧНИК --target localai.sqlite3 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from engine import EngineError, open_database, snapshot
from server.localdb_import.importer import Decisions, run
from server.localdb_import.report import as_text
from server.localdb_import.source import DATABASE_FILE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m server.localdb_import",
        description="Перенос базы Local_DB в LOCALAI. Без --apply выполняется пробный прогон.",
    )
    parser.add_argument("source", help="каталог данных Local_DB или файл local_db.sqlite3")
    parser.add_argument("--target", help="база LOCALAI; без неё пробный прогон идёт на пустой базе")
    parser.add_argument(
        "--apply", action="store_true", help="выполнить перенос, а не пробный прогон"
    )
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
    if args.report and _touches_databases(Path(args.report), args.source, args.target):
        parser.error("файл отчёта совпадает с базой Local_DB или LOCALAI; укажите другой путь")
    with tempfile.TemporaryDirectory(prefix="localai-dry-") as scratch:
        try:
            target = _target(args.target, args.apply, Path(scratch))
            db = open_database(target)
            try:
                report = run(db, args.source, decisions, apply=args.apply)
            finally:
                db.close()
        except EngineError as error:
            print(f"Ошибка: {error.message}", file=sys.stderr)
            return 2
    if not args.apply and args.target:
        note = "копии базы LOCALAI" if Path(args.target).exists() else "пустой базе"
        print(f"Пробный прогон выполнен на {note}; файл {args.target} не изменён.\n")
    print(as_text(report))
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nПолный отчёт: {args.report}")
    return 0 if report["mode"] != "refused" else 1


def _target(target: str | None, apply: bool, scratch: Path) -> str:
    """База для работы. Пробный прогон никогда не пишет в указанный файл.

    При пробном прогоне существующая база копируется во временный каталог,
    а несуществующая не создаётся: прогон идёт на пустой временной базе.
    """
    if apply:
        assert target is not None
        return target
    copy = scratch / "localai.sqlite3"
    if target and Path(target).exists():
        try:
            snapshot.copy_untouched(Path(target), copy)
        except sqlite3.Error as error:
            raise EngineError(
                "target_unreadable", f"Не удалось прочитать базу LOCALAI {target}: {error}"
            ) from error
    return str(copy)


def _touches_databases(report: Path, source: str, target: str | None) -> bool:
    """Отчёт попал бы в файл базы или её служебный файл.

    Сравниваются и пути, и сами файлы: жёсткая ссылка или другое имя того же
    файла тоже считается совпадением.
    """
    if report.exists() and report.is_dir():
        return True
    bases = [Path(source)]
    if Path(source).is_dir():
        bases.append(Path(source) / DATABASE_FILE)
    if target:
        bases.append(Path(target))
    for base in bases:
        for suffix in ("", "-wal", "-shm"):
            taken = base.with_name(base.name + suffix)
            if report.resolve() == taken.resolve():
                return True
            if report.exists() and taken.exists() and os.path.samefile(report, taken):
                return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
