-- LOCALAI, миграция 0015: граф листа схемы (ТЗ 10, VIS-04…06, VIS-09).
--
-- Процесс схем (vision) разбирает лист векторного PDF или DXF на элементы
-- (надпись, символ, клемма, линия связи) с рамкой в координатах листа и на
-- соединения между ними и отдаёт результат вместе с текстом страницы.
-- Код ККС в надписи элемента сопоставляется с каталогом: сопоставление
-- имеет уверенность и становится привязкой документа только после
-- подтверждения человеком (VIS-05). Лист хранится на версию документа:
-- новая версия — новый граф.

CREATE TABLE sheets (
    seq          INTEGER PRIMARY KEY,
    document_seq INTEGER NOT NULL REFERENCES documents(seq) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    page         TEXT NOT NULL,
    width        REAL NOT NULL CHECK (width > 0),
    height       REAL NOT NULL CHECK (height > 0),
    -- Разборщик и его версия (VIS-10); source — vector или scan: по скану
    -- соединения — подсказка, а не факт.
    parser       TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('vector', 'scan')),
    created_at   TEXT NOT NULL,
    UNIQUE (document_seq, version, page)
) STRICT;

CREATE TABLE sheet_elements (
    sheet_seq  INTEGER NOT NULL REFERENCES sheets(seq) ON DELETE CASCADE,
    eid        TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('label', 'symbol', 'terminal', 'wire', 'other')),
    label      TEXT,
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    kks_id     TEXT REFERENCES kks_objects(id) ON DELETE SET NULL,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    -- Сопоставление с ККС: proposed — найдено, confirmed / rejected — решил человек.
    kks_state  TEXT CHECK (kks_state IN ('proposed', 'confirmed', 'rejected')),
    PRIMARY KEY (sheet_seq, eid),
    CHECK ((kks_id IS NULL) = (kks_state IS NULL))
) STRICT, WITHOUT ROWID;

CREATE INDEX ix_sheet_elements_kks ON sheet_elements (kks_id) WHERE kks_id IS NOT NULL;

CREATE TABLE sheet_edges (
    sheet_seq INTEGER NOT NULL REFERENCES sheets(seq) ON DELETE CASCADE,
    a         TEXT NOT NULL,
    b         TEXT NOT NULL,
    kind      TEXT,
    -- Точки линии соединения в координатах листа — для подсветки пути.
    points    TEXT CHECK (points IS NULL OR json_valid(points)),
    PRIMARY KEY (sheet_seq, a, b)
) STRICT, WITHOUT ROWID;
