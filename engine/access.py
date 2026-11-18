"""Роли, матрица прав и учётные записи (ROLE-01…ROLE-10).

Права принадлежат роли и привязаны к её ID: переименование роли ничего не
меняет (ROLE-02). Роль даёт разделы, действия над реестрами (над всеми — «*»
— или над выбранными) и ограничения на отдельные поля. Права читаются из
базы при каждом запросе, поэтому изменение действует на открытые сессии со
следующего запроса (ROLE-09).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from engine import passwords, structure, text
from engine.database import Actor, Tx
from engine.errors import CONFLICT, EngineError, not_found

SECTIONS = {
    "admin": "Администрирование: пользователи, роли, журнал",
    "structure": "Структура: площадки, типы, поля, реестры, справочники",
    "kks": "Каталог ККС: коды, описания, псевдонимы",
    "ai": "ИИ",
    "tools": "Инженерные инструменты",
    "timesheet": "Табель",
    "backups": "Резервные копии",
    "chat": "Общий чат: читать",
    "chat_write": "Общий чат: писать",
    "chat_moderate": "Общий чат: скрывать чужие сообщения",
    "documents": "Документы: загружать, заменять и привязывать",
    "worker": "Фоновые задачи: выполнять (учётная запись процесса схем)",
}
DATASET_ACTIONS = {
    "view": "Видеть записи",
    "create": "Создавать",
    "edit": "Править",
    "archive": "Отправлять в архив и возвращать",
    "purge": "Удалять из архива окончательно",
    "import": "Импорт",
    "export": "Экспорт",
}
FIELD_LIMITS = {"hidden": "Не видеть поле", "readonly": "Не править поле"}
DOC_TYPE_ACTIONS = {"view": "Видеть документы"}
ALL = "*"
MAX_PERMISSIONS = 5000
_LOGIN_RE = re.compile(r"^[\w.@-]{1,64}$")


@dataclass(frozen=True)
class Access:
    """Что разрешено пользователю сейчас: собирается из базы на каждый запрос."""

    user_id: str
    login: str
    name: str
    role_id: str
    role_name: str
    must_change_password: bool
    sections: frozenset[str]
    datasets: dict[str, frozenset[str]]
    hidden: frozenset[str]
    readonly: frozenset[str]
    # Типы документов, которые роль видит; «*» — все (ROLE-03).
    doc_types: frozenset[str] = frozenset()

    @property
    def actor(self) -> Actor:
        return Actor(self.user_id, self.name, "user")

    def has(self, section: str) -> bool:
        return section in self.sections

    def can(self, dataset_id: str, action: str) -> bool:
        granted = self.datasets.get(ALL, frozenset()) | self.datasets.get(dataset_id, frozenset())
        return action in granted

    def sees_documents(self, type_id: str) -> bool:
        return ALL in self.doc_types or type_id in self.doc_types

    def hidden_keys(self, schema: structure.TypeSchema) -> frozenset[str]:
        return frozenset(field.key for field in schema.fields if field.id in self.hidden)

    def locked_keys(self, schema: structure.TypeSchema) -> frozenset[str]:
        """Поля, которые пользователь не может менять: скрытые и только для чтения."""
        locked = self.hidden | self.readonly
        return frozenset(field.key for field in schema.fields if field.id in locked)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": {"id": self.user_id, "login": self.login, "name": self.name},
            "role": {"id": self.role_id, "name": self.role_name},
            "must_change_password": self.must_change_password,
            "permissions": {
                "sections": sorted(self.sections),
                "datasets": {key: sorted(value) for key, value in sorted(self.datasets.items())},
                "fields": {
                    **{field_id: "hidden" for field_id in sorted(self.hidden)},
                    **{field_id: "readonly" for field_id in sorted(self.readonly - self.hidden)},
                },
                "doc_types": sorted(self.doc_types),
            },
        }


def load(conn: sqlite3.Connection, user_id: str) -> Access:
    row = conn.execute(
        "SELECT u.*, r.name AS role_name FROM users u JOIN roles r ON r.id = u.role_id"
        " WHERE u.id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise not_found("user", user_id)
    sections: set[str] = set()
    datasets: dict[str, set[str]] = {}
    hidden: set[str] = set()
    readonly: set[str] = set()
    doc_types: set[str] = set()
    for item in conn.execute(
        "SELECT scope, target, action FROM role_permissions WHERE role_id = ?", (row["role_id"],)
    ):
        if item["scope"] == "section":
            sections.add(item["target"])
        elif item["scope"] == "dataset":
            datasets.setdefault(item["target"], set()).add(item["action"])
        elif item["scope"] == "field":
            (hidden if item["action"] == "hidden" else readonly).add(item["target"])
        elif item["scope"] == "doc_type":
            doc_types.add(item["target"])
    return Access(
        user_id=row["id"],
        login=row["login"],
        name=row["name"],
        role_id=row["role_id"],
        role_name=row["role_name"],
        must_change_password=bool(row["must_change_password"]),
        sections=frozenset(sections),
        datasets={key: frozenset(value) for key, value in datasets.items()},
        hidden=frozenset(hidden),
        readonly=frozenset(readonly),
        doc_types=frozenset(doc_types),
    )


def catalog() -> dict[str, Any]:
    """Что можно выдать роли: подписи для экрана матрицы прав."""
    return {
        "sections": SECTIONS,
        "dataset_actions": DATASET_ACTIONS,
        "field_limits": FIELD_LIMITS,
        "doc_type_actions": DOC_TYPE_ACTIONS,
    }


# Роли ---------------------------------------------------------------------


def list_roles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM users u WHERE u.role_id = r.id) AS users"
        " FROM roles r ORDER BY r.archived_at IS NOT NULL, r.name_norm"
    ).fetchall()
    return [_role(row) for row in rows]


def get_role(conn: sqlite3.Connection, role_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM users u WHERE u.role_id = r.id) AS users"
        " FROM roles r WHERE r.id = ?",
        (role_id,),
    ).fetchone()
    if row is None:
        raise not_found("role", role_id)
    return {**_role(row), "permissions": _permissions(conn, role_id)}


def create_role(
    tx: Tx, name: str, description: str | None = None, *, copy_from: str | None = None
) -> dict[str, Any]:
    """Новая роль; с ``copy_from`` — с правами существующей (ROLE-01)."""
    if copy_from is not None:
        get_role(tx.conn, copy_from)
    shown = text.clean_name(name)
    _check_role_name(tx.conn, shown, None)
    role_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO roles (id, name, name_norm, description, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (role_id, shown, text.norm_name(shown), _description(description), tx.now, tx.now),
    )
    if copy_from is not None:
        tx.conn.execute(
            "INSERT INTO role_permissions (role_id, scope, target, action)"
            " SELECT ?, scope, target, action FROM role_permissions WHERE role_id = ?",
            (role_id, copy_from),
        )
    tx.audit("role.create", "role", role_id, details={"name": shown, "copy_from": copy_from})
    return get_role(tx.conn, role_id)


def update_role(
    tx: Tx, role_id: str, *, name: str | None = None, description: str | None = None
) -> dict[str, Any]:
    current = get_role(tx.conn, role_id)
    changes: dict[str, Any] = {}
    if name is not None:
        shown = text.clean_name(name)
        _check_role_name(tx.conn, shown, role_id)
        if shown != current["name"]:
            changes["name"] = shown
            changes["name_norm"] = text.norm_name(shown)
    if description is not None and _description(description) != current["description"]:
        changes["description"] = _description(description)
    if changes:
        _update(tx, "roles", role_id, changes)
        shown_changes = {key: value for key, value in changes.items() if key != "name_norm"}
        tx.audit("role.update", "role", role_id, details={"from": current["name"], **shown_changes})
    return get_role(tx.conn, role_id)


def set_role_archived(tx: Tx, role_id: str, archived: bool) -> dict[str, Any]:
    """Отключить роль или вернуть. Пользователи отключённой роли не входят."""
    current = get_role(tx.conn, role_id)
    if bool(current["archived_at"]) == archived:
        return current
    with _keeps_admin(tx):
        _update(tx, "roles", role_id, {"archived_at": tx.now if archived else None})
    tx.audit(f"role.{'archive' if archived else 'restore'}", "role", role_id)
    return get_role(tx.conn, role_id)


def set_permissions(tx: Tx, role_id: str, items: list[dict[str, str]]) -> dict[str, Any]:
    """Заменяет матрицу прав роли целиком; в журнал пишется разница."""
    get_role(tx.conn, role_id)
    if not isinstance(items, list) or len(items) > MAX_PERMISSIONS:
        raise EngineError(
            "permissions_invalid", f"Права: список длиной до {MAX_PERMISSIONS}", field="permissions"
        )
    wanted = {_check_permission(tx.conn, item, number) for number, item in enumerate(items)}
    before = {
        (row["scope"], row["target"], row["action"])
        for row in tx.conn.execute(
            "SELECT scope, target, action FROM role_permissions WHERE role_id = ?", (role_id,)
        )
    }
    if wanted == before:
        return get_role(tx.conn, role_id)
    with _keeps_admin(tx):
        tx.conn.execute("DELETE FROM role_permissions WHERE role_id = ?", (role_id,))
        tx.conn.executemany(
            "INSERT INTO role_permissions (role_id, scope, target, action) VALUES (?,?,?,?)",
            [(role_id, *item) for item in sorted(wanted)],
        )
    _touch(tx, "roles", role_id)
    tx.audit(
        "role.permissions",
        "role",
        role_id,
        details={
            "added": [list(item) for item in sorted(wanted - before)],
            "removed": [list(item) for item in sorted(before - wanted)],
        },
    )
    return get_role(tx.conn, role_id)


# Пользователи --------------------------------------------------------------


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users u JOIN roles r ON r.id = u.role_id"
        " ORDER BY u.blocked_at IS NOT NULL, u.login_norm"
    ).fetchall()
    return [dict(row) for row in rows]


def get_user(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise not_found("user", user_id)
    return dict(row)


def create_user(tx: Tx, login: str, name: str, role_id: str) -> tuple[dict[str, Any], str]:
    """Новая учётная запись с временным паролем; его нужно сменить при входе (ROLE-05)."""
    temporary = passwords.temporary()
    user_id = _insert_user(tx, login, name, role_id, temporary, must_change=True)
    tx.audit("user.create", "user", user_id, details={"login": login.strip(), "role_id": role_id})
    return get_user(tx.conn, user_id), temporary


def create_admin(tx: Tx, login: str, name: str, password: str) -> dict[str, Any]:
    """Первый администратор при установке: пароль задаёт установщик (SEC-03)."""
    row = tx.conn.execute(
        "SELECT r.id FROM roles r JOIN role_permissions p ON p.role_id = r.id"
        " WHERE p.scope = 'section' AND p.target = 'admin' AND r.archived_at IS NULL"
        " ORDER BY r.name_norm <> 'администратор', r.created_at LIMIT 1"
    ).fetchone()
    if row is None:
        raise EngineError("admin_role_missing", "Нет действующей роли с правом администрирования")
    passwords.check_new(password, login=login)
    user_id = _insert_user(tx, login, name, row["id"], password, must_change=False)
    tx.audit("user.create", "user", user_id, details={"login": login.strip(), "role_id": row["id"]})
    return get_user(tx.conn, user_id)


def update_user(
    tx: Tx, user_id: str, *, name: str | None = None, role_id: str | None = None
) -> dict[str, Any]:
    current = get_user(tx.conn, user_id)
    changes: dict[str, Any] = {}
    if name is not None:
        shown = text.clean_name(name, "Имя")
        if shown != current["name"]:
            changes["name"] = shown
    if role_id is not None and role_id != current["role_id"]:
        role = get_role(tx.conn, role_id)
        if role["archived_at"]:
            raise EngineError("role_archived", "Роль отключена", field="role_id")
        changes["role_id"] = role_id
    if not changes:
        return current
    with _keeps_admin(tx):
        _update(tx, "users", user_id, changes)
    tx.audit("user.update", "user", user_id, details=changes)
    return get_user(tx.conn, user_id)


def set_blocked(tx: Tx, user_id: str, blocked: bool) -> dict[str, Any]:
    """Блокировка закрывает все сессии пользователя."""
    current = get_user(tx.conn, user_id)
    if bool(current["blocked_at"]) == blocked:
        return current
    with _keeps_admin(tx):
        _update(tx, "users", user_id, {"blocked_at": tx.now if blocked else None})
    if blocked:
        tx.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    tx.audit(f"user.{'block' if blocked else 'unblock'}", "user", user_id)
    return get_user(tx.conn, user_id)


def reset_password(tx: Tx, user_id: str) -> tuple[dict[str, Any], str]:
    """Временный пароль вместо прежнего; сессии закрываются (ROLE-05, ROLE-06).

    Администратор видит только временный пароль, и только в ответе на сброс:
    пароль, который пользователь задаст сам, не видит никто (ROLE-07).
    """
    get_user(tx.conn, user_id)
    temporary = passwords.temporary()
    _update(
        tx,
        "users",
        user_id,
        {
            "password_hash": passwords.hash_password(temporary),
            "must_change_password": 1,
            "password_changed_at": tx.now,
            "failed_logins": 0,
            "locked_until": None,
        },
    )
    tx.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    tx.audit("user.password_reset", "user", user_id)
    return get_user(tx.conn, user_id), temporary


def has_users(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def active_admins(conn: sqlite3.Connection) -> int:
    """Незаблокированные пользователи действующих ролей с правом администрирования."""
    return conn.execute(
        "SELECT COUNT(*) FROM users u JOIN roles r ON r.id = u.role_id"
        " WHERE u.blocked_at IS NULL AND r.archived_at IS NULL AND EXISTS ("
        " SELECT 1 FROM role_permissions p WHERE p.role_id = r.id"
        " AND p.scope = 'section' AND p.target = 'admin')"
    ).fetchone()[0]


# Внутреннее ---------------------------------------------------------------

_USER_COLUMNS = (
    "u.id, u.login, u.name, u.role_id, r.name AS role_name, u.must_change_password,"
    " u.blocked_at, u.locked_until, u.last_login_at, u.password_changed_at,"
    " u.created_at, u.updated_at"
)


@contextmanager
def _keeps_admin(tx: Tx) -> Iterator[None]:
    """Изменение не должно оставить базу без администратора (ROLE-08).

    Проверяется результат, а не намерение: так одно правило покрывает
    блокировку, смену роли, отключение роли и снятие права с роли.
    Ошибка откатывает всю транзакцию вместе с изменением.
    """
    before = active_admins(tx.conn)
    yield
    if before and not active_admins(tx.conn):
        raise EngineError(
            "last_admin",
            "Нельзя оставить базу без администратора: это последний активный администратор."
            " Сначала назначьте администратором другого пользователя",
            status=CONFLICT,
        )


def _insert_user(
    tx: Tx, login: str, name: str, role_id: str, password: str, *, must_change: bool
) -> str:
    shown_login = _check_login(tx.conn, login)
    shown_name = text.clean_name(name, "Имя")
    role = get_role(tx.conn, role_id)
    if role["archived_at"]:
        raise EngineError("role_archived", "Роль отключена", field="role_id")
    user_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO users (id, login, login_norm, name, role_id, password_hash,"
        " must_change_password, password_changed_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            user_id,
            shown_login,
            norm_login(shown_login),
            shown_name,
            role_id,
            passwords.hash_password(password),
            int(must_change),
            tx.now,
            tx.now,
            tx.now,
        ),
    )
    return user_id


def norm_login(login: str) -> str:
    return text.fold(login.strip())


def _check_login(conn: sqlite3.Connection, login: object) -> str:
    if not isinstance(login, str) or not _LOGIN_RE.match(login.strip()):
        raise EngineError(
            "login_invalid",
            "Логин: от 1 до 64 знаков — буквы, цифры, точка, дефис, подчёркивание, @",
            field="login",
        )
    shown = login.strip()
    if conn.execute("SELECT 1 FROM users WHERE login_norm = ?", (norm_login(shown),)).fetchone():
        raise EngineError("login_taken", f"Логин «{shown}» уже занят", field="login")
    return shown


def _check_role_name(conn: sqlite3.Connection, shown: str, except_id: str | None) -> None:
    row = conn.execute(
        "SELECT id FROM roles WHERE name_norm = ?", (text.norm_name(shown),)
    ).fetchone()
    if row and row["id"] != except_id:
        raise EngineError("name_taken", f"Роль «{shown}» уже есть", field="name")


def _description(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return text.clean_name(value, "Описание", limit=1000)


def _check_permission(conn: sqlite3.Connection, item: Any, number: int) -> tuple[str, str, str]:
    where = f"permissions.{number}"

    def bad(message: str) -> EngineError:
        return EngineError("permission_invalid", f"Право № {number + 1}: {message}", field=where)

    if not isinstance(item, dict) or set(item) != {"scope", "target", "action"}:
        raise bad("ожидается объект с полями scope, target, action")
    scope, target, action = item["scope"], item["target"], item["action"]
    if not all(isinstance(value, str) for value in (scope, target, action)):
        raise bad("значения — текст")
    if scope == "section":
        if target not in SECTIONS or action != "use":
            raise bad(f"раздел «{target}» не существует")
    elif scope == "dataset":
        if action not in DATASET_ACTIONS:
            raise bad(f"действие «{action}» над реестром не существует")
        if (
            target != ALL
            and not conn.execute("SELECT 1 FROM datasets WHERE id = ?", (target,)).fetchone()
        ):
            raise bad(f"реестр {target} не найден")
    elif scope == "field":
        if action not in FIELD_LIMITS:
            raise bad(f"ограничение «{action}» не существует")
        if not conn.execute("SELECT 1 FROM fields WHERE id = ?", (target,)).fetchone():
            raise bad(f"поле {target} не найдено")
    elif scope == "doc_type":
        if action not in DOC_TYPE_ACTIONS:
            raise bad(f"действие «{action}» над типом документов не существует")
        if (
            target != ALL
            and not conn.execute("SELECT 1 FROM document_types WHERE id = ?", (target,)).fetchone()
        ):
            raise bad(f"тип документов {target} не найден")
    else:
        raise bad(f"область «{scope}» не существует")
    return scope, target, action


def _permissions(conn: sqlite3.Connection, role_id: str) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT scope, target, action FROM role_permissions WHERE role_id = ?"
        " ORDER BY scope, target, action",
        (role_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _role(row: sqlite3.Row) -> dict[str, Any]:
    found = dict(row)
    found.pop("name_norm", None)
    return found


def _update(tx: Tx, table: str, entity_id: str, values: dict[str, Any]) -> None:
    assert table in ("roles", "users")
    sets = ", ".join(f"{column} = ?" for column in values)
    tx.conn.execute(
        f"UPDATE {table} SET {sets}, updated_at = ? WHERE id = ?",
        (*values.values(), tx.now, entity_id),
    )


def _touch(tx: Tx, table: str, entity_id: str) -> None:
    assert table in ("roles", "users")
    tx.conn.execute(f"UPDATE {table} SET updated_at = ? WHERE id = ?", (tx.now, entity_id))
