"""Настоящий сервер для браузерных сценариев на временной базе C-101."""

from __future__ import annotations

import argparse
import json
import secrets
import socket
from contextlib import closing
from pathlib import Path

import uvicorn

from engine import Actor, access, open_database
from server.app import create_app
from tools.generate import generate_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    generate_database(args.database, 10_000, 201)
    password = secrets.token_urlsafe(24)
    with (
        closing(open_database(args.database)) as db,
        db.write(Actor(None, "Браузерный стенд", "system")) as tx,
    ):
        access.create_admin(tx, "browser-admin", "Администратор стенда", password)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address, port = listener.getsockname()
        listener.listen()
        print(
            json.dumps(
                {"url": f"http://{address}:{port}", "login": "browser-admin", "password": password}
            ),
            flush=True,
        )
        server = uvicorn.Server(
            uvicorn.Config(create_app(args.database), log_level="error", access_log=False)
        )
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
