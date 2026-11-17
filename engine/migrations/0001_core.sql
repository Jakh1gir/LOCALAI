-- LOCALAI, миграция 0001: ядро данных (этап 1).
-- Структура, записи с историей, справочники, каталог ККС, полнотекстовый
-- индекс, аудит, ключи идемпотентности. Описание таблиц: docs/contract/database.md

CREATE TABLE app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- Структура ---------------------------------------------------------------

CREATE TABLE sites (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL UNIQUE,
    archived_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

CREATE TABLE dictionaries (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL UNIQUE,
    archived_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

CREATE TABLE dictionary_values (
    id            TEXT PRIMARY KEY,
    dictionary_id TEXT NOT NULL REFERENCES dictionaries(id),
    value         TEXT NOT NULL,
    value_norm    TEXT NOT NULL,
    synonyms      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(synonyms)),
    position      INTEGER NOT NULL,
    archived_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (dictionary_id, value_norm)
) STRICT;

CREATE TABLE types (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    name_norm     TEXT NOT NULL UNIQUE,
    description   TEXT,
    schema_rev    INTEGER NOT NULL DEFAULT 1,
    next_field_no INTEGER NOT NULL DEFAULT 1,
    archived_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
) STRICT;

CREATE TABLE fields (
    id            TEXT PRIMARY KEY,
    type_id       TEXT NOT NULL REFERENCES types(id),
    key           TEXT NOT NULL,
    name          TEXT NOT NULL,
    name_norm     TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN
                      ('text', 'integer', 'number', 'boolean', 'date', 'choice', 'kks')),
    multiple      INTEGER NOT NULL DEFAULT 0 CHECK (multiple IN (0, 1)),
    required      INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    searchable    INTEGER NOT NULL DEFAULT 1 CHECK (searchable IN (0, 1)),
    filterable    INTEGER NOT NULL DEFAULT 0 CHECK (filterable IN (0, 1)),
    sortable      INTEGER NOT NULL DEFAULT 0 CHECK (sortable IN (0, 1)),
    dictionary_id TEXT REFERENCES dictionaries(id),
    input_mode    TEXT CHECK (input_mode IN ('strict', 'suggest')),
    kks_role      TEXT CHECK (kks_role IN
                      ('object', 'signal', 'route_start', 'route_end', 'route_via', 'related')),
    position      INTEGER NOT NULL,
    archived_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (type_id, key),
    CHECK ((kind = 'choice') = (dictionary_id IS NOT NULL)),
    CHECK ((kind = 'choice') = (input_mode IS NOT NULL)),
    CHECK ((kind = 'kks') = (kks_role IS NOT NULL)),
    CHECK (NOT (multiple = 1 AND sortable = 1))
) STRICT;

CREATE UNIQUE INDEX ux_fields_name ON fields (type_id, name_norm) WHERE archived_at IS NULL;

-- seq — короткий внутренний номер реестра: он стоит в каждой записи и в каждом
-- индексе по полю, поэтому UUID здесь стоил бы сотни байт на запись.
CREATE TABLE datasets (
    seq         INTEGER PRIMARY KEY,
    id          TEXT NOT NULL UNIQUE,
    site_id     TEXT NOT NULL REFERENCES sites(id),
    type_id     TEXT NOT NULL REFERENCES types(id),
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL,
    archived_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (site_id, name_norm)
) STRICT;

CREATE INDEX ix_datasets_type ON datasets (type_id);

-- Записи ------------------------------------------------------------------

CREATE TABLE records (
    seq         INTEGER PRIMARY KEY,
    id          TEXT NOT NULL UNIQUE,
    dataset_seq INTEGER NOT NULL REFERENCES datasets(seq),
    version     INTEGER NOT NULL,
    doc         TEXT NOT NULL CHECK (json_valid(doc)),
    archived_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

CREATE INDEX ix_records_dataset ON records (dataset_seq);

-- История хранит разницу между версиями: {"ключ": [было, стало]}. У первой
-- версии разница пуста: её значения восстанавливаются обратным ходом от
-- текущего документа, поэтому создание записи не хранит документ дважды.
CREATE TABLE record_versions (
    record_seq    INTEGER NOT NULL REFERENCES records(seq) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    op            TEXT NOT NULL CHECK (op IN
                      ('create', 'update', 'archive', 'restore', 'rollback', 'import')),
    changes       TEXT NOT NULL CHECK (json_valid(changes)),
    actor_id      TEXT,
    actor_label   TEXT NOT NULL,
    origin        TEXT NOT NULL,
    at            TEXT NOT NULL,
    restored_from INTEGER,
    PRIMARY KEY (record_seq, version)
) STRICT;

-- Полнотекстовый индекс записей. Текст в индексе не хранится (contentless),
-- подстроки ищутся по триграммам; rowid = records.seq.
CREATE VIRTUAL TABLE record_fts USING fts5 (
    text,
    content = '',
    contentless_delete = 1,
    tokenize = 'trigram'
);

-- ККС ---------------------------------------------------------------------

CREATE TABLE kks_objects (
    id          TEXT PRIMARY KEY,
    site_id     TEXT NOT NULL REFERENCES sites(id),
    code        TEXT NOT NULL,
    code_norm   TEXT NOT NULL,
    name        TEXT,
    archived_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (site_id, code_norm)
) STRICT;

CREATE TABLE kks_aliases (
    id         TEXT PRIMARY KEY,
    kks_id     TEXT NOT NULL REFERENCES kks_objects(id) ON DELETE CASCADE,
    site_id    TEXT NOT NULL REFERENCES sites(id),
    alias      TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('former', 'synonym')),
    created_at TEXT NOT NULL,
    UNIQUE (site_id, alias_norm)
) STRICT;

CREATE INDEX ix_kks_aliases_object ON kks_aliases (kks_id);

-- Связь записи с объектом ККС. Поле задаётся его ключом в типе записи.
-- Позиция (ordinal) уникальна внутри поля записи; одна и та же связь
-- (объект, роль, позиция) не повторяется. Тот же индекс отвечает на вопрос
-- «какие записи связаны с объектом».
CREATE TABLE record_kks_links (
    record_seq INTEGER NOT NULL REFERENCES records(seq) ON DELETE CASCADE,
    field_key  TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    kks_id     TEXT NOT NULL REFERENCES kks_objects(id),
    role       TEXT NOT NULL,
    PRIMARY KEY (record_seq, field_key, ordinal)
) STRICT, WITHOUT ROWID;

CREATE UNIQUE INDEX ux_links_kks ON record_kks_links (kks_id, role, record_seq, ordinal);

-- Аудит и идемпотентность --------------------------------------------------

CREATE TABLE audit_log (
    seq         INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    actor_id    TEXT,
    actor_label TEXT NOT NULL,
    origin      TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id   TEXT,
    dataset_id  TEXT,
    details     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details))
) STRICT;

CREATE INDEX ix_audit_entity ON audit_log (entity_id, seq);
CREATE INDEX ix_audit_at ON audit_log (at);

CREATE TABLE idempotency_keys (
    scope        TEXT NOT NULL,
    key          TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (scope, key)
) STRICT, WITHOUT ROWID;
