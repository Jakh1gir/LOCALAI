-- LOCALAI, миграция 0013: ещё два вида черновиков ИИ (ТЗ 9.1, propose).
--
-- document_link  — связь документа с объектом ККС или записью;
-- column_mapping — сопоставление столбцов листа Excel с полями реестра в
--                  мастере импорта: принятое сопоставление человек применяет
--                  в мастере, сам черновик ничего не импортирует.
-- CHECK не меняется без пересоздания таблицы: строки переносятся как есть.

CREATE TABLE ai_drafts_new (
    seq            INTEGER PRIMARY KEY,
    id             TEXT NOT NULL UNIQUE,
    kind           TEXT NOT NULL
                   CHECK (kind IN ('record_edit', 'note', 'document_link', 'column_mapping')),
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

INSERT INTO ai_drafts_new SELECT * FROM ai_drafts;
DROP TABLE ai_drafts;
ALTER TABLE ai_drafts_new RENAME TO ai_drafts;
CREATE INDEX ix_ai_drafts_user ON ai_drafts (user_id, state, seq);
