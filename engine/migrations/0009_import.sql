-- LOCALAI, миграция 0009: мастер импорта Excel (ТЗ 6.1, IMP-01…17).
--
-- Пакет — один проход мастера: загруженная книга, выбранные лист и
-- назначение, сопоставление столбцов и план. План хранится на сервере
-- (IMP-03): клиент не присылает строки на применение. Сама книга лежит
-- файлом в папке imports рядом с базой и удаляется по сроку пакета.

CREATE TABLE import_batches (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    file_name    TEXT NOT NULL,
    file_sha256  TEXT NOT NULL,
    file_size    INTEGER NOT NULL CHECK (file_size > 0),
    status       TEXT NOT NULL CHECK (status IN ('uploaded', 'planned', 'applied')),
    -- Лист, строка заголовков, режим, назначение, сопоставление, ключ, раскладка.
    settings     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(settings)),
    -- Листы книги для первого шага и сводка плана.
    preview      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(preview)),
    summary      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(summary)),
    -- Состояние базы при планировании (IMP-04): номер журнала и ревизии схем.
    plan_cursor  INTEGER,
    plan_schema  TEXT CHECK (plan_schema IS NULL OR json_valid(plan_schema)),
    report       TEXT CHECK (report IS NULL OR json_valid(report)),
    created_at   TEXT NOT NULL,
    planned_at   TEXT,
    applied_at   TEXT,
    expires_at   TEXT NOT NULL
) STRICT;

CREATE INDEX ix_import_batches_user ON import_batches (user_id, created_at);
CREATE INDEX ix_import_batches_expires ON import_batches (expires_at);

-- Строки плана. cells — значения строки листа по заголовкам, как в книге:
-- применение берёт их отсюда, а не из запроса.
CREATE TABLE import_rows (
    batch_id     TEXT NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    row_no       INTEGER NOT NULL,
    action       TEXT NOT NULL
        CHECK (action IN ('create', 'update', 'unchanged', 'conflict', 'error')),
    record_id    TEXT,
    base_version INTEGER,
    cells        TEXT NOT NULL CHECK (json_valid(cells)),
    changes      TEXT CHECK (changes IS NULL OR json_valid(changes)),
    -- Код причины спорной или ошибочной строки и текст для человека.
    reason       TEXT,
    message      TEXT,
    decision     TEXT CHECK (decision IS NULL OR decision IN ('skip', 'apply')),
    PRIMARY KEY (batch_id, row_no)
) STRICT, WITHOUT ROWID;

CREATE INDEX ix_import_rows_action ON import_rows (batch_id, action, row_no);

-- Профиль (IMP-08): правила разбора книги одного вида. signature —
-- свёрнутые заголовки листа: по ней профиль предлагается следующей книге.
CREATE TABLE import_profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL UNIQUE,
    signature   TEXT NOT NULL,
    settings    TEXT NOT NULL CHECK (json_valid(settings)),
    created_by  TEXT REFERENCES users(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

CREATE INDEX ix_import_profiles_signature ON import_profiles (signature);
