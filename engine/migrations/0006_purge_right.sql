-- LOCALAI, миграция 0006: право «удалять из архива окончательно» (F-009).
-- Окончательное удаление необратимо, поэтому это отдельное действие над
-- реестром, а не часть права «архивировать». CHECK таблицы не меняется
-- без её пересоздания: строки переносятся в новую таблицу как есть.

CREATE TABLE role_permissions_new (
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    scope   TEXT NOT NULL CHECK (scope IN ('section', 'dataset', 'field', 'doc_type')),
    target  TEXT NOT NULL,
    action  TEXT NOT NULL,
    PRIMARY KEY (role_id, scope, target, action),
    CHECK (
        (scope = 'section' AND action = 'use')
        OR (scope = 'dataset' AND action IN
            ('view', 'create', 'edit', 'archive', 'purge', 'import', 'export'))
        OR (scope = 'field' AND action IN ('hidden', 'readonly') AND target <> '*')
        OR (scope = 'doc_type' AND action = 'view')
    )
) STRICT, WITHOUT ROWID;

INSERT INTO role_permissions_new (role_id, scope, target, action)
SELECT role_id, scope, target, action FROM role_permissions;

DROP TABLE role_permissions;
ALTER TABLE role_permissions_new RENAME TO role_permissions;

-- Право получают роли с разделом «Администрирование» — на все реестры.
INSERT INTO role_permissions (role_id, scope, target, action)
SELECT role_id, 'dataset', '*', 'purge' FROM role_permissions
WHERE scope = 'section' AND target = 'admin';
