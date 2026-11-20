"""Векторы фрагментов документов: что посчитать и как хранить (KNOW-02, AI-05).

Вектор считает модель векторов в ИИ-службе; сервер в фоне берёт фрагменты
без вектора текущей модели (``pending``), отправляет их службе и сохраняет
ответ (``store``). Вектор обрезается до ``DIM`` чисел и нормируется к длине
1: модели семейства Qwen3-Embedding обучены так, что первые числа вектора
несут главный смысл, а короткий вектор вдвое экономит память поиска.

Считаются только фрагменты действующей версии документа не из архива.
Сам поиск — ``server/vector_index.py``: косинусное сходство в памяти.
"""

from __future__ import annotations

import math
import sqlite3
from array import array
from typing import Any

from engine.database import Tx
from engine.errors import EngineError

DIM = 512
BATCH = 16

_CURRENT = (
    "FROM chunks c JOIN documents d ON d.seq = c.document_seq"
    " WHERE c.version = d.version AND d.archived_at IS NULL"
)


def pending(conn: sqlite3.Connection, model: str, limit: int = BATCH) -> list[dict[str, Any]]:
    """Фрагменты без вектора этой модели — новые сначала: свежие документы находятся раньше."""
    rows = conn.execute(
        f"SELECT c.seq, c.text {_CURRENT} AND NOT EXISTS (SELECT 1 FROM chunk_vectors v"
        " WHERE v.chunk_seq = c.seq AND v.model = ?) ORDER BY c.seq DESC LIMIT ?",
        (model, limit),
    ).fetchall()
    return [{"seq": row["seq"], "text": row["text"]} for row in rows]


def store(tx: Tx, model: str, items: list[tuple[int, list[float]]]) -> int:
    """Сохранить векторы; фрагмент, удалённый за время расчёта, пропускается."""
    saved = 0
    for seq, raw in items:
        vector = normalize(raw)
        exists = tx.conn.execute("SELECT 1 FROM chunks WHERE seq = ?", (seq,)).fetchone()
        if exists is None:
            continue
        tx.conn.execute(
            "INSERT INTO chunk_vectors (chunk_seq, model, dim, vector) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (chunk_seq) DO UPDATE SET model = excluded.model, dim = excluded.dim,"
            " vector = excluded.vector",
            (seq, model, len(vector), array("f", vector).tobytes()),
        )
        saved += 1
    return saved


def normalize(raw: list[float]) -> list[float]:
    if not isinstance(raw, list) or len(raw) < 16:
        raise EngineError("vector_invalid", "Вектор фрагмента не разобран")
    cut = [float(item) for item in raw[:DIM]]
    length = math.sqrt(sum(item * item for item in cut))
    if length == 0 or not math.isfinite(length):
        raise EngineError("vector_invalid", "Вектор фрагмента пустой")
    return [item / length for item in cut]


def counts(conn: sqlite3.Connection, model: str | None) -> dict[str, Any]:
    """Сколько фрагментов действующих документов уже с векторами модели."""
    total = conn.execute(f"SELECT COUNT(*) {_CURRENT}").fetchone()[0]
    done = 0
    if model:
        done = conn.execute(
            f"SELECT COUNT(*) {_CURRENT} AND EXISTS (SELECT 1 FROM chunk_vectors v"
            " WHERE v.chunk_seq = c.seq AND v.model = ?)",
            (model,),
        ).fetchone()[0]
    return {"model": model, "done": done, "total": total}
