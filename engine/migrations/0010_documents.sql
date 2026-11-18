-- LOCALAI, миграция 0010: документы, файлы, фрагменты и очередь задач
-- (ТЗ 4.1, 8, KNOW-01…04, DATA-09, VIS-01, VIS-02).
--
-- Файл хранится один раз по SHA-256 (DATA-09) в папке files рядом с базой;
-- документ ссылается на файл и меняет его при замене (KNOW-04). Текст
-- документа делится на фрагменты с адресом «документ, страница»; фрагменты
-- попадают в полнотекстовый индекс (KNOW-02). Векторный индекс добавится,
-- когда будет выбрано расширение SQLite (ТЗ 4.2).

CREATE TABLE files (
    sha256      TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    size        INTEGER NOT NULL CHECK (size > 0),
    media_type  TEXT NOT NULL,
    created_at  TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- Тип документа задаёт права «видеть» (ROLE-03, doc_type в матрице).
CREATE TABLE document_types (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL UNIQUE,
    archived_at TEXT,
    created_at  TEXT NOT NULL
) STRICT;

CREATE TABLE documents (
    seq          INTEGER PRIMARY KEY,
    id           TEXT NOT NULL UNIQUE,
    type_id      TEXT NOT NULL REFERENCES document_types(id),
    title        TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    file_name    TEXT NOT NULL,
    file_sha256  TEXT NOT NULL REFERENCES files(sha256),
    version      INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    -- Чтение (KNOW-01): queued — в очереди, reading — читается,
    -- ready — готово, empty — текста нет (скан без распознавания),
    -- error — ошибка после всех попыток.
    state        TEXT NOT NULL CHECK (state IN ('queued', 'reading', 'ready', 'empty', 'error')),
    error        TEXT,
    pages        INTEGER,
    reader       TEXT,
    created_by   TEXT REFERENCES users(id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    read_at      TEXT,
    archived_at  TEXT
) STRICT;

CREATE INDEX ix_documents_type ON documents (type_id, archived_at);
CREATE INDEX ix_documents_file ON documents (file_sha256);

-- Привязка документа к записи или объекту ККС.
CREATE TABLE document_links (
    document_seq INTEGER NOT NULL REFERENCES documents(seq),
    target_kind  TEXT NOT NULL CHECK (target_kind IN ('record', 'kks')),
    target_id    TEXT NOT NULL,
    created_by   TEXT REFERENCES users(id),
    created_at   TEXT NOT NULL,
    PRIMARY KEY (document_seq, target_kind, target_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX ix_document_links_target ON document_links (target_kind, target_id);

-- Коды ККС, найденные в тексте (KNOW-03): предложение связи до решения человека.
CREATE TABLE document_mentions (
    document_seq INTEGER NOT NULL REFERENCES documents(seq),
    kks_id       TEXT NOT NULL REFERENCES kks_objects(id),
    pages        TEXT NOT NULL CHECK (json_valid(pages)),
    hits         INTEGER NOT NULL CHECK (hits > 0),
    state        TEXT NOT NULL DEFAULT 'proposed'
        CHECK (state IN ('proposed', 'confirmed', 'rejected')),
    decided_by   TEXT REFERENCES users(id),
    decided_at   TEXT,
    PRIMARY KEY (document_seq, kks_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX ix_document_mentions_kks ON document_mentions (kks_id, state);

-- Фрагменты текста с адресом источника (KNOW-02).
CREATE TABLE chunks (
    seq          INTEGER PRIMARY KEY,
    document_seq INTEGER NOT NULL REFERENCES documents(seq),
    version      INTEGER NOT NULL,
    ordinal      INTEGER NOT NULL,
    page         TEXT NOT NULL,
    area         TEXT CHECK (area IS NULL OR json_valid(area)),
    text         TEXT NOT NULL,
    UNIQUE (document_seq, ordinal)
) STRICT;

CREATE VIRTUAL TABLE chunk_fts USING fts5 (
    text,
    content = '',
    contentless_delete = 1,
    tokenize = 'trigram'
);

-- Очередь фоновых задач (VIS-01, VIS-02). reader — кто выполняет:
-- server — сам сервер (простые форматы), vision — процесс схем.
CREATE TABLE jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    reader       TEXT NOT NULL CHECK (reader IN ('server', 'vision')),
    target_id    TEXT NOT NULL,
    target_version INTEGER NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done', 'error', 'cancelled')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    worker       TEXT,
    lease_until  TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
) STRICT;

CREATE INDEX ix_jobs_queue ON jobs (reader, state, created_at);
CREATE INDEX ix_jobs_target ON jobs (target_id, state);

INSERT INTO document_types (id, name, name_norm, created_at) VALUES
    ('00000000-0000-4000-8000-0000000d0c01', 'Документ', 'документ', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('00000000-0000-4000-8000-0000000d0c02', 'Схема', 'схема', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

-- Раздел «Документы»: загружать, заменять, привязывать — администратору и инженеру.
INSERT INTO role_permissions (role_id, scope, target, action)
SELECT id, 'section', 'documents', 'use' FROM roles WHERE name_norm IN ('администратор', 'инженер');
