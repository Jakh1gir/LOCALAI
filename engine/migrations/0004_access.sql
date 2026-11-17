-- LOCALAI, миграция 0004: доступ (этап 2). Роли как данные, матрица прав,
-- личные учётные записи, сессии. Описание таблиц: docs/contract/database.md
--
-- Пользователей миграция не создаёт: стартовых паролей в программе нет (SEC-03),
-- первого администратора заводит установщик командой `python -m server.admin`.

CREATE TABLE roles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL UNIQUE,
    description TEXT,
    archived_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

-- Строка матрицы прав. target — UUID реестра, поля или типа документа, имя
-- раздела или «*» (все реестры, все типы документов). Права на поле — это
-- ограничения: без строки поле подчиняется правам на реестр.
CREATE TABLE role_permissions (
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    scope   TEXT NOT NULL CHECK (scope IN ('section', 'dataset', 'field', 'doc_type')),
    target  TEXT NOT NULL,
    action  TEXT NOT NULL,
    PRIMARY KEY (role_id, scope, target, action),
    CHECK (
        (scope = 'section' AND action = 'use')
        OR (scope = 'dataset' AND action IN
            ('view', 'create', 'edit', 'archive', 'import', 'export'))
        OR (scope = 'field' AND action IN ('hidden', 'readonly') AND target <> '*')
        OR (scope = 'doc_type' AND action = 'view')
    )
) STRICT, WITHOUT ROWID;

CREATE TABLE users (
    id                  TEXT PRIMARY KEY,
    login               TEXT NOT NULL,
    login_norm          TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    role_id             TEXT NOT NULL REFERENCES roles(id),
    password_hash       TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 1 CHECK (must_change_password IN (0, 1)),
    password_changed_at TEXT NOT NULL,
    failed_logins       INTEGER NOT NULL DEFAULT 0,
    locked_until        TEXT,
    blocked_at          TEXT,
    last_login_at       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
) STRICT;

CREATE INDEX ix_users_role ON users (role_id);

-- Токен сессии хранится только как SHA-256 (SEC-01): копия базы не даёт войти.
CREATE TABLE sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    client       TEXT
) STRICT, WITHOUT ROWID;

CREATE INDEX ix_sessions_user ON sessions (user_id);

-- Стартовые роли (ROLE-10). Их можно переименовать, изменить и отключить,
-- кроме случая, когда это оставит базу без администратора (ROLE-08).

-- name_norm задан явно: lower() в SQLite не знает кириллицы.
CREATE TEMP TABLE start_roles (name TEXT, name_norm TEXT, description TEXT);
INSERT INTO start_roles VALUES
    ('Администратор', 'администратор', 'Пользователи, роли, структура и все реестры'),
    ('Инженер', 'инженер', 'Ведёт записи и каталог ККС во всех реестрах'),
    ('Наблюдатель', 'наблюдатель', 'Просматривает и выгружает записи');

INSERT INTO roles (id, name, name_norm, description, created_at, updated_at)
SELECT
    lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4'
        || substr(lower(hex(randomblob(2))), 2) || '-'
        || substr('89ab', 1 + abs(random()) % 4, 1) || substr(lower(hex(randomblob(2))), 2)
        || '-' || lower(hex(randomblob(6))),
    name,
    name_norm,
    description,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM start_roles;

CREATE TEMP TABLE start_rights (role TEXT, scope TEXT, target TEXT, action TEXT);
INSERT INTO start_rights VALUES
    ('администратор', 'section', 'admin', 'use'),
    ('администратор', 'section', 'structure', 'use'),
    ('администратор', 'section', 'kks', 'use'),
    ('администратор', 'section', 'ai', 'use'),
    ('администратор', 'section', 'tools', 'use'),
    ('администратор', 'section', 'timesheet', 'use'),
    ('администратор', 'section', 'backups', 'use'),
    ('администратор', 'dataset', '*', 'view'),
    ('администратор', 'dataset', '*', 'create'),
    ('администратор', 'dataset', '*', 'edit'),
    ('администратор', 'dataset', '*', 'archive'),
    ('администратор', 'dataset', '*', 'import'),
    ('администратор', 'dataset', '*', 'export'),
    ('администратор', 'doc_type', '*', 'view'),
    ('инженер', 'section', 'kks', 'use'),
    ('инженер', 'section', 'ai', 'use'),
    ('инженер', 'section', 'tools', 'use'),
    ('инженер', 'section', 'timesheet', 'use'),
    ('инженер', 'dataset', '*', 'view'),
    ('инженер', 'dataset', '*', 'create'),
    ('инженер', 'dataset', '*', 'edit'),
    ('инженер', 'dataset', '*', 'archive'),
    ('инженер', 'dataset', '*', 'import'),
    ('инженер', 'dataset', '*', 'export'),
    ('инженер', 'doc_type', '*', 'view'),
    ('наблюдатель', 'section', 'ai', 'use'),
    ('наблюдатель', 'section', 'tools', 'use'),
    ('наблюдатель', 'dataset', '*', 'view'),
    ('наблюдатель', 'dataset', '*', 'export'),
    ('наблюдатель', 'doc_type', '*', 'view');

INSERT INTO role_permissions (role_id, scope, target, action)
SELECT roles.id, start_rights.scope, start_rights.target, start_rights.action
FROM start_rights JOIN roles ON roles.name_norm = start_rights.role;

DROP TABLE start_roles;
DROP TABLE start_rights;
