"""Вход, сессии, смена пароля (ROLE-04, ROLE-06, ROLE-07, SEC-01, SEC-02, SEC-08).

Токен сессии — случайная строка; в базе лежит только его SHA-256, поэтому
копия базы не позволяет войти чужой сессией. Сессия живёт не дольше
``LIFETIME`` и закрывается после ``IDLE`` без запросов. После
``MAX_FAILURES`` неверных паролей подряд вход в учётную запись закрыт на
``LOCK``; неверный пароль и неизвестный логин дают один и тот же ответ.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from engine import access, passwords
from engine.access import Access
from engine.database import Actor, Tx
from engine.errors import FORBIDDEN, TOO_MANY, UNAUTHORIZED, EngineError

LIFETIME = timedelta(hours=12)
IDLE = timedelta(minutes=15)
LOCK = timedelta(minutes=15)
MAX_FAILURES = 5
# Время последнего запроса обновляется не чаще раза в минуту: иначе каждое
# чтение превращалось бы в запись и ждало бы очереди на запись.
TOUCH_EVERY = timedelta(minutes=1)


@dataclass(frozen=True)
class Outcome:
    """Итог попытки входа. Отказ тоже итог: его след в журнале и счётчике
    неверных паролей должен сохраниться, поэтому отказ не прерывает транзакцию."""

    token: str | None = None
    access: Access | None = None
    error: EngineError | None = None


@dataclass(frozen=True)
class Session:
    access: Access
    token_hash: str
    expires_at: str
    stale: bool


def login(tx: Tx, login_name: object, password: object, client: str | None = None) -> Outcome:
    """Проверяет логин и пароль и открывает сессию."""
    if not isinstance(login_name, str) or not isinstance(password, str):
        return Outcome(error=_wrong())
    row = tx.conn.execute(
        "SELECT u.*, r.archived_at AS role_archived_at FROM users u"
        " JOIN roles r ON r.id = u.role_id WHERE u.login_norm = ?",
        (access.norm_login(login_name),),
    ).fetchone()
    shown = login_name.strip()[:64]
    if row is None:
        passwords.burn_time()
        _failed(tx, None, shown, "unknown_login")
        return Outcome(error=_wrong())
    user = Actor(row["id"], row["name"], "user")
    if row["locked_until"] and row["locked_until"] > tx.now:
        _failed(tx, user, shown, "locked")
        return Outcome(error=_locked(row["locked_until"]))
    if not passwords.verify(password, row["password_hash"]):
        return Outcome(error=_count_failure(tx, row, user, shown))
    if row["blocked_at"] or row["role_archived_at"]:
        _failed(tx, user, shown, "blocked")
        return Outcome(
            error=EngineError(
                "user_blocked",
                "Учётная запись заблокирована. Обратитесь к администратору",
                status=FORBIDDEN,
            )
        )
    changes: dict[str, Any] = {"failed_logins": 0, "locked_until": None, "last_login_at": tx.now}
    if passwords.needs_rehash(row["password_hash"]):
        changes["password_hash"] = passwords.hash_password(password)
    _set(tx, row["id"], changes)
    _purge(tx)
    token = _open(tx, row["id"], client)
    tx.audit("auth.login", "user", row["id"], details={"client": client}, actor=user)
    return Outcome(token=token, access=access.load(tx.conn, row["id"]))


def resolve(conn: sqlite3.Connection, token: str | None, now: str) -> Session:
    """Сессия по токену и права её пользователя на этот момент (ROLE-09)."""
    if not token:
        raise EngineError("auth_required", "Нужно войти в систему", status=UNAUTHORIZED)
    row = conn.execute(
        "SELECT s.*, u.blocked_at, r.archived_at AS role_archived_at FROM sessions s"
        " JOIN users u ON u.id = s.user_id JOIN roles r ON r.id = u.role_id"
        " WHERE s.token_hash = ?",
        (token_hash(token),),
    ).fetchone()
    if row is None or row["blocked_at"] or row["role_archived_at"]:
        raise EngineError("session_invalid", "Сессия закрыта. Войдите заново", status=UNAUTHORIZED)
    if row["expires_at"] <= now or _shift(row["last_seen_at"], IDLE) <= now:
        raise EngineError("session_expired", "Сессия истекла. Войдите заново", status=UNAUTHORIZED)
    return Session(
        access=access.load(conn, row["user_id"]),
        token_hash=row["token_hash"],
        expires_at=row["expires_at"],
        stale=_shift(row["last_seen_at"], TOUCH_EVERY) <= now,
    )


def touch(tx: Tx, session: Session) -> None:
    tx.conn.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (tx.now, session.token_hash)
    )


def logout(tx: Tx, session: Session) -> None:
    tx.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session.token_hash,))
    tx.audit("auth.logout", "user", session.access.user_id)


def change_password(tx: Tx, session: Session, current: object, new: object) -> None:
    """Пользователь меняет свой пароль; другие его сессии закрываются (ROLE-07)."""
    user_id = session.access.user_id
    row = tx.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not isinstance(current, str) or not passwords.verify(current, row["password_hash"]):
        raise EngineError(
            "password_wrong", "Текущий пароль указан неверно", field="current_password"
        )
    fresh = passwords.check_new(new, login=row["login"])
    if fresh == current:
        raise EngineError("password_same", "Новый пароль совпадает с текущим", field="new_password")
    _set(
        tx,
        user_id,
        {
            "password_hash": passwords.hash_password(fresh),
            "must_change_password": 0,
            "password_changed_at": tx.now,
        },
    )
    tx.conn.execute(
        "DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?",
        (user_id, session.token_hash),
    )
    tx.audit("user.password_change", "user", user_id)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _open(tx: Tx, user_id: str, client: str | None) -> str:
    token = secrets.token_urlsafe(32)
    tx.conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at, client)"
        " VALUES (?,?,?,?,?,?)",
        (token_hash(token), user_id, tx.now, tx.now, _shift(tx.now, LIFETIME), client),
    )
    return token


def _purge(tx: Tx) -> None:
    """Истёкшие сессии удаляются при каждом входе: таблица не растёт."""
    idle_since = _shift(tx.now, -IDLE)
    tx.conn.execute(
        "DELETE FROM sessions WHERE expires_at <= ? OR last_seen_at <= ?", (tx.now, idle_since)
    )


def _count_failure(tx: Tx, row: sqlite3.Row, user: Actor, shown: str) -> EngineError:
    failures = row["failed_logins"] + 1
    if failures >= MAX_FAILURES:
        until = _shift(tx.now, LOCK)
        _set(tx, row["id"], {"failed_logins": 0, "locked_until": until})
        _failed(tx, user, shown, "wrong_password_locked")
        return _locked(until)
    _set(tx, row["id"], {"failed_logins": failures})
    _failed(tx, user, shown, "wrong_password")
    return _wrong()


def _failed(tx: Tx, user: Actor | None, shown: str, reason: str) -> None:
    actor = user or Actor(None, f"Неизвестный логин «{shown}»", "user")
    tx.audit(
        "auth.login_failed",
        "user",
        user.id if user else None,
        details={"login": shown, "reason": reason},
        actor=actor,
    )


def _set(tx: Tx, user_id: str, values: dict[str, Any]) -> None:
    sets = ", ".join(f"{column} = ?" for column in values)
    tx.conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*values.values(), user_id))


def _wrong() -> EngineError:
    return EngineError("login_failed", "Неверный логин или пароль", status=UNAUTHORIZED)


def _locked(until: str) -> EngineError:
    return EngineError(
        "login_locked",
        f"Слишком много неверных попыток. Вход откроется через {_minutes(until)} мин",
        status=TOO_MANY,
        details={"locked_until": until},
    )


def _minutes(until: str) -> int:
    left = _parse(until) - datetime.now(UTC)
    return max(1, -(-int(left.total_seconds()) // 60))


def _shift(stamp: str, delta: timedelta) -> str:
    moved = _parse(stamp) + delta
    return moved.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moved.microsecond // 1000:03d}Z"


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
