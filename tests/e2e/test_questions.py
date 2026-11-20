"""Валидатор и эталоны через настоящий HTTP, без модели и без правки источника."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest

from engine import SYSTEM, open_database, structure
from tests.e2e.question_data import add_signals
from tools.generate import generate_database
from tools.questions.__main__ import main
from tools.questions.answers import Canonical
from tools.questions.format import DEFAULT_SET, QuestionError, load_suite, pointer, render
from tools.questions.runner import run_suite
from tools.questions.workspace import snapshot

ROOT = Path(__file__).resolve().parents[2]


def _raw() -> dict:
    return json.loads(DEFAULT_SET.read_text(encoding="utf-8"))


def _save(folder: Path, raw: dict) -> Path:
    target = folder / "questions.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "unknown_role",
        "unknown_profile",
        "extra_key",
        "write",
        "unknown_path",
        "wrong_path_param",
        "unknown_query",
        "extra_body",
        "wrong_limit",
        "bad_op",
        "empty_in",
        "nested_any",
        "bad_sort",
        "forward_reference",
        "broken_reference",
        "unknown_answer",
        "wrong_pointer",
        "missing_source",
        "missing_location",
    ],
)
def test_validator_rejects_unusable_expected_queries(tmp_path: Path, change: str) -> None:
    raw = _raw()
    question = raw["questions"][4]
    step = question["expected"]["steps"][0]
    if change == "duplicate":
        raw["questions"].append(deepcopy(question))
    elif change in ("unknown_role", "unknown_profile"):
        question[change.removeprefix("unknown_")] = "unknown"
    elif change == "extra_key":
        question["typo"] = True
    elif change == "write":
        step.update(path="/api/v1/datasets/{id}/records", body={"values": {}})
    elif change == "unknown_path":
        step["path"] = "/api/v1/no-such-route"
    elif change == "wrong_path_param":
        step["path_params"] = {"dataset": "{{operating.id}}"}
    elif change == "unknown_query":
        step["query"] = {"password": "нет"}
    elif change == "extra_body":
        step["body"] = {"limti": 100}
    elif change == "wrong_limit":
        step["path"] = "/api/v1/datasets/{id}/records/query"
        step["body"] = {"limit": 201}
    elif change == "bad_op":
        step["body"] = {"where": [{"field": "quantity", "op": "equals", "value": 1}]}
    elif change == "empty_in":
        step["body"] = {"where": [{"field": "quantity", "op": "in", "value": []}]}
    elif change == "nested_any":
        step["body"] = {"where": [{"any": [{"any": []}]}]}
    elif change == "bad_sort":
        step["path"] = "/api/v1/datasets/{id}/records/query"
        step["body"] = {"sort": {"field": "quantity", "dir": "random"}}
    elif change == "forward_reference":
        step["path_params"]["id"] = "{{result.id}}"
    elif change == "broken_reference":
        step["path_params"]["id"] = "{{operating.id}"
    elif change == "unknown_answer":
        question["expected"]["answer"]["step"] = "unknown"
    elif change == "wrong_pointer":
        question["expected"]["answer"]["pointer"] = "count"
    else:
        question.update(kind="document", expected={"type": "text", "answer": "Ответ"})
        if change == "missing_location":
            question["expected"]["source"] = {"document": "РЭ-1.pdf", "revision": "1"}
    with pytest.raises(QuestionError):
        load_suite(_save(tmp_path, raw))


def test_duplicate_json_key_is_not_silently_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "questions.json"
    target.write_text('{"version": 1, "version": 2}', encoding="utf-8")
    with pytest.raises(QuestionError, match="Повтор ключа"):
        load_suite(target)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_numbers_are_refused(tmp_path: Path, value: str) -> None:
    target = tmp_path / "questions.json"
    target.write_text('{"version": ' + value + "}", encoding="utf-8")
    with pytest.raises(QuestionError, match="JSON не поддерживает"):
        load_suite(target)


def test_business_fields_with_metadata_names_are_preserved() -> None:
    answer = {
        "created_at": "Служебная отметка",
        "doc": {
            "created_at": "2020-03-01",
            "next_cursor": "Марка изделия",
            "archived_at": "Значение поля",
        },
    }
    assert Canonical({}, {}).normalize(answer) == {"doc": answer["doc"]}


def test_document_and_diagram_questions_keep_source_and_are_manual(tmp_path: Path) -> None:
    raw = _raw()
    question = raw["questions"][0]
    question.update(
        kind="document",
        expected={
            "type": "text",
            "answer": "Проверить положение клапана.",
            "source": {"document": "РЭ-001.pdf", "revision": "2", "page": 14},
        },
    )
    suite = load_suite(_save(tmp_path, raw))
    assert suite.questions[0].expected.source.page == 14
    question.update(kind="diagram")
    question["expected"]["source"] = {"document": "Схема.pdf", "revision": "A", "sheet": "Лист 3"}
    suite = load_suite(_save(tmp_path, raw))
    source = tmp_path / "source.sqlite3"
    generate_database(source, 100, 105)
    suite.questions = suite.questions[:1]
    report = run_suite(suite, source, ["c101"])
    assert report["summary"] == {"ok": 0, "failed": 0, "manual": 1}
    assert report["results"][0]["expected"]["source"]["sheet"] == "Лист 3"


def test_references_and_json_pointer_preserve_types() -> None:
    context = {"page": {"items": [{"value": 12}]}, "label": {"value": "К-001"}}
    assert render("{{page.items.0.value}}", context) == 12
    assert render("Кабель {{label.value}}", context) == "Кабель К-001"
    assert pointer({"a/b": {"~": [12]}}, "/a~1b/~0/0") == 12
    with pytest.raises(QuestionError):
        render("{{page.items.9.value}}", context)
    with pytest.raises(QuestionError):
        pointer({"count": 1}, "/missing")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_all_thirty_answers_are_reproducible_and_enforce_roles(tmp_path: Path) -> None:
    reports = []
    for name in ("first", "second"):
        source = tmp_path / f"{name}.sqlite3"
        generate_database(source, 200, 105)
        add_signals(source)
        before = _digest(source)
        files = set(tmp_path.iterdir())
        report = run_suite(load_suite(), source, ["c101", "c104"])
        assert report["summary"] == {"ok": 30, "failed": 0, "manual": 0}, report["results"]
        assert _digest(source) == before
        added = set(tmp_path.iterdir()) - files
        assert added <= {source.with_name(source.name + suffix) for suffix in ("-wal", "-shm")}
        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as conn:
            assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        reports.append(report)
    assert reports[0] == reports[1]
    answers = {item["id"]: item for item in reports[0]["results"]}
    assert answers["Q-001"]["answer"] == "10LAC10AP001"
    assert answers["Q-021"]["answer"] == "10LAC10AP001"
    assert answers["Q-023"]["answer"] == 9
    assert answers["Q-024"]["answer"] == 3
    assert answers["Q-028"]["answer"] == 3
    assert answers["Q-029"]["answer"] == "forbidden"
    assert answers["Q-030"]["answer"]["via_links"] == []
    assert all("/Сигналы" in item["dataset_id"] for item in answers["Q-030"]["answer"]["usage"])
    assert any("/Кабели" in item["dataset_id"] for item in answers["Q-027"]["answer"])
    assert "/Кабели" not in json.dumps(answers["Q-030"], ensure_ascii=False)
    with closing(sqlite3.connect(source)) as conn:
        total = conn.execute(
            "SELECT count(*) FROM records r JOIN datasets d ON d.seq=r.dataset_seq WHERE d.name='В эксплуатации' AND r.archived_at IS NULL"
        ).fetchone()[0]
    assert answers["Q-005"]["answer"] == total
    text = json.dumps(reports[0], ensure_ascii=False)
    assert "password" not in text and "localai_session" not in text


def test_snapshot_sees_committed_wal_but_not_uncommitted_changes(tmp_path: Path) -> None:
    source, target = tmp_path / "live.sqlite3", tmp_path / "copy.sqlite3"
    generate_database(source, 100, 105)
    with closing(open_database(str(source))) as db:
        with db.write(SYSTEM) as tx:
            site = structure.list_sites(tx.conn)[0]
            structure.rename_site(tx, site["id"], "Зафиксированное имя")
        with db.write(SYSTEM) as tx:
            structure.rename_site(tx, site["id"], "Незавершённая правка")
            snapshot(source, target)
        with closing(open_database(str(target))) as copy, copy.read() as conn:
            assert structure.list_sites(conn)[0]["name"] == "Зафиксированное имя"


def test_cli_checks_and_runs_default_profile(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    generate_database(source, 100, 105)
    output = tmp_path / "reference.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.questions",
            "run",
            "--db",
            str(source),
            "--out",
            str(output),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["ok"] == 20 and report["excluded"] == 10
    assert "вне профиля: 10" in result.stdout
    assert main(["check"]) == 0


def test_wrong_expected_status_fails_and_cli_protects_existing_files(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    generate_database(source, 100, 105)
    raw = _raw()
    raw["questions"] = [raw["questions"][18]]
    raw["questions"][0]["expected"]["steps"][0].update(status=200, error_code=None)
    suite_path = _save(tmp_path, raw)
    output = tmp_path / "failed.json"
    assert main(["run", "--set", str(suite_path), "--db", str(source), "--out", str(output)]) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["failed"] == 1
    before = _digest(source)
    assert main(["run", "--db", str(source), "--out", str(source)]) == 1
    linked = tmp_path / "linked.json"
    linked.hardlink_to(source)
    assert main(["run", "--db", str(source), "--out", str(linked)]) == 1
    assert _digest(source) == before
    for suffix in ("-wal", "-shm", "-journal"):
        output = source.with_name(source.name + suffix)
        before_output = output.read_bytes() if output.exists() else None
        assert main(["run", "--db", str(source), "--out", str(output)]) == 1
        assert (output.read_bytes() if output.exists() else None) == before_output
