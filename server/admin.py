"""Учётные записи из командной строки сервера.

``python -m server.admin --db localai.sqlite3 create-admin --login admin --name "Иванов И. И."``
создаёт первого администратора: стартовых паролей в программе нет (SEC-03),
пароль задаёт установщик — вводом дважды или одной строкой через
``--password-stdin``.

``reset-password --login …`` выдаёт временный пароль, если единственный
администратор забыл свой. Команда доступна только тому, у кого есть доступ к
файлу базы на сервере, и пишется в журнал.

``create-certificate --host <имя> --host <адрес>`` создаёт сертификат HTTPS
для сетевого режима в папке ``tls`` рядом с базой (SEC-04).

``check-vectors`` проверяет на этом компьютере дополнение SQLite для поиска
по смыслу (sqlite-vec): загружается ли, ищет ли правильно и как быстро; для
сравнения — поиск в памяти. Код выхода 0 — дополнение работает, 2 — нет
(сервер тогда ищет в памяти).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from engine import Actor, EngineError, access, open_database
from server import tls

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
    certificate = commands.add_parser("create-certificate", help="сертификат HTTPS")
    certificate.add_argument(
        "--host", action="append", required=True, help="имя или адрес сервера; можно несколько"
    )
    certificate.add_argument("--replace", action="store_true", help="заменить прежний")
    vectors_check = commands.add_parser("check-vectors", help="проверить поиск по смыслу")
    vectors_check.add_argument("--count", type=int, default=20000, help="сколько векторов")
    args = parser.parse_args(argv)
    if args.command == "check-vectors":
        return _check_vectors(args.count)
    if args.command == "create-certificate":
        return _certificate(tls_folder(args.db), args.host, args.replace)
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


def tls_folder(db_path: str) -> Path:
    return Path(db_path).resolve().parent / "tls"


def _certificate(folder: Path, hosts: list[str], replace: bool) -> int:
    try:
        digest = tls.create(folder, hosts, replace=replace)
    except EngineError as error:
        print(f"Ошибка: {error.message}", file=sys.stderr)
        return 1
    print(f"Сертификат создан: {folder / tls.CERT}")
    print(f"Отпечаток SHA-256: {digest}")
    print("Сверьте отпечаток в браузере рабочего места при первом входе.")
    return 0


def _check_vectors(count: int) -> int:
    from server import vector_index

    report = vector_index.check(count)
    labels = {
        "python": "Python", "system": "Система", "sqlite": "SQLite",
        "load_extension": "Загрузка дополнений в Python", "numpy": "numpy",
        "count": "Векторов в проверке", "extension": "Дополнение sqlite-vec",
        "extension_error": "Ошибка дополнения", "extension_insert_s": "Запись индекса, с",
        "extension_search_ms": "Поиск дополнением, мс", "extension_correct": "Дополнение ищет верно",
        "memory_search_ms": "Поиск в памяти, мс", "memory_correct": "Память ищет верно",
    }  # fmt: skip
    for key, value in report.items():
        print(f"{labels.get(key, key)}: {'нет' if value in (None, False) else value}")
    works = bool(report.get("extension")) and report.get("extension_correct") is True
    print("Итог: " + ("поиск по смыслу — дополнением sqlite-vec." if works else
                      "дополнение не работает, сервер ищет по смыслу в памяти."))  # fmt: skip
    return 0 if works else 2


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
