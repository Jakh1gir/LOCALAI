-- LOCALAI, миграция 0008: общий чат пользователей (ТЗ 11.3, CHAT-01…09).
--
-- Один общий канал на базу. Сообщение хранит текущий текст; прежние тексты
-- лежат в chat_message_versions (CHAT-05). Удалённое автором и скрытое
-- модератором сообщение остаётся в базе: другие видят только отметку.

CREATE TABLE chat_messages (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT NOT NULL UNIQUE,
    author_id     TEXT NOT NULL REFERENCES users(id),
    body          TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
    version       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at    TEXT NOT NULL,
    edited_at     TEXT,
    deleted_at    TEXT,
    hidden_at     TEXT,
    hidden_by     TEXT REFERENCES users(id),
    hidden_reason TEXT CHECK (hidden_reason IS NULL OR length(hidden_reason) <= 500)
) STRICT;

-- Частота сообщений автора (CHAT-07) считается по этому индексу.
CREATE INDEX ix_chat_author ON chat_messages (author_id, created_at);

-- Прежние тексты: версия N — текст до правки, сделавшей версию N + 1.
CREATE TABLE chat_message_versions (
    message_seq INTEGER NOT NULL REFERENCES chat_messages(seq),
    version     INTEGER NOT NULL,
    body        TEXT NOT NULL,
    at          TEXT NOT NULL,
    PRIMARY KEY (message_seq, version)
) STRICT, WITHOUT ROWID;

-- До какого сообщения пользователь дочитал ленту: счётчик непрочитанного.
CREATE TABLE chat_reads (
    user_id  TEXT PRIMARY KEY REFERENCES users(id),
    last_seq INTEGER NOT NULL,
    read_at  TEXT NOT NULL
) STRICT, WITHOUT ROWID;

-- Поиск по сообщениям, как по записям: свёрнутый текст, триграммы,
-- текст в индексе не хранится; rowid = chat_messages.seq.
CREATE VIRTUAL TABLE chat_fts USING fts5 (
    text,
    content = '',
    contentless_delete = 1,
    tokenize = 'trigram'
);

-- Права (CHAT-06): читать и писать — всем ролям, что есть на момент
-- обновления, в том числе стартовым; модерировать — ролям с разделом
-- «Администрирование». Дальше права выдаёт администратор в матрице ролей.
INSERT INTO role_permissions (role_id, scope, target, action)
SELECT id, 'section', 'chat', 'use' FROM roles;

INSERT INTO role_permissions (role_id, scope, target, action)
SELECT id, 'section', 'chat_write', 'use' FROM roles;

INSERT INTO role_permissions (role_id, scope, target, action)
SELECT role_id, 'section', 'chat_moderate', 'use' FROM role_permissions
WHERE scope = 'section' AND target = 'admin';
