"""События из журнала (API-09) и сигнал о записи для ожидающих."""

from __future__ import annotations

from engine import SYSTEM, Actor, access, events


def _viewer(db, sections):
    with db.write(SYSTEM) as tx:
        role = access.create_role(tx, "Смена " + "-".join(sorted(sections)))
        rights = [{"scope": "section", "target": name, "action": "use"} for name in sections]
        access.set_permissions(tx, role["id"], rights)
        user, _ = access.create_user(tx, "u" + str(len(sections)), "Сменщик", role["id"])
    with db.read() as conn:
        return access.load(conn, user["id"])


def test_imported_journal_and_foreign_rows_give_no_events(db):
    viewer = _viewer(db, {"chat"})
    with db.read() as conn:
        start = events.cursor(conn)
    with db.write(Actor(None, "Перенос")) as tx:
        tx.conn.execute(
            "INSERT INTO audit_log (at, actor_label, origin, action, entity_kind, entity_id)"
            " VALUES (?, 'Иванов (из Local_DB)', 'localdb', 'localdb.update_record', 'record', 'x')",
            (tx.now,),
        )
        tx.audit("chat.post", "chat_message", "m1")
        tx.audit("role.update", "role", "r1")
    with db.read() as conn:
        found = events.since(conn, viewer, start)
    assert [item["action"] for item in found["events"]] == ["chat.post"]
    assert found["cursor"] == start + 3 and not found["more"]  # курсор прошёл и невидимые


def test_listener_runs_after_commit_and_its_failure_keeps_the_data(db):
    calls = []
    db.on_commit(lambda: calls.append("ok"))
    db.on_commit(lambda: 1 / 0)
    with db.write(SYSTEM) as tx:
        tx.audit("kks.create", "kks", "k1")
    assert calls == ["ok"]
    try:
        with db.write(SYSTEM) as tx:
            tx.audit("kks.create", "kks", "k2")
            raise RuntimeError("отказ")
    except RuntimeError:
        pass
    assert calls == ["ok"]  # откат — не запись
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE entity_id = 'k1'").fetchone()[0]
