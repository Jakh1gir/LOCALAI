"""Записи: создание, правка, архив, история и откат (DATA-04, DATA-05).

Каждое изменение проверяет ожидаемую версию, пишет разницу в историю,
обновляет связи ККС и индекс поиска и оставляет строку аудита — всё в
одной транзакции. Правка без фактических изменений версию не создаёт.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from engine import fulltext, structure, text
from engine.database import Tx, dumps
from engine.errors import CONFLICT, EngineError, not_found
from engine.validation import Normalizer

MAX_BULK = 5000


_SELECT = (
    "SELECT r.seq, r.id, d.id AS dataset_id, r.version, r.doc, r.archived_at,"
    " r.created_at, r.updated_at FROM records r JOIN datasets d ON d.seq = r.dataset_seq"
)


def get(conn: sqlite3.Connection, record_id: str) -> dict[str, Any]:
    return _record(_row(conn, record_id))


def create(
    tx: Tx, dataset_id: str, values: dict[str, Any], *, record_id: str | None = None
) -> dict[str, Any]:
    writer = _Writer(tx, dataset_id)
    seq = writer.insert(writer.normalizer.full(values), record_id, "create")
    created = get_by_seq(tx.conn, seq)
    tx.audit("record.create", "record", created["id"], dataset_id=dataset_id)
    return created


def create_many(
    tx: Tx,
    dataset_id: str,
    rows: list[dict[str, Any]],
    *,
    op: str = "create",
    create_missing_kks: bool = True,
) -> list[str]:
    """Создаёт записи одной транзакцией; сбой на любой строке отменяет все (IMP-05)."""
    if len(rows) > MAX_BULK:
        raise EngineError("bulk_too_large", f"За один раз создаётся до {MAX_BULK} записей")
    if op not in ("create", "import"):
        raise EngineError("bulk_op", f"Недопустимая операция пакетной записи: {op}")
    writer = _Writer(tx, dataset_id, create_missing_kks=create_missing_kks, audit_kks=False)
    ids: list[str] = []
    for number, values in enumerate(rows):
        try:
            doc = writer.normalizer.full(values)
        except EngineError as error:
            error.details = {**error.details, "row": number}
            raise
        record_id = text.new_id()
        writer.insert(doc, record_id, op)
        ids.append(record_id)
    tx.audit(f"record.{op}_many", "dataset", dataset_id, dataset_id=dataset_id,
             details={"count": len(ids), "kks_created": writer.normalizer.kks_created})  # fmt: skip
    return ids


def update(
    tx: Tx, record_id: str, patch: dict[str, Any], expected_version: int
) -> dict[str, Any]:
    row = _row_for_write(tx.conn, record_id, expected_version)
    writer = _Writer(tx, row["dataset_id"])
    old = json.loads(row["doc"])
    changes = writer.normalizer.partial(patch, current=old)
    diff = {key: [old.get(key), new] for key, new in changes.items() if old.get(key) != new}
    if not diff:
        return _record(row)
    writer.apply(row, old, diff, "update")
    tx.audit("record.update", "record", record_id, dataset_id=row["dataset_id"],
             details={"fields": sorted(diff)})  # fmt: skip
    return get(tx.conn, record_id)


def set_archived(
    tx: Tx, record_id: str, archived: bool, expected_version: int
) -> dict[str, Any]:
    """Архив и восстановление. Связи ККС и история при архиве сохраняются (DATA-08)."""
    row = _row_for_write(tx.conn, record_id, expected_version, allow_archived=True)
    if (row["archived_at"] is not None) == archived:
        return _record(row)
    op = "archive" if archived else "restore"
    version = row["version"] + 1
    tx.conn.execute(
        "UPDATE records SET archived_at = ?, version = ?, updated_at = ? WHERE seq = ?",
        (tx.now if archived else None, version, tx.now, row["seq"]),
    )
    _add_version(tx, row["seq"], version, op, {})
    tx.audit(f"record.{op}", "record", record_id, dataset_id=row["dataset_id"])
    return get(tx.conn, record_id)


def history(conn: sqlite3.Connection, record_id: str) -> list[dict[str, Any]]:
    """Версии записи от новой к старой с разницей каждой версии.

    У первой версии разница в базе не хранится; она восстанавливается здесь
    как «было пусто — стало значение».
    """
    row = _row(conn, record_id)
    rows = conn.execute(
        "SELECT version, op, changes, actor_id, actor_label, origin, at, restored_from"
        " FROM record_versions WHERE record_seq = ? ORDER BY version DESC",
        (row["seq"],),
    ).fetchall()
    log = [{**dict(item), "changes": json.loads(item["changes"])} for item in rows]
    if log and log[-1]["version"] == 1:
        first = doc_at(conn, record_id, 1)
        log[-1]["changes"] = {key: [None, value] for key, value in first.items()}
    return log


def doc_at(conn: sqlite3.Connection, record_id: str, version: int) -> dict[str, Any]:
    """Документ записи на указанной версии, восстановленный по разницам."""
    row = _row(conn, record_id)
    if not 1 <= version <= row["version"]:
        raise EngineError(
            "version_unknown", f"У записи нет версии {version}", field="version"
        )
    doc = json.loads(row["doc"])
    later = conn.execute(
        "SELECT changes FROM record_versions WHERE record_seq = ? AND version > ?"
        " ORDER BY version DESC",
        (row["seq"], version),
    ).fetchall()
    for item in later:
        for key, (before, _after) in json.loads(item["changes"]).items():
            if before is None:
                doc.pop(key, None)
            else:
                doc[key] = before
    return doc


def rollback(
    tx: Tx, record_id: str, to_version: int, expected_version: int
) -> dict[str, Any]:
    """Возвращает значения версии ``to_version`` новой версией записи."""
    row = _row_for_write(tx.conn, record_id, expected_version)
    target = doc_at(tx.conn, record_id, to_version)
    old = json.loads(row["doc"])
    keys = sorted(set(old) | set(target))
    diff = {key: [old.get(key), target.get(key)] for key in keys if old.get(key) != target.get(key)}
    if not diff:
        return _record(row)
    writer = _Writer(tx, row["dataset_id"])
    writer.check_kks_alive(diff)
    writer.apply(row, old, diff, "rollback", restored_from=to_version)
    tx.audit("record.rollback", "record", record_id, dataset_id=row["dataset_id"],
             details={"to_version": to_version})  # fmt: skip
    return get(tx.conn, record_id)


def get_by_seq(conn: sqlite3.Connection, seq: int) -> dict[str, Any]:
    return _record(conn.execute(f"{_SELECT} WHERE r.seq = ?", (seq,)).fetchone())


# Внутреннее -----------------------------------------------------------------


class _Writer:
    """Запись в один реестр: схема, приведение значений, связи и индекс поиска."""

    def __init__(
        self,
        tx: Tx,
        dataset_id: str,
        *,
        create_missing_kks: bool = True,
        audit_kks: bool = True,
    ) -> None:
        self.tx = tx
        self.dataset = structure.load_dataset(tx.conn, dataset_id)
        if self.dataset["archived_at"]:
            raise EngineError("dataset_archived", "Реестр в архиве; запись в него закрыта")
        self.schema = structure.load_schema(tx.conn, self.dataset["type_id"])
        self.normalizer = Normalizer(
            tx, self.schema, self.dataset["site_id"],
            create_missing_kks=create_missing_kks, audit_kks=audit_kks,
        )  # fmt: skip
        self.labels = fulltext.Labels(tx.conn)
        self.kks_fields = [field for field in self.schema.fields if field.kind == "kks"]

    def insert(self, doc: dict[str, Any], record_id: str | None, op: str) -> int:
        tx = self.tx
        record_id = record_id or text.new_id()
        if not text.is_uuid(record_id):
            raise EngineError("record_id", "ID записи должен быть UUID", field="id")
        try:
            cursor = tx.conn.execute(
                "INSERT INTO records (id, dataset_seq, version, doc, created_at, updated_at)"
                " VALUES (?, ?, 1, ?, ?, ?)",
                (record_id, self.dataset["seq"], dumps(doc), tx.now, tx.now),
            )
        except sqlite3.IntegrityError:
            raise EngineError(
                "record_exists", "Запись с таким ID уже есть", status=CONFLICT, field="id"
            ) from None
        seq = cursor.lastrowid
        assert seq is not None
        _add_version(tx, seq, 1, op, {})
        self._links(seq, doc)
        fulltext.write(tx.conn, seq, fulltext.record_text(doc, self.schema, self.labels))
        return seq

    def apply(
        self,
        row: sqlite3.Row,
        old: dict[str, Any],
        diff: dict[str, list[Any]],
        op: str,
        *,
        restored_from: int | None = None,
    ) -> None:
        tx = self.tx
        doc = dict(old)
        for key, (_before, after) in diff.items():
            if after is None:
                doc.pop(key, None)
            else:
                doc[key] = after
        version = row["version"] + 1
        tx.conn.execute(
            "UPDATE records SET doc = ?, version = ?, updated_at = ? WHERE seq = ?",
            (dumps(doc), version, tx.now, row["seq"]),
        )
        _add_version(tx, row["seq"], version, op, diff, restored_from)
        if any(field.key in diff for field in self.kks_fields):
            tx.conn.execute("DELETE FROM record_kks_links WHERE record_seq = ?", (row["seq"],))
            self._links(row["seq"], doc)
        fulltext.write(tx.conn, row["seq"], fulltext.record_text(doc, self.schema, self.labels))

    def check_kks_alive(self, diff: dict[str, list[Any]]) -> None:
        """Откат не может заново связать запись с архивным объектом ККС."""
        for field in self.kks_fields:
            if field.key not in diff:
                continue
            before, after = diff[field.key]
            had = set(before if isinstance(before, list) else [before])
            for kks_id in after if isinstance(after, list) else [after]:
                if kks_id is None or kks_id in had:
                    continue
                row = self.tx.conn.execute(
                    "SELECT code, archived_at FROM kks_objects WHERE id = ?", (kks_id,)
                ).fetchone()
                if row is None or row["archived_at"]:
                    code = row["code"] if row else kks_id
                    raise EngineError(
                        "kks_archived",
                        f"Объект ККС «{code}» в архиве; его нужно восстановить",
                        field=field.key,
                    )

    def _links(self, seq: int, doc: dict[str, Any]) -> None:
        rows = []
        for field in self.kks_fields:
            value = doc.get(field.key)
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            for ordinal, kks_id in enumerate(items):
                rows.append((seq, field.key, ordinal, kks_id, field.kks_role))
        if not rows:
            return
        try:
            self.tx.conn.executemany(
                "INSERT INTO record_kks_links (record_seq, field_key, ordinal, kks_id, role)"
                " VALUES (?,?,?,?,?)",
                rows,
            )
        except sqlite3.IntegrityError:
            raise EngineError(
                "kks_link_duplicate",
                "Одна и та же связь с объектом ККС указана в записи дважды",
            ) from None


def _add_version(
    tx: Tx,
    seq: int,
    version: int,
    op: str,
    changes: dict[str, Any],
    restored_from: int | None = None,
) -> None:
    tx.conn.execute(
        "INSERT INTO record_versions (record_seq, version, op, changes, actor_id,"
        " actor_label, origin, at, restored_from) VALUES (?,?,?,?,?,?,?,?,?)",
        (seq, version, op, dumps(changes), tx.actor.id, tx.actor.label, tx.actor.origin,
         tx.now, restored_from),
    )  # fmt: skip


def _row(conn: sqlite3.Connection, record_id: str) -> sqlite3.Row:
    row = conn.execute(f"{_SELECT} WHERE r.id = ?", (record_id,)).fetchone()
    if row is None:
        raise not_found("record", record_id)
    return row


def _row_for_write(
    conn: sqlite3.Connection,
    record_id: str,
    expected_version: int,
    *,
    allow_archived: bool = False,
) -> sqlite3.Row:
    row = _row(conn, record_id)
    if row["version"] != expected_version:
        raise EngineError(
            "version_conflict",
            "Запись уже изменена другим пользователем. Обновите её и повторите правку.",
            status=CONFLICT,
            details={"current_version": row["version"]},
        )
    if row["archived_at"] and not allow_archived:
        raise EngineError(
            "record_archived", "Запись в архиве; сначала восстановите её", status=CONFLICT
        )
    return row


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "dataset_id": row["dataset_id"],
        "version": row["version"],
        "doc": json.loads(row["doc"]),
        "archived_at": row["archived_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
