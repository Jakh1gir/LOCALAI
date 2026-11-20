"""Поиск по смыслу: ближайшие векторы фрагментов (AI-05).

Два способа с одним интерфейсом (``search(conn, model, query, k)``):

- **дополнение SQLite sqlite-vec** (``ExtensionIndex``) — основной, решение
  владельца 20.11.2026. Индекс лежит в отдельном файле ``vectors.sqlite3``
  рядом с базой: это производные данные, они пересобираются из таблицы
  ``chunk_vectors`` основной базы. Основная база дополнения не требует —
  копия, перенос и открытие базы без дополнения работают как раньше.
- **в памяти сервера** (``MemoryIndex``, numpy) — если дополнение не
  загрузилось: точный поиск, около 35 мс на 100 000 фрагментов, 2 КБ памяти
  на фрагмент (замер 20.11.2026).

Выбор — ``choose``: переменная ``LOCALAI_VECTORS`` = ``auto`` (дополнение,
если загружается, иначе память), ``extension`` или ``memory``. Проверка на
Windows — ``python -m server.admin --db … check-vectors``.

Права здесь не проверяются: индекс возвращает номера фрагментов, а видимость
документа, архив и версию проверяет ``engine.documents.hybrid``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from array import array
from pathlib import Path
from typing import Any

from engine import vectors

try:
    import numpy as np
except ImportError:  # без numpy поиск по смыслу выключен, полнотекстовый работает
    np = None  # type: ignore[assignment]

log = logging.getLogger("localai.vectors")
BLOCK = 65536  # строк матрицы за одно умножение: память под промежуточный результат


class MemoryIndex:
    kind = "memory"
    fallback_error: str | None = None  # почему не дополнение, если выбрано вместо него

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[Any, ...] | None = None
        self._seqs: Any = None
        self._matrix: Any = None

    @property
    def available(self) -> bool:
        return np is not None

    def status(self) -> dict[str, Any]:
        rows = 0 if self._seqs is None else len(self._seqs)
        error = None if np else "numpy не установлен"
        return {"kind": self.kind, "rows": rows, "error": error, "extension": self.fallback_error}

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


# Дополнение sqlite-vec ---------------------------------------------------------------


def load_extension(conn: sqlite3.Connection) -> str:
    """Загрузить sqlite-vec в соединение; версия дополнения."""
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    return str(conn.execute("SELECT vec_version()").fetchone()[0])


class ExtensionIndex:
    """Индекс vec0 в отдельном файле; догоняет ``chunk_vectors`` перед каждым поиском."""

    kind = "extension"
    BATCH = 2000

    def __init__(self, path: Any, loader: Any = load_extension) -> None:
        self.path = str(path)
        self.error: str | None = None
        self.version: str | None = None
        self._lock = threading.Lock()
        self._key: tuple[Any, ...] | None = None
        self._side: sqlite3.Connection | None = None
        try:
            loader(sqlite3.connect(":memory:"))  # сначала проверка: файл индекса — только если есть
            side = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self.version = loader(side)
            side.execute("PRAGMA journal_mode = WAL")
            side.execute(
                "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id = 1),"
                " model TEXT, dim INTEGER, last INTEGER, count INTEGER)"
            )
            self._side = side
        except Exception as error:  # noqa: BLE001 — нет пакета, нет загрузки дополнений и т. п.
            self.error = f"{type(error).__name__}: {error}"

    @property
    def available(self) -> bool:
        return self._side is not None

    def status(self) -> dict[str, Any]:
        rows = 0
        if self._side is not None:
            with self._lock:
                found = self._side.execute("SELECT count FROM state WHERE id = 1").fetchone()
            rows = found[0] if found else 0
        return {"kind": self.kind, "rows": rows, "version": self.version, "error": self.error}

    def search(
        self, conn: sqlite3.Connection, model: str, query: list[float], k: int
    ) -> list[tuple[int, float]]:
        if self._side is None or not model:
            return []
        with self._lock:
            dim = self._sync(conn, model)
            if not dim:
                return []
            wanted = vectors.normalize(query)[:dim]
            if len(wanted) != dim:
                return []
            blob = array("f", wanted).tobytes()
            found = self._knn(self._side, blob, k)
        # Векторы длины 1: расстояние d и косинусное сходство связаны как 1 − d²/2.
        return [(int(seq), round(1 - distance * distance / 2, 6)) for seq, distance in found]

    # Догоняние основной базы ---------------------------------------------------------

    def _sync(self, conn: sqlite3.Connection, model: str) -> int | None:
        count, last = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(chunk_seq), 0) FROM chunk_vectors WHERE model = ?",
            (model,),
        ).fetchone()
        side = self._side
        assert side is not None
        state = side.execute("SELECT model, dim, last, count FROM state WHERE id = 1").fetchone()
        if self._key == (model, count, last) and state:
            return state[1]
        if count == 0:
            return None
        if state is None or state[0] != model:
            dim = conn.execute(
                "SELECT dim FROM chunk_vectors WHERE model = ? LIMIT 1", (model,)
            ).fetchone()[0]
            side.execute("BEGIN")
            side.execute("DROP TABLE IF EXISTS vec_chunks")
            self._create(side, dim)
            side.execute("INSERT OR REPLACE INTO state VALUES (1, ?, ?, 0, 0)", (model, dim))
            side.execute("COMMIT")
            state = (model, dim, 0, 0)
        dim = state[1]
        side.execute("BEGIN")
        try:
            self._copy(conn, side, model, dim, after=state[2])
            if self._count(side) != count:  # были удаления: сверяем номера целиком
                have = self._rowids(side)
                want = {row[0] for row in conn.execute(
                    "SELECT chunk_seq FROM chunk_vectors WHERE model = ? AND dim = ?",
                    (model, dim))}  # fmt: skip
                self._delete(side, sorted(have - want))
                self._copy(conn, side, model, dim, only=sorted(want - have))
            side.execute(
                "UPDATE state SET last = ?, count = ? WHERE id = 1", (last, self._count(side))
            )
            side.execute("COMMIT")
        except BaseException:
            side.execute("ROLLBACK")
            raise
        self._key = (model, count, last)
        return dim

    def _copy(
        self,
        conn: sqlite3.Connection,
        side: sqlite3.Connection,
        model: str,
        dim: int,
        *,
        after: int = 0,
        only: list[int] | None = None,
    ) -> None:
        if only is not None:
            for start in range(0, len(only), 500):
                part = only[start : start + 500]
                marks = ",".join("?" * len(part))
                self._insert(side, conn.execute(
                    f"SELECT chunk_seq, vector FROM chunk_vectors WHERE chunk_seq IN ({marks})",
                    part).fetchall())  # fmt: skip
            return
        cursor = conn.execute(
            "SELECT chunk_seq, vector FROM chunk_vectors WHERE model = ? AND dim = ?"
            " AND chunk_seq > ? ORDER BY chunk_seq",
            (model, dim, after),
        )
        while rows := cursor.fetchmany(self.BATCH):
            self._insert(side, rows)

    # Обращения к vec0: в тестах подменяются обычной таблицей -------------------------

    def _create(self, side: sqlite3.Connection, dim: int) -> None:
        side.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{int(dim)}])")

    def _insert(self, side: sqlite3.Connection, rows: list[Any]) -> None:
        side.executemany(
            "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
            [(row[0], row[1]) for row in rows],
        )

    def _delete(self, side: sqlite3.Connection, seqs: list[int]) -> None:
        side.executemany("DELETE FROM vec_chunks WHERE rowid = ?", [(seq,) for seq in seqs])

    def _knn(self, side: sqlite3.Connection, blob: bytes, k: int) -> list[tuple[int, float]]:
        return side.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance",
            (blob, k),
        ).fetchall()

    def _rowids(self, side: sqlite3.Connection) -> set[int]:
        return {row[0] for row in side.execute("SELECT rowid FROM vec_chunks")}

    def _count(self, side: sqlite3.Connection) -> int:
        return side.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]


def choose(folder: Any, mode: str = "auto") -> MemoryIndex | ExtensionIndex:
    """Способ поиска по смыслу: дополнение, если загружается (или требуется), иначе память."""
    if mode not in ("auto", "extension", "memory"):
        raise ValueError("LOCALAI_VECTORS: auto, extension или memory")
    if mode == "memory":
        return MemoryIndex()
    found = ExtensionIndex(Path(folder) / SIDE_FILE)
    if found.available:
        return found
    log.warning(
        "Дополнение sqlite-vec не загрузилось (%s): поиск по смыслу — в памяти", found.error
    )
    fallback = MemoryIndex()
    fallback.fallback_error = found.error
    return fallback


SIDE_FILE = "vectors.sqlite3"
VectorIndex = MemoryIndex  # прежнее имя


# Проверка на месте (Windows) -----------------------------------------------------------


def check(count: int = 20000, dim: int = 512) -> dict[str, Any]:
    """Загружается ли дополнение на этом компьютере и как быстро ищет; то же — в памяти.

    Векторы случайные; проверяется и правильность: вектор строки 123 находит
    строку 123 с сходством 1.
    """
    import platform
    import random
    import time

    report: dict[str, Any] = {
        "python": platform.python_version(),
        "system": f"{platform.system()} {platform.release()} {platform.machine()}",
        "sqlite": sqlite3.sqlite_version,
        "load_extension": hasattr(sqlite3.Connection, "enable_load_extension"),
        "numpy": None if np is None else np.__version__,
        "count": count,
    }
    rng = random.Random(7)
    rows = []
    for seq in range(1, count + 1):
        raw = [rng.gauss(0, 1) for _ in range(dim)]
        rows.append((seq, array("f", vectors.normalize(raw)[:dim]).tobytes()))
    wanted = list(array("f", rows[122][1]))
    try:
        conn = sqlite3.connect(":memory:")
        report["extension"] = load_extension(conn)
        started = time.perf_counter()
        conn.execute(f"CREATE VIRTUAL TABLE v USING vec0(embedding float[{dim}])")
        conn.executemany("INSERT INTO v (rowid, embedding) VALUES (?, ?)", rows)
        report["extension_insert_s"] = round(time.perf_counter() - started, 2)
        started = time.perf_counter()
        for _ in range(10):
            found = conn.execute(
                "SELECT rowid, distance FROM v WHERE embedding MATCH ? AND k = 10"
                " ORDER BY distance",
                (array("f", wanted).tobytes(),),
            ).fetchall()
        report["extension_search_ms"] = round((time.perf_counter() - started) * 100, 1)
        report["extension_correct"] = found[0][0] == 123 and abs(found[0][1]) < 1e-3
    except Exception as error:  # noqa: BLE001
        report["extension"] = None
        report["extension_error"] = f"{type(error).__name__}: {error}"
    if np is not None:
        memory = sqlite3.connect(":memory:")
        memory.row_factory = sqlite3.Row
        memory.execute(
            "CREATE TABLE chunk_vectors (chunk_seq INTEGER PRIMARY KEY, model TEXT, dim INT,"
            " vector BLOB)"
        )
        memory.executemany(
            "INSERT INTO chunk_vectors VALUES (?, 'm', ?, ?)", [(s, dim, b) for s, b in rows]
        )
        index = MemoryIndex()
        index.search(memory, "m", wanted, 10)
        started = time.perf_counter()
        for _ in range(10):
            found = index.search(memory, "m", wanted, 10)
        report["memory_search_ms"] = round((time.perf_counter() - started) * 100, 1)
        report["memory_correct"] = found[0][0] == 123 and found[0][1] > 0.999
    return report
