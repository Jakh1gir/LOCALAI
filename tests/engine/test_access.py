"""Роли, права, учётные записи, вход и сессии в ядре (ROLE-01…10, SEC-01, SEC-02)."""

from __future__ import annotations

import pytest

from engine import SYSTEM, EngineError, access, audit, passwords, sessions, text

PASSWORD = "длинный-пароль-1"


def _roles(db) -> dict[str, str]:
    with db.read() as conn:
        return {role["name"]: role["id"] for role in access.list_roles(conn)}


def _admin(db, login="admin"):
    with db.write(SYSTEM) as tx:
        return access.create_admin(tx, login, f"Админ {login}", PASSWORD)


def _login(db, login="admin", password=PASSWORD):
    with db.write(SYSTEM) as tx:
        return sessions.login(tx, login, password, "127.0.0.1")


def test_new_database_has_start_roles_and_no_users(db):
    roles = _roles(db)
    assert set(roles) == {"Администратор", "Инженер", "Наблюдатель"}  # ROLE-10
    with db.read() as conn:
        assert not access.has_users(conn)  # стартовых паролей нет (SEC-03)
        admin = access.get_role(conn, roles["Администратор"])
        viewer = access.get_role(conn, roles["Наблюдатель"])
    assert {"scope": "section", "target": "admin", "action": "use"} in admin["permissions"]
    assert {item["action"] for item in viewer["permissions"] if item["scope"] == "dataset"} == {
        "view",
        "export",
    }


def test_password_hash_keeps_parameters_and_checks_rules():
    stored = passwords.hash_password(PASSWORD)
    assert stored.startswith(f"scrypt${passwords.COST}$") and PASSWORD not in stored
    assert passwords.verify(PASSWORD, stored)
    assert not passwords.verify(PASSWORD + "x", stored)
    assert not passwords.verify(PASSWORD, "мусор")
    assert not passwords.needs_rehash(stored)
    assert passwords.needs_rehash(stored.replace(f"scrypt${passwords.COST}$", "scrypt$3$"))
    for bad, code in (
        ("короткий", "password_too_short"),
        ("ivanov-12345678", "password_contains_login"),
    ):
        with pytest.raises(EngineError) as caught:
            passwords.check_new(bad, login="Ivanov")
        assert caught.value.code == code


def test_role_copy_rename_and_permissions_stay_with_its_id(db):
    roles = _roles(db)
    with db.write(SYSTEM) as tx:
        copy = access.create_role(tx, "Инженер КИП", copy_from=roles["Инженер"])
        renamed = access.update_role(tx, copy["id"], name="Метролог")
    with db.read() as conn:
        engineer = access.get_role(conn, roles["Инженер"])
        events = [event["action"] for event in _events(conn, copy["id"])]
    assert renamed["id"] == copy["id"] and renamed["name"] == "Метролог"
    assert renamed["permissions"] == engineer["permissions"]  # ROLE-01, ROLE-02
    assert events == ["role.update", "role.create"]


@pytest.mark.parametrize(
    "item",
    [
        {"scope": "section", "target": "чужой", "action": "use"},
        {"scope": "dataset", "target": "00000000-0000-4000-8000-000000000000", "action": "view"},
        {"scope": "dataset", "target": "*", "action": "delete"},
        {"scope": "field", "target": "*", "action": "hidden"},
        {"scope": "doc_type", "target": "паспорт", "action": "view"},
        {"scope": "section", "target": "admin"},
    ],
)
def test_unknown_permission_is_rejected_with_its_number(db, item):
    roles = _roles(db)
    with pytest.raises(EngineError) as caught, db.write(SYSTEM) as tx:
        access.set_permissions(
            tx, roles["Наблюдатель"], [{"scope": "section", "target": "ai", "action": "use"}, item]
        )
    assert caught.value.code == "permission_invalid" and caught.value.field == "permissions.1"


def test_permission_change_is_audited_as_a_difference(db):
    roles = _roles(db)
    with db.write(SYSTEM) as tx:
        access.set_permissions(
            tx,
            roles["Наблюдатель"],
            [
                {"scope": "dataset", "target": "*", "action": "view"},
                {"scope": "section", "target": "kks", "action": "use"},
            ],
        )
    with db.read() as conn:
        event = _events(conn, roles["Наблюдатель"])[0]
    assert event["details"]["added"] == [["section", "kks", "use"]]
    assert ["dataset", "*", "export"] in event["details"]["removed"]


def test_last_admin_cannot_be_removed_any_way(db):
    """AT-22: блокировка, смена роли, отключение роли и снятие права — один отказ."""
    admin = _admin(db)
    roles = _roles(db)
    without_admin = [{"scope": "dataset", "target": "*", "action": "view"}]
    attempts = [
        lambda tx: access.set_blocked(tx, admin["id"], True),
        lambda tx: access.update_user(tx, admin["id"], role_id=roles["Инженер"]),
        lambda tx: access.set_role_archived(tx, roles["Администратор"], True),
        lambda tx: access.set_permissions(tx, roles["Администратор"], without_admin),
    ]
    for attempt in attempts:
        with pytest.raises(EngineError) as caught, db.write(SYSTEM) as tx:
            attempt(tx)
        assert caught.value.code == "last_admin"
        assert "последний активный администратор" in caught.value.message
    with db.read() as conn:
        assert access.active_admins(conn) == 1
    second = _admin(db, "admin2")
    with db.write(SYSTEM) as tx:
        access.set_blocked(tx, admin["id"], True)  # второй администратор есть
    with db.read() as conn:
        assert access.active_admins(conn) == 1
        assert access.get_user(conn, second["id"])["blocked_at"] is None


def test_login_opens_a_session_stored_only_as_a_hash(db):
    admin = _admin(db)
    outcome = _login(db, "ADMIN")  # логин без учёта регистра
    assert outcome.error is None and outcome.access.user_id == admin["id"]
    with db.read() as conn:
        stored = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
        session = sessions.resolve(conn, outcome.token, text.now())
        event = _events(conn, admin["id"])[0]
    assert stored == sessions.token_hash(outcome.token) and outcome.token not in stored
    assert session.access.has("admin") and session.access.can("любой", "edit")
    assert event["action"] == "auth.login" and event["actor_id"] == admin["id"]  # ROLE-04


def test_wrong_password_and_unknown_login_look_the_same_and_lock_after_five(db):
    admin = _admin(db)
    unknown = _login(db, "никто")
    wrong = _login(db, "admin", "неверный-пароль")
    assert unknown.error.to_dict() == wrong.error.to_dict()
    assert wrong.error.code == "login_failed"
    for _ in range(3):
        _login(db, "admin", "неверный-пароль")
    locked = _login(db, "admin", "неверный-пароль")
    assert locked.error.code == "login_locked"
    assert _login(db).error.code == "login_locked"  # и верный пароль ждёт
    with db.read() as conn:
        reasons = [
            event["details"]["reason"]
            for event in _events(conn, admin["id"])
            if event["action"] == "auth.login_failed"
        ]
        anonymous = conn.execute(
            "SELECT actor_label FROM audit_log WHERE action = 'auth.login_failed'"
            " AND entity_id IS NULL"
        ).fetchone()[0]
    assert reasons[:2] == ["locked", "wrong_password_locked"]
    assert anonymous == "Неизвестный логин «никто»"


def test_blocked_user_and_archived_role_lose_sessions(db):
    _admin(db)
    roles = _roles(db)
    with db.write(SYSTEM) as tx:
        _, temporary = access.create_user(tx, "petrov", "Петров", roles["Инженер"])
    outcome = _login(db, "petrov", temporary)
    assert outcome.access.must_change_password
    with db.write(SYSTEM) as tx:
        access.set_role_archived(tx, roles["Инженер"], True)
    with pytest.raises(EngineError) as caught, db.read() as conn:
        sessions.resolve(conn, outcome.token, text.now())
    assert caught.value.code == "session_invalid"
    assert _login(db, "petrov", temporary).error.code == "user_blocked"


def test_session_closes_after_idle_and_lifetime(db):
    _admin(db)
    token = _login(db).token
    with db.read() as conn:
        fresh = sessions.resolve(conn, token, text.now())
    assert not fresh.stale
    later = sessions._shift(text.now(), sessions.TOUCH_EVERY * 2)
    with db.read() as conn:
        assert sessions.resolve(conn, token, later).stale
    for delta in (sessions.IDLE, sessions.LIFETIME):
        moment = sessions._shift(text.now(), delta)
        with pytest.raises(EngineError) as caught, db.read() as conn:
            sessions.resolve(conn, token, moment)
        assert caught.value.code == "session_expired"


def test_reset_closes_sessions_and_requires_new_password(db):
    """AT-21 в ядре: старые сессии закрыты, временный пароль требует смены."""
    admin = _admin(db)
    token = _login(db).token
    with db.write(SYSTEM) as tx:
        _, temporary = access.reset_password(tx, admin["id"])
    with pytest.raises(EngineError), db.read() as conn:
        sessions.resolve(conn, token, text.now())
    outcome = _login(db, "admin", temporary)
    assert outcome.access.must_change_password
    with db.read() as conn:
        session = sessions.resolve(conn, outcome.token, text.now())
    with pytest.raises(EngineError) as caught, db.write(SYSTEM) as tx:
        sessions.change_password(tx, session, temporary, temporary)
    assert caught.value.code == "password_same"
    with db.write(SYSTEM) as tx:
        sessions.change_password(tx, session, temporary, "новый-пароль-2026")
    assert _login(db, "admin", temporary).error.code == "login_failed"
    assert not _login(db, "admin", "новый-пароль-2026").access.must_change_password


def test_login_upgrades_an_old_hash(db, monkeypatch):
    admin = _admin(db)
    monkeypatch.setattr(passwords, "COST", passwords.COST + 1)
    assert _login(db).error is None
    with db.read() as conn:
        stored = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (admin["id"],)
        ).fetchone()[0]
    assert not passwords.needs_rehash(stored)


def _events(conn, entity_id):
    return audit.list_events(conn, entity_id=entity_id)
