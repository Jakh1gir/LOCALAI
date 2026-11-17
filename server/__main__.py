"""Запуск сервера: ``python -m server --db localai.sqlite3``."""

from __future__ import annotations

import argparse

import uvicorn

from server.app import create_app

DEFAULT_PORT = 8730


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m server", description="Сервер LOCALAI")
    parser.add_argument("--db", required=True, help="файл базы LOCALAI; создаётся, если его нет")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="адрес; до появления входа (этап 2) — только этот компьютер",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
