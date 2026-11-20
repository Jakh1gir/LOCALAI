"""Проверка набора вопросов и получение эталонных ответов без модели ИИ."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .format import DEFAULT_SET, QuestionError, load_suite
from .runner import run_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.questions", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="проверить формат и запросы по контракту")
    run = commands.add_parser("run", help="проверить набор и выполнить запросы на копии базы")
    for command in (check, run):
        command.add_argument("--set", type=Path, default=DEFAULT_SET, help="набор вопросов JSON")
    run.add_argument(
        "--db", type=Path, required=True, help="исходная база; работа идёт на временной копии"
    )
    run.add_argument(
        "--profile", default="c101", help="профиль набора, по умолчанию c101; all — все профили"
    )
    run.add_argument(
        "--out", type=Path, default=Path("questions-reference.json"), help="новый файл эталона"
    )
    args = parser.parse_args(argv)
    try:
        suite = load_suite(args.set)
        if args.command == "check":
            print(
                f"Набор проверен: {len(suite.questions)} вопросов, профили {', '.join(suite.profiles)}."
            )
            return 0
        if args.out.exists() or args.out.is_symlink():
            raise QuestionError("Файл эталона уже существует; укажите новый --out")
        sources = {args.db.absolute(), args.db.resolve()}
        reserved = {
            source.with_name(source.name + suffix).resolve()
            for source in sources
            for suffix in ("", "-wal", "-shm", "-journal")
        }
        if args.out.resolve() in reserved:
            raise QuestionError("Эталон не может занять путь базы или её служебного файла")
        if not args.out.parent.is_dir():
            raise QuestionError("Каталог эталона не существует")
        profiles = list(suite.profiles) if args.profile == "all" else [args.profile]
        report = run_suite(suite, args.db, profiles)
        with args.out.open("x", encoding="utf-8") as target:
            json.dump(report, target, ensure_ascii=False, sort_keys=True, indent=2)
            target.write("\n")
        summary = report["summary"]
        print(
            f"Проверено: {summary['ok']}; ошибок: {summary['failed']}; ручных: {summary['manual']}; вне профиля: {report['excluded']}. Эталон: {args.out}"
        )
        return 1 if summary["failed"] else 0
    except (QuestionError, OSError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
