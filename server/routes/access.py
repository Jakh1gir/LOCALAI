"""Пользователи и роли: только для раздела «Администрирование»."""

from __future__ import annotations

from typing import Any

from engine import access
from server import schemas
from server.api import Call, Endpoint

ADMIN = "section.admin"


def list_roles(call: Call) -> dict[str, Any]:
    """Роли с числом пользователей."""
    return {"items": access.list_roles(call.conn)}


def create_role(call: Call) -> dict[str, Any]:
    """Создать роль, пустую или копией существующей."""
    body = call.body
    return access.create_role(call.tx, body.name, body.description, copy_from=body.copy_from)


def get_role(call: Call) -> dict[str, Any]:
    """Роль и её матрица прав."""
    return access.get_role(call.conn, call.params["id"])


def update_role(call: Call) -> dict[str, Any]:
    """Переименовать роль или сменить описание; права остаются за ID роли."""
    body = call.body
    return access.update_role(
        call.tx, call.params["id"], name=body.name, description=body.description
    )


def archive_role(call: Call) -> dict[str, Any]:
    """Отключить роль: её пользователи не войдут, открытые сессии закрываются."""
    return access.set_role_archived(call.tx, call.params["id"], True)


def restore_role(call: Call) -> dict[str, Any]:
    """Вернуть отключённую роль."""
    return access.set_role_archived(call.tx, call.params["id"], False)


def set_permissions(call: Call) -> dict[str, Any]:
    """Заменить матрицу прав роли целиком."""
    items = [item.model_dump() for item in call.body.permissions]
    return access.set_permissions(call.tx, call.params["id"], items)


def list_users(call: Call) -> dict[str, Any]:
    """Учётные записи; пароли и их хэши не выдаются."""
    return {"items": access.list_users(call.conn)}


def create_user(call: Call) -> dict[str, Any]:
    """Создать учётную запись; временный пароль показывается один раз."""
    body = call.body
    user, temporary = access.create_user(call.tx, body.login, body.name, body.role_id)
    call.once["temporary_password"] = temporary
    return user


def get_user(call: Call) -> dict[str, Any]:
    """Учётная запись."""
    return access.get_user(call.conn, call.params["id"])


def update_user(call: Call) -> dict[str, Any]:
    """Сменить имя или роль пользователя."""
    body = call.body
    return access.update_user(call.tx, call.params["id"], name=body.name, role_id=body.role_id)


def block_user(call: Call) -> dict[str, Any]:
    """Заблокировать: сессии закрываются, вход невозможен."""
    return access.set_blocked(call.tx, call.params["id"], True)


def unblock_user(call: Call) -> dict[str, Any]:
    """Снять блокировку."""
    return access.set_blocked(call.tx, call.params["id"], False)


def reset_password(call: Call) -> dict[str, Any]:
    """Сбросить пароль на временный; при входе пользователь задаст свой."""
    user, temporary = access.reset_password(call.tx, call.params["id"])
    call.once["temporary_password"] = temporary
    return user


def _e(method: str, path: str, handler, **extra: Any) -> Endpoint:  # type: ignore[no-untyped-def]
    return Endpoint(method, path, handler, ADMIN, tag="Пользователи и роли", **extra)


ENDPOINTS = [
    _e("GET", "/roles", list_roles),
    _e("POST", "/roles", create_role, body=schemas.RoleCreate, status=201),
    _e("GET", "/roles/{id}", get_role),
    _e("PATCH", "/roles/{id}", update_role, body=schemas.RolePatch),
    _e("POST", "/roles/{id}/archive", archive_role),
    _e("POST", "/roles/{id}/restore", restore_role),
    _e("PUT", "/roles/{id}/permissions", set_permissions, body=schemas.Permissions),
    _e("GET", "/users", list_users),
    _e("POST", "/users", create_user, body=schemas.UserCreate, status=201),
    _e("GET", "/users/{id}", get_user),
    _e("PATCH", "/users/{id}", update_user, body=schemas.UserPatch),
    _e("POST", "/users/{id}/block", block_user),
    _e("POST", "/users/{id}/unblock", unblock_user),
    _e("POST", "/users/{id}/reset-password", reset_password),
]
