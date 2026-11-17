"""Структура как данные, миграции и правила репозитория (DATA-01, DATA-11, ARCH-01, 03, 05)."""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from engine import Database, EngineError, migrator, open_database, query, records, structure

from .conftest import ENGINEER

ROOT = Path(__file__).resolve().parents[2]


def test_new_type_field_and_dataset_need_no_migration(db, plant):
    with db.read() as conn:
        before = migrator.current_version(conn)
    with db.write(ENGINEER) as tx:
        coupling = structure.create_type(tx, "Кабельная муфта")
        mark = structure.add_field(tx, coupling["id"], "Марка", "text", filterable=True)
        dataset = structure.create_dataset(tx, plant["site"]["id"], coupling["id"], "Муфты")
        records.create(tx, dataset["id"], {mark["key"]: "СТп-10"})
    with db.read() as conn:
        page = query.list_records(
            conn, dataset["id"], {"where": [{"field": mark["key"], "op": "eq", "value": "СТп-10"}]}
        )
        assert migrator.current_version(conn) == before
    assert len(page["items"]) == 1


def test_rename_changes_nothing_but_the_label(db, plant):
    keys = plant["keys"]
    with db.write(ENGINEER) as tx:
        record = records.create(tx, plant["dataset"]["id"], {keys["kks"]: "A1", keys["name"]: "Насос"})
        structure.update_field(tx, plant["fields"]["name"]["id"], name="Название оборудования")
        structure.rename_type(tx, plant["type"]["id"], "Оборудование ТЭЦ")
        structure.rename_dataset(tx, plant["dataset"]["id"], "Эксплуатация-2026")
    with db.read() as conn:
        kind = structure.get_type(conn, plant["type"]["id"])
        same = records.get(conn, record["id"])
    renamed = next(field for field in kind["fields"] if field["id"] == plant["fields"]["name"]["id"])
    assert renamed["key"] == keys["name"] and renamed["name"] == "Название оборудования"
    assert same["doc"][keys["name"]] == "Насос" and same["version"] == 1
    assert kind["schema_rev"] > plant["type"]["schema_rev"]


def test_names_are_unique_ignoring_case_and_spaces(db, plant):
    for action in (
        lambda tx: structure.create_site(tx, " тэц-1 "),
        lambda tx: structure.create_type(tx, "ОБОРУДОВАНИЕ"),
        lambda tx: structure.add_field(tx, plant["type"]["id"], "наименование", "text"),
        lambda tx: structure.create_dataset(
            tx, plant["site"]["id"], plant["type"]["id"], "эксплуатация"
        ),
        lambda tx: structure.add_dictionary_value(tx, plant["makers"]["id"], "сименс"),
    ):
        with pytest.raises(EngineError) as caught:
            with db.write(ENGINEER) as tx:
                action(tx)
        assert caught.value.code in {"name_taken", "value_taken"}


def test_archived_field_keeps_values_and_leaves_search(db, plant):
    keys = plant["keys"]
    with db.write(ENGINEER) as tx:
        record = records.create(
            tx, plant["dataset"]["id"], {keys["kks"]: "A1", keys["name"]: "Секретный насос"}
        )
        structure.archive_field(tx, plant["fields"]["name"]["id"])
    with db.read() as conn:
        kept = records.get(conn, record["id"])["doc"][keys["name"]]
        found = query.list_records(conn, plant["dataset"]["id"], {"search": "секретный"})
    assert kept == "Секретный насос" and found["items"] == []
    with pytest.raises(EngineError) as caught:
        with db.write(ENGINEER) as tx:
            records.update(tx, record["id"], {keys["name"]: "x"}, 1)
    assert caught.value.code == "field_archived"


def test_failed_action_leaves_no_trace(db, plant):
    with pytest.raises(RuntimeError):
        with db.write(ENGINEER) as tx:
            structure.create_type(tx, "Временный")
            raise RuntimeError("сбой посреди действия")
    with db.read() as conn:
        names = [kind["name"] for kind in structure.list_types(conn)]
        events = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE details LIKE '%Временный%'"
        ).fetchone()[0]
    assert "Временный" not in names and events == 0


def test_database_settings_and_foreign_keys(db, plant):
    conn = db.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO records (id, dataset_seq, version, doc, created_at, updated_at)"
            " VALUES ('x', 999999, 1, '{}', '', '')"
        )


def _with_extra_migration(tmp_path, monkeypatch, name: str, sql: str) -> int:
    """Копия настоящих миграций плюс одна следующая; возвращает её номер."""
    extra = tmp_path / "migrations"
    extra.mkdir()
    for source in migrator.MIGRATIONS_DIR.glob("*.sql"):
        (extra / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    number = len(migrator.available()) + 1
    (extra / f"{number:04d}_{name}.sql").write_text(sql, encoding="utf-8")
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", extra)
    return number


def test_migration_backs_up_existing_database(tmp_path, monkeypatch):
    path = tmp_path / "base.sqlite3"
    open_database(str(path)).close()
    latest = len(migrator.available())
    number = _with_extra_migration(
        tmp_path, monkeypatch, "probe", "CREATE TABLE probe (id INTEGER PRIMARY KEY);"
    )
    db = Database(str(path))
    assert migrator.migrate(db) == [number]
    copies = list(tmp_path.glob(f"base.schema{latest:04d}.*.sqlite3"))
    assert len(copies) == 1  # копия сделана до изменения схемы
    copy = sqlite3.connect(copies[0])
    assert copy.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == latest
    copy.close()
    db.close()
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", ROOT / "engine" / "migrations")
    with pytest.raises(EngineError) as caught:
        migrator.migrate(Database(str(path)))  # база новее программы
    assert caught.value.code == "schema_newer"


def test_broken_migration_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "base.sqlite3"
    open_database(str(path)).close()
    latest = len(migrator.available())
    _with_extra_migration(
        tmp_path, monkeypatch, "broken", "CREATE TABLE half (id INTEGER); СЛОМАНО;"
    )
    db = Database(str(path))
    with pytest.raises(EngineError):
        migrator.migrate(db)
    conn = db.connect()
    assert migrator.current_version(conn) == latest
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'half'").fetchone()[0] == 0
    db.close()


# Правила репозитория -----------------------------------------------------------

_PYTHON = [
    path for folder in ("engine", "server", "ai") for path in (ROOT / folder).rglob("*.py")
]


def test_engine_does_not_import_other_components():
    for path in (ROOT / "engine").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert name.split(".")[0] not in {"server", "ai", "vision"}, f"{path}: {name}"


def test_functions_and_modules_stay_small():
    for path in _PYTHON:
        source = path.read_text(encoding="utf-8")
        assert source.count("\n") <= 800, f"{path}: модуль длиннее 800 строк"
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                assert length <= 150, f"{path}:{node.name}: функция длиннее 150 строк"


def test_file_names_carry_no_versions_or_patch_words():
    banned = re.compile(r"(patch|fix|hotfix|_v\d+|\d{4}-\d{2}-\d{2}|20\d{6})", re.IGNORECASE)
    for path in _PYTHON:
        assert not banned.search(path.stem), f"{path}: имя нарушает ARCH-05"
