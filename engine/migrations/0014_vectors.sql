-- LOCALAI, миграция 0014: векторы фрагментов документов (ТЗ 4.2, KNOW-02, AI-05).
--
-- Вектор — смысл фрагмента, посчитанный моделью векторов (по умолчанию
-- qwen3-embedding:0.6b в Ollama). Хранится в базе как массив float32,
-- нормированный к длине 1, вместе с именем модели: сменилась модель —
-- фрагменты пересчитываются, старые векторы не смешиваются с новыми.
-- Поиск по векторам идёт в памяти сервера (server/vector_index.py), без
-- расширения SQLite: проверять расширение на Windows не нужно.

CREATE TABLE chunk_vectors (
    chunk_seq INTEGER PRIMARY KEY REFERENCES chunks(seq) ON DELETE CASCADE,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL CHECK (dim BETWEEN 16 AND 4096),
    vector    BLOB NOT NULL CHECK (length(vector) = dim * 4)
) STRICT;

CREATE INDEX ix_chunk_vectors_model ON chunk_vectors (model);
