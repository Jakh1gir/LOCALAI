"""Резервные копии, разрешённые адреса, сертификат HTTPS (F-022, F-023, SEC-04, SEC-09)."""

from __future__ import annotations

import ssl
import struct
from pathlib import Path

import pytest
from cryptography import x509

from server import backups, tls
from server.__main__ import main as serve
from server.admin import main as admin_cli
from server.app import create_app

from .asgi import Client

PASSWORD = "пароль-копии-2026"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "live.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


def _sites(client):
    return [site["name"] for site in client.get("/sites").json()["items"]]


def _backup(admin, label=None):
    reply = admin.post("/backups", {"password": PASSWORD, "label": label})
    assert reply.status == 201, reply.json()
    return reply.json()


def _folder(app):
    return Path(app.state.db.path).parent / "backups"


# Копия и восстановление ---------------------------------------------------------


def test_backup_is_encrypted_and_listed(app, admin):
    admin.post("/sites", {"name": "Секретная площадка ТЭЦ"})
    made = _backup(admin, "Перед обновлением")
    assert made["label"] == "Перед обновлением" and made["schema_version"] >= 5
    assert admin.get("/backups").json()["items"] == [made]
    raw = (_folder(app) / made["name"]).read_bytes()
    assert raw.startswith(backups.MAGIC)
    assert "Секретная".encode() not in raw and b"SQLite format 3" not in raw  # SEC-09
    assert not list((_folder(app) / ".work").iterdir())  # открытой копии не осталось
    download = admin.get(f"/backups/{made['name']}/file")
    assert download.status == 200 and download.body == raw


def test_restore_returns_the_data_and_keeps_a_protective_backup(app, admin):
    admin.post("/sites", {"name": "ТЭЦ-1"})
    made = _backup(admin)
    admin.post("/sites", {"name": "ТЭЦ-2"})
    restored = admin.post(f"/backups/{made['name']}/restore", {"password": PASSWORD})
    assert restored.status == 200, restored.json()
    protective = restored.json()["protective_backup"]
    assert protective["label"] == backups.PROTECTIVE_LABEL and protective["protective"] is True
    assert restored.json()["restored"]["protective"] is False
    assert admin.get("/sites").json()["error"]["code"] == "session_invalid"  # сессии закрыты
    fresh = Client(app)
    assert _sites(fresh) == ["ТЭЦ-1"]
    events = fresh.get("/audit").json()["items"]
    assert [event["action"] for event in events[:2]] == ["auth.login", "backup.restore"]
    assert events[1]["details"]["protective_backup"] == protective["name"]
    # Ошибочное восстановление отменяется восстановлением из защитной копии.
    fresh.post(f"/backups/{protective['name']}/restore", {"password": PASSWORD})
    assert _sites(Client(app)) == ["ТЭЦ-1", "ТЭЦ-2"]


def test_wrong_password_leaves_the_database_untouched(app, admin):
    admin.post("/sites", {"name": "ТЭЦ-1"})
    made = _backup(admin)
    admin.post("/sites", {"name": "ТЭЦ-2"})
    reply = admin.post(f"/backups/{made['name']}/restore", {"password": "другой-пароль-12"})
    assert reply.status == 400 and reply.json()["error"]["code"] == "backup_password"
    assert _sites(admin) == ["ТЭЦ-1", "ТЭЦ-2"]
    assert len(admin.get("/backups").json()["items"]) == 1  # защитная копия не делалась


@pytest.mark.parametrize("damage", ["truncate", "flip", "header", "append"])
def test_damaged_backup_is_rejected_before_restore(app, admin, damage):
    admin.post("/sites", {"name": "ТЭЦ-1"})
    made = _backup(admin)
    path = _folder(app) / made["name"]
    raw = bytearray(path.read_bytes())
    header_end = len(backups.MAGIC) + 4 + struct.unpack(">I", raw[15:19])[0]
    if damage == "truncate":
        raw = raw[:-20]
    elif damage == "flip":
        raw[-5] ^= 1
    elif damage == "header":  # подмена подписи в открытом заголовке
        raw[header_end - 3] ^= 1
    else:
        raw += raw[header_end:]
    path.write_bytes(bytes(raw))
    admin.post("/sites", {"name": "ТЭЦ-2"})
    reply = admin.post(f"/backups/{made['name']}/restore", {"password": PASSWORD})
    assert reply.status == 400
    assert reply.json()["error"]["code"] in ("backup_corrupt", "backup_password")
    assert _sites(admin) == ["ТЭЦ-1", "ТЭЦ-2"]


def test_backup_names_cannot_leave_the_folder(app, admin):
    for name in ("..%2Flive.sqlite3", "live.sqlite3", "localai-x.localai-backup"):
        reply = admin.get(f"/backups/{name}/file")
        assert reply.status == 404, name


def test_short_backup_password_and_missing_right_are_refused(app, admin):
    reply = admin.post("/backups", {"password": "короткий"})
    assert reply.status == 400 and reply.json()["error"]["code"] == "password_too_short"
    roles = {role["name"]: role["id"] for role in admin.get("/roles").json()["items"]}
    created = admin.post(
        "/users", {"login": "eng", "name": "Инженер", "role_id": roles["Инженер"]}
    ).json()
    engineer = Client(app, login="eng", password=created["temporary_password"])
    engineer.post(
        "/auth/password",
        {"current_password": created["temporary_password"], "new_password": "пароль-инженера"},
    )
    denied = engineer.get("/backups")
    assert denied.status == 403 and denied.json()["error"]["details"]["permission"] == (
        "section.backups"
    )


def test_backup_from_a_newer_program_is_not_restored(app, admin, monkeypatch):
    made = _backup(admin)
    monkeypatch.setattr(backups.migrator, "available", lambda: [(1, "core", None)])
    reply = admin.post(f"/backups/{made['name']}/restore", {"password": PASSWORD})
    assert reply.status == 400 and reply.json()["error"]["code"] == "schema_newer"


# Разрешённые адреса ---------------------------------------------------------------


def test_allowed_addresses_close_other_workstations(app):
    admin = Client(app, address="10.0.5.7")
    assert Client(app, login=None, address="10.0.9.9").get("/system/health").status == 200
    locked_out = admin.post("/system/allowed-ips", {"network": "10.0.6.0/24"})
    assert locked_out.status == 409 and locked_out.json()["error"]["code"] == "network_lockout"
    added = admin.post("/system/allowed-ips", {"network": "10.0.5.0/24", "note": "Цех"})
    assert added.status == 201 and added.json()["network"] == "10.0.5.0/24"
    other = Client(app, login=None, address="10.0.9.9")
    blocked = other.post("/auth/login", {"login": "admin", "password": "что-угодно-12"})
    assert blocked.status == 403 and blocked.json()["error"]["code"] == "address_not_allowed"
    assert Client(app, login=None, address="127.0.0.1").get("/system/health").status == 200
    assert admin.get("/system/allowed-ips").json()["items"][0]["note"] == "Цех"
    entry = added.json()["id"]
    assert admin.delete(f"/system/allowed-ips/{entry}").status == 204  # пустой список — все
    assert other.get("/system/health").status == 200


def test_bad_network_is_explained(admin):
    reply = admin.post("/system/allowed-ips", {"network": "10.0.5.300"})
    assert reply.status == 400 and reply.json()["error"]["code"] == "network_invalid"


# HTTPS ---------------------------------------------------------------------------


def test_certificate_covers_names_and_addresses(tmp_path, capsys):
    db = tmp_path / "srv.sqlite3"
    assert (
        admin_cli(
            ["--db", str(db), "create-certificate", "--host", "localai", "--host", "10.0.5.1"]
        )
        == 0
    )
    out = capsys.readouterr().out
    folder = tmp_path / "tls"
    assert tls.fingerprint(folder / tls.CERT) in out
    cert = x509.load_pem_x509_certificate((folder / tls.CERT).read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["localai"]
    assert [str(ip) for ip in names.get_values_for_type(x509.IPAddress)] == ["10.0.5.1"]
    assert (folder / tls.KEY).stat().st_mode & 0o077 == 0  # ключ читает только владелец
    ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(
        folder / tls.CERT, folder / tls.KEY
    )
    assert admin_cli(["--db", str(db), "create-certificate", "--host", "x"]) == 1  # не затирает


def test_network_mode_requires_a_certificate(tmp_path, monkeypatch):
    started = {}
    monkeypatch.setattr(
        "server.__main__.uvicorn.run", lambda app, **options: started.update(options)
    )
    db = str(tmp_path / "srv.sqlite3")
    with pytest.raises(SystemExit, match="только по HTTPS"):
        serve(["--db", db, "--host", "0.0.0.0"])
    serve(["--db", db])  # этот компьютер — без сертификата можно
    assert "ssl_certfile" not in started
    tls.create(tmp_path / "tls", ["localai"])
    serve(["--db", db, "--host", "0.0.0.0"])
    assert started["ssl_certfile"].endswith(tls.CERT) and started["host"] == "0.0.0.0"


def test_label_alone_does_not_make_a_copy_protective(admin):
    made = _backup(admin, label=backups.PROTECTIVE_LABEL)
    listed = {item["name"]: item for item in admin.get("/backups").json()["items"]}
    assert listed[made["name"]]["label"] == backups.PROTECTIVE_LABEL
    assert listed[made["name"]]["protective"] is False
