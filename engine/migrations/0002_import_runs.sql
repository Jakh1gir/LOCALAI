-- LOCALAI, миграция 0002: журнал переносов данных из других систем.
-- Один запуск применённого переноса — одна строка. Пробный прогон строк не
-- оставляет. По журналу повторный перенос той же базы знает, что уже
-- перенесено, и дозагружает только новое (MIG-11).

CREATE TABLE import_runs (
    id             TEXT PRIMARY KEY,
    source_kind    TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_schema  INTEGER,
    site_id        TEXT REFERENCES sites(id),
    actor_id       TEXT,
    actor_label    TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT NOT NULL,
    audit_cursor   INTEGER NOT NULL DEFAULT 0,
    report         TEXT NOT NULL CHECK (json_valid(report))
) STRICT;

CREATE INDEX ix_import_runs_source ON import_runs (source_kind, source_id, finished_at);
