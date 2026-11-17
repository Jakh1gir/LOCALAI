"""Запуск сервера: ``python -m server --db localai.sqlite3``.

По умолчанию сервер слушает только этот компьютер. В сети (``--host 0.0.0.0``
или адрес сетевой карты) он работает только по HTTPS: без сертификата в папке
``tls`` рядом с базой запуск отклоняется (SEC-04). Сертификат создаёт
``python -m server.admin --db … create-certificate --host <имя или адрес>``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from engine import EngineError
from server import tls
from server.admin import tls_folder
from server.app import create_app

DEFAULT_PORT = 8730


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m server", description=__doc__)
    parser.add_argument("--db", required=True, help="файл базы LOCALAI; создаётся, если его нет")
    parser.add_argument(
        "--host", default="127.0.0.1", help="адрес; в сети — только с сертификатом HTTPS"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--tls-dir", help="папка с server.crt и server.key; по умолчанию tls рядом с базой"
    )
    parser.add_argument("--backups", help="папка копий; по умолчанию backups рядом с базой")
    args = parser.parse_args(argv)
    folder = Path(args.tls_dir) if args.tls_dir else tls_folder(args.db)
    try:
        options = tls.server_options(args.host, folder)
    except EngineError as error:
        sys.exit(f"Ошибка: {error.message}")
    app = create_app(args.db, backups=args.backups)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", **options)


if __name__ == "__main__":
    main()
