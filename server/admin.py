"""Учётные записи из командной строки сервера.

``python -m server.admin --db localai.sqlite3 create-admin --login admin --name "Иванов И. И."``
создаёт первого администратора: стартовых паролей в программе нет (SEC-03),
пароль задаёт установщик — вводом дважды или одной строкой через
``--password-stdin``.

``reset-password --login …`` выдаёт временный пароль, если единственный
администратор забыл свой. Команда доступна только тому, у кого есть доступ к
файлу базы на сервере, и пишется в журнал.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from engine import Actor, EngineError, access, open_database

CONSOLE = Actor(None, "Командная строка сервера", "system")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.admin", description=__doc__)
    parser.add_argument("--db", required=True, help="файл базы LOCALAI")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="создать администратора")
    create.add_argument("--login", required=True)
    create.add_argument("--name", required=True, help="имя для журнала")
    create.add_argument(
        "--password-stdin", action="store_true", help="прочитать пароль из первой строки ввода"
    )
    reset = commands.add_parser("reset-password", help="выдать временный пароль")
    reset.add_argument("--login", required=True)
    args = parser.parse_args(argv)
    db = open_database(args.db)
    try:
        if args.command == "create-admin":
            password = _password(args.password_stdin)
            with db.write(CONSOLE) as tx:
                user = access.create_admin(tx, args.login, args.name, password)
            print(f"Администратор «{user['login']}» создан, роль «{user['role_name']}».")
        else:
            with db.write(CONSOLE) as tx:
                user_id = _user_id(tx.conn, args.login)
                user, temporary = access.reset_password(tx, user_id)
            print(f"Временный пароль для «{user['login']}»: {temporary}")
            print("При входе пользователь задаст свой пароль.")
    except EngineError as error:
        print(f"Ошибка: {error.message}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


def _password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Пароль (не короче 12 символов): ")
    if getpass.getpass("Ещё раз: ") != first:
        raise EngineError("password_mismatch", "Пароли не совпадают")
    return first


def _user_id(conn, login: str) -> str:  # type: ignore[no-untyped-def]
    row = conn.execute(
        "SELECT id FROM users WHERE login_norm = ?", (access.norm_login(login),)
    ).fetchone()
    if row is None:
        raise EngineError("user_not_found", f"Пользователь «{login}» не найден")
    return row[0]


if __name__ == "__main__":
    raise SystemExit(main())
