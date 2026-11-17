"""Проверка генератора через командную строку и сохранённую базу."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _generate(path: Path, count: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.generate", "--records", str(count),
         "--out", str(path), "--seed", "1"],
        cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True, encoding="utf-8", check=False,
    )


def _inspect(path: Path) -> tuple:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        assert conn.execute("SELECT name FROM sites").fetchall() == [("ТЭЦ-1",)]
        assert conn.execute("SELECT name FROM types").fetchall() == [("Оборудование",)]
        assert conn.execute("SELECT name FROM datasets ORDER BY seq").fetchall() == [
            ("В эксплуатации",), ("Склад",),
        ]
        fields = conn.execute("SELECT key, kind, multiple, dictionary_id FROM fields").fetchall()
        assert Counter(kind for _, kind, _, _ in fields) == {
            "kks": 1, "choice": 12, "integer": 4, "date": 2, "text": 2,
        }
        assert {key for key, _, multiple, _ in fields if multiple} == {"kks"}
        dictionaries = {
            key: dictionary_id for key, kind, _, dictionary_id in fields if kind == "choice"
        }
        sizes = dict(conn.execute(
            "SELECT dictionary_id, count(*) FROM dictionary_values GROUP BY dictionary_id"
        ))
        assert len(sizes) == len(set(dictionaries.values())) == 12
        assert all(4 <= sizes[dictionary_id] <= 400 for dictionary_id in dictionaries.values())
        synonym_row = conn.execute(
            "SELECT synonyms FROM dictionary_values WHERE dictionary_id = ? AND value = ?",
            (dictionaries["manufacturer"], "Siemens"),
        ).fetchone()
        assert "Сименс" in json.loads(synonym_row[0])
        labels = dict(conn.execute("SELECT id, value FROM dictionary_values"))
        codes = dict(conn.execute("SELECT id, code FROM kks_objects"))
        assert "10LAC10AP001" in codes.values()

        changes_by_record = defaultdict(list)
        for seq, version, op, raw in conn.execute(
            "SELECT record_seq, version, op, changes FROM record_versions"
            " ORDER BY record_seq, version"
        ):
            changes = json.loads(raw)
            if op == "update":
                assert {"state", "quantity"} <= changes.keys()
            for key, pair in changes.items():
                if key in dictionaries:
                    changes[key] = [labels[value] if value is not None else None for value in pair]
            changes_by_record[seq].append((version, op, changes))

        rows = []
        codes_by_dataset = defaultdict(set)
        for seq, dataset, version, archived, raw in conn.execute(
            "SELECT r.seq, d.name, r.version, r.archived_at IS NOT NULL, r.doc FROM records r"
            " JOIN datasets d ON d.seq = r.dataset_seq ORDER BY r.seq"
        ):
            doc = json.loads(raw)
            for key in dictionaries:
                doc[key] = labels[doc[key]]
            doc["kks"] = [codes[object_id] for object_id in doc["kks"]]
            assert 1 <= len(doc["kks"]) <= 2
            assert doc["serial"]
            interval = date.fromisoformat(doc["next_inspection"]) - date.fromisoformat(
                doc["last_inspection"]
            )
            assert interval.days in (180, 365)
            history = changes_by_record[seq]
            assert [item[0] for item in history] == list(range(1, version + 1))
            assert [item[1] for item in history] == (
                ["create"] + ["update"] * (version - 1 - archived) + ["archive"] * archived
            )
            assert 1 <= version <= 4 and (not archived or version >= 3)
            codes_by_dataset[dataset].update(doc["kks"])
            rows.append((dataset, version, archived, doc, history))

        assert Counter(row[0] for row in rows) == {"В эксплуатации": 240, "Склад": 60}
        assert sum(row[1] > 1 for row in rows) == 120
        assert 6 <= sum(row[2] for row in rows) <= 12
        assert len({row[3]["serial"] for row in rows}) == 300
        assert sum("note" in row[3] for row in rows) == 90
        assert sum(len(row[3]["kks"]) == 2 for row in rows) == 45
        assert any(
            "manufacturer" in changes
            for log in changes_by_record.values()
            for _, op, changes in log if op == "update"
        )
        assert codes_by_dataset["В эксплуатации"] & codes_by_dataset["Склад"]
        assert conn.execute("SELECT count(*) FROM record_kks_links").fetchone() == (345,)
    return rows, sorted(codes.values())


def test_cli_generates_reproducible_data(tmp_path: Path) -> None:
    first = tmp_path / "первая база.sqlite3"
    second = tmp_path / "повтор.sqlite3"
    for path in (first, second):
        result = _generate(path)
        assert result.returncode == 0, result.stderr
        assert path.is_file()
    assert _inspect(first) == _inspect(second)


def test_cli_preserves_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "существующая база.sqlite3"
    contents = b"original content"
    path.write_bytes(contents)
    result = _generate(path)
    assert result.returncode == 2
    assert path.name in result.stderr and "уже существует" in result.stderr
    assert path.read_bytes() == contents


def test_cli_rejects_zero_records(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    result = _generate(path, count=0)
    assert result.returncode == 2
    assert "положительным целым числом" in result.stderr
    assert not path.exists()
