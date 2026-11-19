-- LOCALAI, миграция 0011: заметки знаний и словарь станции (ТЗ 8, KNOW-06…10, SEC-06).
--
-- Заметка — подтверждённое человеком знание, на которое опирается ИИ. У неё
-- есть состояние (предложена, подтверждена, устарела, отклонена), автор,
-- подтвердивший и источники с версиями. Изменение записи или документа-
-- источника делает заметку устаревшей: это делают триггеры ниже, поэтому
-- ни правка, ни импорт, ни пакетная правка, ни удаление не пройдут мимо.

CREATE TABLE knowledge_notes (
    seq           INTEGER PRIMARY KEY,
    id            TEXT NOT NULL UNIQUE,
    -- На что заметка: объект ККС, тип записей или вся станция.
    scope_kind    TEXT NOT NULL CHECK (scope_kind IN ('kks', 'type', 'station')),
    scope_id      TEXT,
    title         TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    body          TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 8000),
    state         TEXT NOT NULL CHECK (state IN ('proposed', 'confirmed', 'stale', 'rejected')),
    origin        TEXT NOT NULL CHECK (origin IN ('user', 'ai')),
    version       INTEGER NOT NULL DEFAULT 1,
    author_id     TEXT REFERENCES users(id),
    confirmed_by  TEXT REFERENCES users(id),
    confirmed_at  TEXT,
    stale_reason  TEXT,
    reject_reason TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    CHECK ((scope_kind = 'station') = (scope_id IS NULL))
) STRICT;

CREATE INDEX ix_notes_state ON knowledge_notes (state, updated_at);
CREATE INDEX ix_notes_scope ON knowledge_notes (scope_kind, scope_id, state);

-- Источники заметки с версией на момент подтверждения (KNOW-07).
CREATE TABLE note_sources (
    note_seq       INTEGER NOT NULL REFERENCES knowledge_notes(seq) ON DELETE CASCADE,
    source_kind    TEXT NOT NULL CHECK (source_kind IN ('record', 'document', 'kks')),
    source_id      TEXT NOT NULL,
    source_version INTEGER,
    page           TEXT,
    PRIMARY KEY (note_seq, source_kind, source_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX ix_note_sources_target ON note_sources (source_kind, source_id);

CREATE VIRTUAL TABLE note_fts USING fts5 (
    text,
    content = '',
    contentless_delete = 1,
    tokenize = 'trigram'
);

-- Словарь станции (KNOW-10): местное название → смысл и объекты ККС.
CREATE TABLE glossary_terms (
    id          TEXT PRIMARY KEY,
    term        TEXT NOT NULL CHECK (length(term) BETWEEN 1 AND 100),
    term_norm   TEXT NOT NULL UNIQUE,
    meaning     TEXT NOT NULL CHECK (length(meaning) BETWEEN 1 AND 1000),
    kks_ids     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(kks_ids)),
    created_by  TEXT REFERENCES users(id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

-- Устаревание (KNOW-08). Журнал получает строку note.stale с причиной:
-- её видно в событиях и в очереди проверки.

CREATE TRIGGER notes_stale_on_record AFTER UPDATE OF version, archived_at ON records
WHEN NEW.version <> OLD.version OR NEW.archived_at IS NOT OLD.archived_at
BEGIN
    INSERT INTO audit_log (at, actor_label, origin, action, entity_kind, entity_id, details)
    SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'Система', 'system', 'note.stale',
           'knowledge_note', n.id, json_object('source', 'record', 'source_id', NEW.id)
    FROM knowledge_notes n JOIN note_sources s ON s.note_seq = n.seq
    WHERE s.source_kind = 'record' AND s.source_id = NEW.id
      AND n.state IN ('proposed', 'confirmed');
    UPDATE knowledge_notes
    SET state = 'stale',
        stale_reason = CASE WHEN NEW.archived_at IS NOT NULL AND OLD.archived_at IS NULL
                            THEN 'Запись-источник отправлена в архив'
                            ELSE 'Запись-источник изменена' END,
        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE state IN ('proposed', 'confirmed')
      AND seq IN (SELECT note_seq FROM note_sources
                  WHERE source_kind = 'record' AND source_id = NEW.id);
END;

CREATE TRIGGER notes_stale_on_record_delete AFTER DELETE ON records
BEGIN
    INSERT INTO audit_log (at, actor_label, origin, action, entity_kind, entity_id, details)
    SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'Система', 'system', 'note.stale',
           'knowledge_note', n.id, json_object('source', 'record', 'source_id', OLD.id)
    FROM knowledge_notes n JOIN note_sources s ON s.note_seq = n.seq
    WHERE s.source_kind = 'record' AND s.source_id = OLD.id
      AND n.state IN ('proposed', 'confirmed');
    -- Причина уточняется и у уже устаревшей заметки: удаление важнее архива.
    UPDATE knowledge_notes
    SET state = 'stale', stale_reason = 'Запись-источник удалена',
        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE state IN ('proposed', 'confirmed', 'stale')
      AND seq IN (SELECT note_seq FROM note_sources
                  WHERE source_kind = 'record' AND source_id = OLD.id);
END;

CREATE TRIGGER notes_stale_on_document AFTER UPDATE OF version, archived_at ON documents
WHEN NEW.version <> OLD.version OR NEW.archived_at IS NOT OLD.archived_at
BEGIN
    INSERT INTO audit_log (at, actor_label, origin, action, entity_kind, entity_id, details)
    SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'Система', 'system', 'note.stale',
           'knowledge_note', n.id, json_object('source', 'document', 'source_id', NEW.id)
    FROM knowledge_notes n JOIN note_sources s ON s.note_seq = n.seq
    WHERE s.source_kind = 'document' AND s.source_id = NEW.id
      AND n.state IN ('proposed', 'confirmed');
    UPDATE knowledge_notes
    SET state = 'stale',
        stale_reason = CASE WHEN NEW.archived_at IS NOT NULL AND OLD.archived_at IS NULL
                            THEN 'Документ-источник отправлен в архив'
                            ELSE 'Документ-источник заменён новой версией' END,
        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE state IN ('proposed', 'confirmed')
      AND seq IN (SELECT note_seq FROM note_sources
                  WHERE source_kind = 'document' AND source_id = NEW.id);
END;

-- Право подтверждать заметки и вести словарь (KNOW-09) — администратору.
INSERT INTO role_permissions (role_id, scope, target, action)
SELECT role_id, 'section', 'knowledge', 'use' FROM role_permissions
WHERE scope = 'section' AND target = 'admin';
