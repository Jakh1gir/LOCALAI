"""Записи: создание, правка, архив, история и откат (DATA-04, DATA-05).

Каждое изменение проверяет ожидаемую версию, пишет разницу в историю,
обновляет связи ККС и индекс поиска и оставляет строку аудита — всё в
одной транзакции. Правка без фактических изменений версию не создаёт.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from engine import fulltext, structure, text
from engine.database import Tx, dumps
from engine.errors import CONFLICT, NOT_FOUND, EngineError, not_found
from engine.validation import Normalizer, coerce_scalar

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
    tx.audit(
        f"record.{op}_many",
        "dataset",
        dataset_id,
        dataset_id=dataset_id,
        details={"count": len(ids), "kks_created": writer.normalizer.kks_created},
    )
    return ids


def update(tx: Tx, record_id: str, patch: dict[str, Any], expected_version: int) -> dict[str, Any]:
    row = _row_for_write(tx.conn, record_id, expected_version)
    writer = _Writer(tx, row["dataset_id"])
    old = json.loads(row["doc"])
    changes = writer.normalizer.partial(patch, current=old)
    diff = {key: [old.get(key), new] for key, new in changes.items() if old.get(key) != new}
    if not diff:
        return _record(row)
    writer.apply(row, old, diff, "update")
    tx.audit(
        "record.update",
        "record",
        record_id,
        dataset_id=row["dataset_id"],
        details={"fields": sorted(diff)},
    )
    return get(tx.conn, record_id)


def set_archived(tx: Tx, record_id: str, archived: bool, expected_version: int) -> dict[str, Any]:
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
        raise EngineError("version_unknown", f"У записи нет версии {version}", field="version")
    kept = conn.execute(
        "SELECT 1 FROM record_versions WHERE record_seq = ? AND version = ?", (row["seq"], version)
    ).fetchone()
    if kept is None:
        raise EngineError(
            "version_missing",
            f"Версия {version} в истории записи не сохранилась",
            status=NOT_FOUND,
            field="version",
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


def rollback(tx: Tx, record_id: str, to_version: int, expected_version: int) -> dict[str, Any]:
    """Возвращает значения версии ``to_version`` новой версией записи.

    Откат подчиняется правилам обычной правки: значение должно подходить
    полю, обязательное поле не очищается, а архивное значение справочника
    или объект ККС нельзя поставить заново — их сначала восстанавливают.
    Поля, отправленные в архив, откат не меняет. Версия, которую откатить
    нельзя, остаётся доступной для просмотра.
    """
    row = _row_for_write(tx.conn, record_id, expected_version)
    target = doc_at(tx.conn, record_id, to_version)
    old = json.loads(row["doc"])
    writer = _Writer(tx, row["dataset_id"])
    keys = sorted(set(old) | set(target))
    diff = {
        key: [old.get(key), target.get(key)]
        for key in keys
        if old.get(key) != target.get(key) and not writer.archived_field(key)
    }
    if not diff:
        return _record(row)
    writer.check_restorable(diff, to_version)
    writer.apply(row, old, diff, "rollback", restored_from=to_version)
    tx.audit(
        "record.rollback",
        "record",
        record_id,
        dataset_id=row["dataset_id"],
        details={"to_version": to_version},
    )
    return get(tx.conn, record_id)


# Загрузка готовых записей ------------------------------------------------------


OPS = ("create", "update", "archive", "restore", "rollback", "import")


@dataclass(frozen=True)
class LoadedVersion:
    """Версия записи из другой системы: номер, разница, автор и время сохраняются."""

    version: int
    op: str
    changes: dict[str, Any]
    actor_label: str
    at: str
    origin: str = "import"
    restored_from: int | None = None


@dataclass(frozen=True)
class LoadedRecord:
    """Запись для загрузки: документ уже в виде хранения (списки и ККС — ID)."""

    id: str
    doc: dict[str, Any]
    versions: tuple[LoadedVersion, ...]
    created_at: str
    updated_at: str
    archived_at: str | None = None


def load(tx: Tx, dataset_id: str, items: list[LoadedRecord]) -> int:
    """Загружает записи с историей как есть: перенос базы и восстановление копии.

    В отличие от ``create`` номера версий, авторы и время берутся из
    источника, а обязательность полей не проверяется: перенесённые данные
    не должны теряться из-за правил, появившихся позже. Значения проверяются
    на соответствие виду поля, справочнику и площадке.
    """
    writer = _Writer(tx, dataset_id, allow_archived=True)
    for item in items:
        writer.check_loaded(item)
        with tx.savepoint():
            _insert_loaded(tx, writer, item)
    return len(items)


def _insert_loaded(tx: Tx, writer: _Writer, item: LoadedRecord) -> None:
    try:
        cursor = tx.conn.execute(
            "INSERT INTO records (id, dataset_seq, version, doc, archived_at, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?)",
            (
                item.id,
                writer.dataset["seq"],
                item.versions[-1].version,
                dumps(item.doc),
                item.archived_at,
                item.created_at,
                item.updated_at,
            ),
        )
    except sqlite3.IntegrityError:
        raise EngineError(
            "record_exists",
            "Запись с таким ID уже есть",
            status=CONFLICT,
            details={"record_id": item.id},
        ) from None
    seq = cursor.lastrowid
    assert seq is not None
    _add_loaded_versions(tx, seq, item.versions)
    writer.index(seq, item.doc)


def extend(tx: Tx, item: LoadedRecord) -> int:
    """Дописывает к существующей записи версии новее её текущей. Возвращает их число.

    Перед записью проверяется, что новые разницы, применённые в обратном
    порядке к новому документу, дают текущий документ: история остаётся
    непрерывной, а не склеенной из двух несовпадающих частей.
    """
    row = _row(tx.conn, item.id)
    fresh = [version for version in item.versions if version.version > row["version"]]
    if not fresh:
        return 0
    writer = _Writer(tx, row["dataset_id"], allow_archived=True)
    writer.check_loaded(item)
    if _unwind(item.doc, fresh) != json.loads(row["doc"]):
        raise EngineError(
            "load_history_gap",
            "Новые версии записи не продолжают её текущую историю",
            status=CONFLICT,
            details={"record_id": item.id},
        )
    with tx.savepoint():  # запись дописывается целиком или остаётся прежней
        tx.conn.execute(
            "UPDATE records SET doc = ?, version = ?, archived_at = ?, updated_at = ?"
            " WHERE seq = ?",
            (dumps(item.doc), fresh[-1].version, item.archived_at, item.updated_at, row["seq"]),
        )
        _add_loaded_versions(tx, row["seq"], tuple(fresh))
        tx.conn.execute("DELETE FROM record_kks_links WHERE record_seq = ?", (row["seq"],))
        writer.index(row["seq"], item.doc)
    return len(fresh)


def latest_origin(conn: sqlite3.Connection, record_id: str) -> tuple[int, str] | None:
    """Номер и происхождение последней версии записи; ``None``, если записи нет."""
    row = conn.execute(
        "SELECT v.version, v.origin FROM records r JOIN record_versions v"
        " ON v.record_seq = r.seq WHERE r.id = ? ORDER BY v.version DESC LIMIT 1",
        (record_id,),
    ).fetchone()
    return None if row is None else (row["version"], row["origin"])


def _unwind(doc: dict[str, Any], versions: list[LoadedVersion]) -> dict[str, Any]:
    state = dict(doc)
    for version in reversed(versions):
        for key, (before, _after) in version.changes.items():
            if before is None:
                state.pop(key, None)
            else:
                state[key] = before
    return state


def _add_loaded_versions(tx: Tx, seq: int, versions: tuple[LoadedVersion, ...]) -> None:
    tx.conn.executemany(
        "INSERT INTO record_versions (record_seq, version, op, changes, actor_id,"
        " actor_label, origin, at, restored_from) VALUES (?,?,?,?,NULL,?,?,?,?)",
        [
            (
                seq,
                item.version,
                item.op,
                dumps(item.changes),
                item.actor_label,
                item.origin,
                item.at,
                item.restored_from,
            )
            for item in versions
        ],
    )


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
        allow_archived: bool = False,
    ) -> None:
        self.tx = tx
        self.dataset = structure.load_dataset(tx.conn, dataset_id)
        if self.dataset["archived_at"] and not allow_archived:
            raise EngineError("dataset_archived", "Реестр в архиве; запись в него закрыта")
        self.schema = structure.load_schema(tx.conn, self.dataset["type_id"])
        self.normalizer = Normalizer(
            tx,
            self.schema,
            self.dataset["site_id"],
            create_missing_kks=create_missing_kks,
            audit_kks=audit_kks,
        )
        self.labels = fulltext.Labels(tx.conn)
        self.kks_fields = [field for field in self.schema.fields if field.kind == "kks"]
        self._known: dict[str, set[str]] = {}

    def index(self, seq: int, doc: dict[str, Any]) -> None:
        """Связи ККС и строка поиска для записи, у которой их ещё нет."""
        self._links(seq, doc)
        fulltext.write(self.tx.conn, seq, fulltext.record_text(doc, self.schema, self.labels))

    def check_loaded(self, item: LoadedRecord) -> None:
        """Проверка загружаемой записи: ID, версии, ключи и виды значений."""
        if not text.is_uuid(item.id):
            raise EngineError("record_id", "ID записи должен быть UUID", field="id")
        numbers = [version.version for version in item.versions]
        if not numbers or numbers != sorted(set(numbers)) or numbers[0] < 1:
            raise EngineError(
                "load_versions",
                "Номера версий должны возрастать, начиная с 1 или больше",
                details={"record_id": item.id},
            )
        for version in item.versions:
            if version.op not in OPS:
                raise EngineError("load_versions", f"Неизвестная операция версии: {version.op}")
        for key, value in item.doc.items():
            field = self.schema.by_key(key)
            if field is None:
                raise EngineError(
                    "field_unknown",
                    f"В типе нет поля «{key}»",
                    field=key,
                    details={"record_id": item.id},
                )
            items = value if isinstance(value, list) else [value]
            if field.multiple != isinstance(value, list) or not items:
                raise EngineError(
                    "load_value",
                    f"Поле «{field.name}»: неверная форма значения",
                    field=key,
                    details={"record_id": item.id},
                )
            for one in items:
                if not self._valid(field, one):
                    raise EngineError(
                        "load_value",
                        f"Поле «{field.name}»: недопустимое значение {one!r}",
                        field=key,
                        details={"record_id": item.id},
                    )

    def _valid(self, field: structure.Field, value: Any) -> bool:
        if field.kind in ("choice", "kks"):
            return isinstance(value, str) and value in self._ids(field)
        try:
            coerced = coerce_scalar(field.kind, value, field.key)
        except EngineError:
            return False
        return coerced == value and type(coerced) is type(value)

    def _ids(self, field: structure.Field) -> set[str]:
        """Допустимые ID для поля: значения его справочника или объекты ККС площадки."""
        scope = field.dictionary_id if field.kind == "choice" else f"kks:{self.dataset['site_id']}"
        assert scope is not None
        if scope not in self._known:
            if field.kind == "choice":
                rows = self.tx.conn.execute(
                    "SELECT id FROM dictionary_values WHERE dictionary_id = ?", (scope,)
                )
            else:
                rows = self.tx.conn.execute(
                    "SELECT id FROM kks_objects WHERE site_id = ?", (self.dataset["site_id"],)
                )
            self._known[scope] = {row[0] for row in rows}
        return self._known[scope]

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
        self.index(seq, doc)
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

    def archived_field(self, key: str) -> bool:
        field = self.schema.by_key(key)
        return field is not None and field.archived_at is not None

    def check_restorable(self, diff: dict[str, list[Any]], version: int) -> None:
        """Проверка отката по правилам обычной правки; см. ``rollback``."""
        for key, (before, after) in diff.items():
            field = self.schema.by_key(key)
            if field is None:
                raise EngineError(
                    "rollback_invalid",
                    f"Поля «{key}» больше нет в типе; откат невозможен",
                    field=key,
                )
            if after is None:
                if field.required:
                    raise EngineError(
                        "value_required",
                        f"Поле «{field.name}» обязательно, а в версии {version} оно пустое",
                        field=key,
                    )
                continue
            items = after if isinstance(after, list) else [after]
            if field.multiple != isinstance(after, list) or not all(
                self._valid(field, item) for item in items
            ):
                raise EngineError(
                    "rollback_invalid",
                    f"В версии {version} поле «{field.name}» содержит недопустимое значение;"
                    " откатить её нельзя, просмотр доступен",
                    field=key,
                )
            had = set(before if isinstance(before, list) else [before])
            for item in items:
                if item not in had and field.kind in ("choice", "kks"):
                    self._check_not_archived(field, item)

    def _check_not_archived(self, field: structure.Field, value_id: str) -> None:
        if field.kind == "choice":
            sql = "SELECT value AS shown, archived_at FROM dictionary_values WHERE id = ?"
            code, what = "value_choice_archived", "Значение «{}» в архиве"
        else:
            sql = "SELECT code AS shown, archived_at FROM kks_objects WHERE id = ?"
            code, what = "kks_archived", "Объект ККС «{}» в архиве"
        row = self.tx.conn.execute(sql, (value_id,)).fetchone()
        if row is not None and row["archived_at"]:
            raise EngineError(
                code,
                what.format(row["shown"]) + "; для отката его нужно восстановить",
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
        except sqlite3.IntegrityError:  # позиция поля занята дважды: ошибка в самом коде
            raise EngineError(
                "kks_link_duplicate", "Одна позиция поля ККС указана в записи дважды"
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
        (
            seq,
            version,
            op,
            dumps(changes),
            tx.actor.id,
            tx.actor.label,
            tx.actor.origin,
            tx.now,
            restored_from,
        ),
    )


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
