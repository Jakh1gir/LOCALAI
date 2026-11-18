"""Вход, выход, смена своего пароля, текущая сессия и права."""

from __future__ import annotations

from typing import Any

from engine import access, sessions
from server import schemas
from server.api import Call, Endpoint


def login(call: Call) -> dict[str, Any] | None:
    """Войти по логину и паролю; сессия приходит в cookie."""
    assert call.throttle is not None
    call.throttle.check(call.client)
    outcome = sessions.login(call.tx, call.body.login, call.body.password, call.client)
    if outcome.error is not None:
        call.throttle.fail(call.client)
        call.deferred = outcome.error  # след неудачного входа сохраняется
        return None
    assert outcome.token is not None and outcome.access is not None
    call.cookie = outcome.token
    return outcome.access.to_dict()


def logout(call: Call) -> None:
    """Выйти: сессия закрывается."""
    assert call.session is not None
    sessions.logout(call.tx, call.session)
    call.cookie = ""


def me(call: Call) -> dict[str, Any]:
    """Текущий пользователь, его роль и права."""
    assert call.session is not None
    return {**call.access.to_dict(), "session_expires_at": call.session.expires_at}


def change_password(call: Call) -> dict[str, Any]:
    """Сменить свой пароль; остальные сессии пользователя закрываются."""
    assert call.session is not None
    body = call.body
    sessions.change_password(call.tx, call.session, body.current_password, body.new_password)
    return access.load(call.tx.conn, call.access.user_id).to_dict()


def permission_catalog(call: Call) -> dict[str, Any]:
    """Что можно выдать роли: разделы, действия над реестром, ограничения полей."""
    return access.catalog()


def _e(method: str, path: str, handler, permission: str, **extra: Any) -> Endpoint:  # type: ignore[no-untyped-def]
    return Endpoint(method, path, handler, permission, tag="Вход", **extra)


ENDPOINTS = [
    _e("POST", "/auth/login", login, "public", body=schemas.Login, idempotent=False),
    _e("POST", "/auth/logout", logout, "self", status=204, idempotent=False),
    _e("GET", "/auth/me", me, "self"),
    _e(
        "POST",
        "/auth/password",
        change_password,
        "self",
        body=schemas.PasswordChange,
        idempotent=False,
    ),
    _e("GET", "/auth/permissions", permission_catalog, "session"),
]
