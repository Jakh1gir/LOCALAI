-- LOCALAI, миграция 0007: один ККС — много строк (DATA-12…16).
--
-- • уникальное поле: значения хранятся в record_unique, повтор не даёт
--   первичный ключ;
-- • поле «ссылка на запись» (вид record): связи в record_links, удалить
--   запись, на которую ссылаются, база не даст;
-- • контакт: текстовое поле с contact_for подписывает связь поля-ссылки
--   или поля ККС того же типа (клемма, жила, вход блока);
-- • правила кода ККС станции: признак сигнала в конце кода.
--
-- Новый вид поля меняет CHECK таблицы fields, поэтому она пересоздаётся.
-- На fields никто не ссылается внешним ключом, строки переносятся как есть.

CREATE TABLE fields_new (
    id             TEXT PRIMARY KEY,
    type_id        TEXT NOT NULL REFERENCES types(id),
    key            TEXT NOT NULL,
    name           TEXT NOT NULL,
    name_norm      TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN
                       ('text', 'integer', 'number', 'boolean', 'date', 'choice', 'kks',
                        'record')),
    multiple       INTEGER NOT NULL DEFAULT 0 CHECK (multiple IN (0, 1)),
    required       INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    searchable     INTEGER NOT NULL DEFAULT 1 CHECK (searchable IN (0, 1)),
    filterable     INTEGER NOT NULL DEFAULT 0 CHECK (filterable IN (0, 1)),
    sortable       INTEGER NOT NULL DEFAULT 0 CHECK (sortable IN (0, 1)),
    unique_values  INTEGER NOT NULL DEFAULT 0 CHECK (unique_values IN (0, 1)),
    dictionary_id  TEXT REFERENCES dictionaries(id),
    input_mode     TEXT CHECK (input_mode IN ('strict', 'suggest')),
    kks_role       TEXT CHECK (kks_role IN
                       ('object', 'signal', 'route_start', 'route_end', 'route_via', 'related')),
    target_type_id TEXT REFERENCES types(id),
    contact_for    TEXT,
    position       INTEGER NOT NULL,
    archived_at    TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (type_id, key),
    CHECK ((kind = 'choice') = (dictionary_id IS NOT NULL)),
    CHECK ((kind = 'choice') = (input_mode IS NOT NULL)),
    CHECK ((kind = 'kks') = (kks_role IS NOT NULL)),
    CHECK ((kind = 'record') = (target_type_id IS NOT NULL)),
    CHECK (NOT (multiple = 1 AND sortable = 1)),
    CHECK (NOT (unique_values = 1 AND (multiple = 1
        OR kind NOT IN ('text', 'integer', 'number', 'date', 'kks')))),
    CHECK (contact_for IS NULL OR kind = 'text')
) STRICT;

INSERT INTO fields_new (id, type_id, key, name, name_norm, kind, multiple, required,
    searchable, filterable, sortable, dictionary_id, input_mode, kks_role, position,
    archived_at, created_at, updated_at)
SELECT id, type_id, key, name, name_norm, kind, multiple, required,
    searchable, filterable, sortable, dictionary_id, input_mode, kks_role, position,
    archived_at, created_at, updated_at
FROM fields;

DROP TABLE fields;
ALTER TABLE fields_new RENAME TO fields;
CREATE UNIQUE INDEX ux_fields_name ON fields (type_id, name_norm) WHERE archived_at IS NULL;

-- Значения уникальных полей действующих записей. value — нормализованный
-- вид: текст без учёта регистра и «ё», число и дата как есть, ККС — UUID.
CREATE TABLE record_unique (
    dataset_seq INTEGER NOT NULL,
    field_key   TEXT NOT NULL,
    value       TEXT NOT NULL,
    record_seq  INTEGER NOT NULL REFERENCES records(seq) ON DELETE CASCADE,
    PRIMARY KEY (dataset_seq, field_key, value)
) STRICT, WITHOUT ROWID;

CREATE INDEX ix_unique_record ON record_unique (record_seq);

-- Ссылки записей на записи. target_seq без каскада: запись, на которую
-- ссылаются, нельзя удалить окончательно, пока ссылка жива.
CREATE TABLE record_links (
    record_seq INTEGER NOT NULL REFERENCES records(seq) ON DELETE CASCADE,
    field_key  TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    target_seq INTEGER NOT NULL REFERENCES records(seq),
    PRIMARY KEY (record_seq, field_key, ordinal)
) STRICT, WITHOUT ROWID;

CREATE INDEX ix_links_target ON record_links (target_seq, record_seq);

-- Правила кода ККС станции (DATA-16). scope '*' — для всех площадок.
-- signal_pattern — регулярное выражение признака сигнала в конце кода.
CREATE TABLE kks_rules (
    scope          TEXT PRIMARY KEY,
    signal_pattern TEXT NOT NULL,
    updated_at     TEXT NOT NULL
) STRICT;

INSERT INTO kks_rules (scope, signal_pattern, updated_at)
VALUES ('*', 'X[A-Z][0-9]{2}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
