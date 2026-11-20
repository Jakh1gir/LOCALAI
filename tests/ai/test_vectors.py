"""Поиск по смыслу (KNOW-02, AI-05): векторы фрагментов, индекс в памяти, гибридный поиск.

Модель векторов в тестах — счётчик слов одного смысла (tests/ai/conftest.py,
``Meaning``): «давление» и «напор» близки, хотя общих слов у них нет.
Данные вымышленные.
"""

from __future__ import annotations

import math
from array import array

import pytest

from ai.model import Chunk
from engine import SYSTEM, vectors
from server.vector_index import ExtensionIndex, VectorIndex, choose
from server.worker import Embedder, run_pending

from .conftest import ask, tool, tool_results, user


def _upload(admin, app, name, body, type_id=None):
    query = {"name": name}
    if type_id:
        query["type_id"] = type_id
    document = admin.request("POST", "/documents", raw=body.encode(), query=query, key="").json()
    run_pending(app.state.db, app.state.files)
    return document


def _embed_all(app):
    return Embedder(app.state.db, app.state.ai).run()


@pytest.fixture
def library(app, admin, plant):
    passport = _upload(admin, app, "passport.txt", "Питательный агрегат. Рабочий напор 16 кгс.")
    cables = _upload(admin, app, "cables.txt", "Провода и жилы контрольных линий шкафа.")
    both = _upload(admin, app, "pump.txt", "Давление насоса проверяется по манометру.")
    return {"passport": passport, "cables": cables, "both": both}


def test_worker_computes_vectors_once(app, admin, library, link):
    with app.state.db.read() as conn:
        assert vectors.counts(conn, "смысл-тест") == {"model": "смысл-тест", "done": 0, "total": 3}
    assert _embed_all(app) == 3
    calls = link.meaning.calls
    assert _embed_all(app) == 0 and link.meaning.calls == calls  # всё посчитано — служба не нужна
    status = admin.get("/ai/status").json()
    assert status["vectors"] | {"index": None} == {"model": "смысл-тест", "done": 3, "total": 3,
                                                   "index": None}  # fmt: skip
    assert status["embedding"]["name"] == "смысл-тест"

    admin.request(  # новая версия документа — старые фрагменты и их векторы уходят
        "PUT", f"/documents/{library['cables']['id']}/file",
        raw="Кабели питания насосов.".encode(), query={"name": "cables.txt", "version": 1}, key="",
    )  # fmt: skip
    run_pending(app.state.db, app.state.files)
    with app.state.db.read() as conn:
        assert vectors.counts(conn, "смысл-тест")["done"] == 2
        stored = conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
    assert stored == 2  # каскад: фрагменты прежней версии удалены вместе с векторами
    assert _embed_all(app) == 1


def test_hybrid_search_finds_by_meaning_and_by_words(app, admin, library):
    _embed_all(app)
    plain = admin.get("/search", q="давление насоса").json()
    assert plain["meaning"] is False
    assert [item["document_id"] for item in plain["documents"]] == [library["both"]["id"]]

    mixed = admin.get("/search", q="давление насоса", mode="hybrid").json()
    assert mixed["meaning"] is True
    found = [(item["document_id"], item["via"]) for item in mixed["documents"]]
    assert found[0] == (library["both"]["id"], "both")  # и слова, и смысл — первым
    assert (library["passport"]["id"], "meaning") in found  # «напор агрегата» — без общих слов
    assert found.index((library["passport"]["id"], "meaning")) < len(found)
    assert admin.get("/search", q="x", mode="эвристика").status == 400


def test_hybrid_search_respects_document_rights(app, admin, plant, library):
    _embed_all(app)
    types = {item["name"]: item["id"] for item in admin.get("/document-types").json()["items"]}
    secret = _upload(admin, app, "secret.txt", "Напор и давление резервного агрегата.",
                     type_id=types["Схема"])  # fmt: skip
    _embed_all(app)
    reader = user(app, admin, "reader", [
        {"scope": "section", "target": "ai", "action": "use"},
        {"scope": "doc_type", "target": types["Документ"], "action": "view"},
    ])  # fmt: skip
    found = reader.get("/search", q="давление", mode="hybrid").json()["documents"]
    assert secret["id"] not in [item["document_id"] for item in found]
    assert library["passport"]["id"] in [item["document_id"] for item in found]


def test_without_ai_service_search_falls_back_to_words(app, admin, library, link):
    _embed_all(app)
    link.up = False
    found = admin.get("/search", q="давление насоса", mode="hybrid").json()
    assert found["meaning"] is False
    assert [item["document_id"] for item in found["documents"]] == [library["both"]["id"]]


def test_ai_finds_document_by_meaning(app, admin, library, model):
    _embed_all(app)
    model.script = lambda messages, tools: (
        [tool("search_documents", text="давление насоса")] if not tool_results(messages)
        else [Chunk("Напор указан в паспорте [1].")]
    )  # fmt: skip
    _, message = ask(admin, "Где написано про давление насоса?")
    result = tool_results(model.seen[-1][0])[0]
    by = {item["title"]: item["found_by"] for item in result["documents"]}
    assert by[library["passport"]["title"]] == "meaning"
    assert message["sources"][0]["kind"] == "document"


# Индекс в памяти --------------------------------------------------------------------------


def test_index_grows_reloads_and_switches_model(app, admin, library):
    index = VectorIndex()
    db = app.state.db
    with db.read() as conn:
        seqs = [row[0] for row in conn.execute("SELECT seq FROM chunks ORDER BY seq")]

    def put(model, items):
        with db.write(SYSTEM) as tx:
            vectors.store(tx, model, items)

    one_hot = lambda place: [1.0 if i == place else 0.0 for i in range(32)]
    put("m1", [(seqs[0], one_hot(0)), (seqs[1], one_hot(1))])
    with db.read() as conn:
        assert index.search(conn, "m1", one_hot(1), 1)[0][0] == seqs[1]
    put("m1", [(seqs[2], one_hot(2))])  # догрузка новых векторов
    with db.read() as conn:
        hits = index.search(conn, "m1", one_hot(2), 3)
        assert hits[0] == (seqs[2], pytest.approx(1.0, abs=1e-3)) and len(hits) == 3
        assert index.search(conn, "m2", one_hot(0), 3) == []  # другая модель — своих векторов нет
    with db.write(SYSTEM) as tx:  # удаление — индекс собирается заново
        tx.conn.execute("DELETE FROM chunk_vectors WHERE chunk_seq = ?", (seqs[0],))
    put("m1", [(seqs[1], one_hot(5))])
    with db.read() as conn:
        assert [seq for seq, _ in index.search(conn, "m1", one_hot(5), 3)] == [seqs[1], seqs[2]]
    with pytest.raises(Exception):  # noqa: B017 — короткий вектор не принимается
        vectors.normalize([1.0, 2.0])


# Дополнение sqlite-vec: догоняние основной базы ---------------------------------------------


class TableIndex(ExtensionIndex):
    """ExtensionIndex, где vec0 заменён обычной таблицей: проверяется логика догоняния.

    Сам vec0 проверяется на месте командой ``python -m server.admin … check-vectors``.
    """

    def __init__(self, path):
        super().__init__(path, loader=lambda conn: "таблица")

    def _create(self, side, dim):
        side.execute("CREATE TABLE vec_chunks (rowid INTEGER PRIMARY KEY, embedding BLOB)")

    def _knn(self, side, blob, k):
        wanted = array("f", blob)
        scored = []
        for rowid, raw in side.execute("SELECT rowid, embedding FROM vec_chunks"):
            other = array("f", raw)
            scored.append((rowid, math.dist(wanted, other)))
        return sorted(scored, key=lambda item: item[1])[:k]


def test_extension_index_follows_main_database(app, admin, library, tmp_path):
    index = TableIndex(tmp_path / "vectors.sqlite3")
    assert index.available and index.status()["kind"] == "extension"
    db = app.state.db
    with db.read() as conn:
        seqs = [row[0] for row in conn.execute("SELECT seq FROM chunks ORDER BY seq")]

    def put(model, items):
        with db.write(SYSTEM) as tx:
            vectors.store(tx, model, items)

    one_hot = lambda place: [1.0 if i == place else 0.0 for i in range(32)]
    put("m1", [(seqs[0], one_hot(0)), (seqs[1], one_hot(1))])
    with db.read() as conn:
        hits = index.search(conn, "m1", one_hot(1), 2)
    assert hits[0] == (seqs[1], pytest.approx(1.0)) and hits[1][1] == pytest.approx(0.0)
    put("m1", [(seqs[2], one_hot(2))])  # новые векторы догоняются
    with db.read() as conn:
        assert index.search(conn, "m1", one_hot(2), 1)[0][0] == seqs[2]
    with db.write(SYSTEM) as tx:  # удаление в основной базе — и в индексе
        tx.conn.execute("DELETE FROM chunk_vectors WHERE chunk_seq = ?", (seqs[0],))
    with db.read() as conn:
        assert seqs[0] not in [seq for seq, _ in index.search(conn, "m1", one_hot(0), 5)]
    assert index.status()["rows"] == 2
    with db.write(SYSTEM) as tx:  # смена модели — индекс собирается заново
        tx.conn.execute("DELETE FROM chunk_vectors")
    put("m2", [(seqs[1], one_hot(4))])
    with db.read() as conn:
        assert index.search(conn, "m2", one_hot(4), 3) == [(seqs[1], pytest.approx(1.0))]
    reopened = TableIndex(tmp_path / "vectors.sqlite3")  # после перезапуска сервера
    with db.read() as conn:
        assert reopened.search(conn, "m2", one_hot(4), 3)[0][0] == seqs[1]


def test_missing_extension_falls_back_to_memory(tmp_path, admin, plant):
    chosen = choose(tmp_path, "auto")
    assert chosen.kind == "memory" and "sqlite_vec" in chosen.status()["extension"]
    assert not (tmp_path / "vectors.sqlite3").exists()  # файл индекса — только с дополнением
    assert choose(tmp_path, "memory").status()["extension"] is None
    with pytest.raises(ValueError):
        choose(tmp_path, "диск")
    index = admin.get("/ai/status").json()["vectors"]["index"]
    assert index["kind"] == "memory"
