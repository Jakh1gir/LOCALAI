"""Поиск по смыслу: косинусное сходство векторов фрагментов в памяти сервера (AI-05).

Расширение SQLite не нужно: векторы лежат в таблице ``chunk_vectors``, а
сервер держит их в памяти матрицей float32 и считает сходство с вектором
вопроса одним умножением (numpy). Это точный поиск, без приближений.

Память: 512 чисел × 4 байта = 2 КБ на фрагмент — 100 000 фрагментов
(≈ 2 000 документов по 50 страниц) занимают около 200 МБ; поиск по ним —
около 35 мс, первая загрузка — меньше секунды (замер 20.11.2026). Новые векторы догружаются по мере появления; удаление
или смена модели — полная перезагрузка.

Права здесь не проверяются: индекс возвращает номера фрагментов, а видимость
документа, архив и версию проверяет ``engine.documents.hybrid``.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from engine import vectors

try:
    import numpy as np
except ImportError:  # без numpy поиск по смыслу выключен, полнотекстовый работает
    np = None  # type: ignore[assignment]

BLOCK = 65536  # строк матрицы за одно умножение: память под промежуточный результат


class VectorIndex:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[Any, ...] | None = None
        self._seqs: Any = None
        self._matrix: Any = None

    @property
    def available(self) -> bool:
        return np is not None

    def search(
        self, conn: sqlite3.Connection, model: str, query: list[float], k: int
    ) -> list[tuple[int, float]]:
        """Номера фрагментов, ближайших по смыслу, и сходство от 1 (то же) до −1."""
        if np is None or not model:
            return []
        self._refresh(conn, model)
        with self._lock:
            matrix, seqs = self._matrix, self._seqs
        if not len(seqs):
            return []
        wanted = np.asarray(vectors.normalize(query)[: matrix.shape[1]], dtype=np.float32)
        if len(wanted) != matrix.shape[1]:
            return []  # вектор вопроса другой длины: модель сменилась, индекс пересчитывается
        scores = np.empty(len(seqs), dtype=np.float32)
        for start in range(0, len(seqs), BLOCK):
            scores[start : start + BLOCK] = matrix[start : start + BLOCK] @ wanted
        k = min(k, len(seqs))
        best = np.argpartition(-scores, k - 1)[:k]
        best = best[np.argsort(-scores[best])]
        return [(int(seqs[i]), float(scores[i])) for i in best]

    def _refresh(self, conn: sqlite3.Connection, model: str) -> None:
        count, last = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(chunk_seq), 0) FROM chunk_vectors WHERE model = ?",
            (model,),
        ).fetchone()
        key = (model, count, last)
        with self._lock:
            if key == self._key:
                return
            old = self._key
            if old is not None and old[0] == model and count > old[1] and self._matrix is not None:
                rows = conn.execute(_SELECT + " AND chunk_seq > ? ORDER BY chunk_seq",
                                    (model, old[2])).fetchall()  # fmt: skip
                seqs, matrix = _load(rows)
                if len(rows) == count - old[1] and matrix.shape[1] == self._matrix.shape[1]:
                    self._seqs = np.concatenate([self._seqs, seqs])
                    self._matrix = np.vstack([self._matrix, matrix])
                    self._key = key
                    return
            # Первая загрузка, удаления или смена модели — собираем заново.
            self._seqs, self._matrix = _load(
                conn.execute(_SELECT + " ORDER BY chunk_seq", (model,)).fetchall()
            )
            self._key = key


_SELECT = "SELECT chunk_seq, dim, vector FROM chunk_vectors WHERE model = ?"


def _load(rows: list[sqlite3.Row]) -> tuple[Any, Any]:
    if not rows:
        return np.empty(0, dtype=np.int64), np.empty((0, vectors.DIM), dtype=np.float32)
    dims = [row["dim"] for row in rows]
    dim = max(set(dims), key=dims.count)
    kept = [row for row in rows if row["dim"] == dim]  # векторы одной длины
    seqs = np.fromiter((row["chunk_seq"] for row in kept), dtype=np.int64, count=len(kept))
    matrix = np.frombuffer(b"".join(row["vector"] for row in kept), dtype=np.float32)
    return seqs, matrix.reshape(len(kept), dim).copy()
