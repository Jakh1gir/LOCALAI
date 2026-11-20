-- LOCALAI, миграция 0012: память ИИ (ТЗ 9, AI-07, AI-09, KNOW-05, KNOW-12).
--
-- Диалог принадлежит пользователю: видит и удаляет его только он, срок
-- хранения задаёт администратор. Ответ ИИ хранится с источниками, по
-- которым он построен, и шагами (какие инструменты выполнялись).
-- Оценка ответа живёт отдельно от диалога: из оценок собирается набор
-- проверочных вопросов (AI-13), удаление диалога его не обедняет.
-- Черновик — единственный способ ИИ что-то изменить (AI-09): изменение
-- делает человек, принимая черновик, и в журнал пишется он.

CREATE TABLE ai_dialogs (
    seq        INTEGER PRIMARY KEY,
    id         TEXT NOT NULL UNIQUE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX ix_ai_dialogs_user ON ai_dialogs (user_id, updated_at);
CREATE INDEX ix_ai_dialogs_age ON ai_dialogs (updated_at);

CREATE TABLE ai_messages (
    seq        INTEGER PRIMARY KEY,
    id         TEXT NOT NULL UNIQUE,
    dialog_seq INTEGER NOT NULL REFERENCES ai_dialogs(seq) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text       TEXT NOT NULL,
    -- done — ответ дан; limit — прерван пределом шагов (AI-10); no_model —
    -- модели нет, ответ без неё (AI-12); no_service — служба ИИ недоступна;
    -- error — служба вернула ошибку.
    status     TEXT NOT NULL CHECK (status IN ('done', 'limit', 'no_model', 'no_service', 'error')),
    -- fast — точный вопрос без модели (AI-03); model — с моделью.
    path       TEXT CHECK (path IN ('fast', 'model')),
    -- Какая модель ответила: основная или запасная, имя в Ollama.
    model      TEXT,
    context    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(context)),
    steps      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(steps)),
    sources    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(sources)),
    took_ms    REAL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX ix_ai_messages_dialog ON ai_messages (dialog_seq, seq);

CREATE TABLE ai_feedback (
    message_id TEXT PRIMARY KEY,
    user_id    TEXT REFERENCES users(id) ON DELETE SET NULL,
    verdict    TEXT NOT NULL CHECK (verdict IN ('right', 'wrong')),
    correction TEXT CHECK (correction IS NULL OR length(correction) <= 4000),
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    sources    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(sources)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT, WITHOUT ROWID;

CREATE TABLE ai_drafts (
    seq            INTEGER PRIMARY KEY,
    id             TEXT NOT NULL UNIQUE,
    -- record_edit — правка значений записи; note — заметка знаний.
    kind           TEXT NOT NULL CHECK (kind IN ('record_edit', 'note')),
    state          TEXT NOT NULL CHECK (state IN ('proposed', 'accepted', 'rejected')),
    user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id     TEXT,
    target_id      TEXT,
    target_version INTEGER,
    summary        TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 500),
    payload        TEXT NOT NULL CHECK (json_valid(payload)),
    result_id      TEXT,
    reason         TEXT,
    decided_at     TEXT,
    created_at     TEXT NOT NULL
) STRICT;

CREATE INDEX ix_ai_drafts_user ON ai_drafts (user_id, state, seq);

-- Настройки ИИ (KNOW-12, AI-15, AI-01): ключ — значение JSON. Значения по
-- умолчанию — в engine/ai_memory.py; здесь строка появляется при изменении.
CREATE TABLE ai_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL CHECK (json_valid(value))
) STRICT, WITHOUT ROWID;
