"""Каталог ККС: объекты, псевдонимы, связи записей (DATA-06, DATA-07).

Объект ККС имеет постоянный UUID. Код уникален в пределах площадки по
нормализованному виду; при переименовании UUID сохраняется, а прежний код
становится псевдонимом. Канонический код и псевдоним проверяются по обеим
таблицам в одной транзакции записи.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from engine import text
from engine.database import Tx
from engine.errors import CONFLICT, EngineError, not_found

SEARCH_LIMIT = 50


def get(conn: sqlite3.Connection, kks_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM kks_objects WHERE id = ?", (kks_id,)).fetchone()
    if row is None:
        raise not_found("kks", kks_id)
    return _object(row)


def resolve(conn: sqlite3.Connection, site_id: str, code: str) -> dict[str, Any] | None:
    """Находит объект по коду или псевдониму. ``via`` говорит, как он найден."""
    _, norm = text.norm_kks(code)
    row = conn.execute(
        "SELECT * FROM kks_objects WHERE site_id = ? AND code_norm = ?", (site_id, norm)
    ).fetchone()
    if row is not None:
        return {**_object(row), "via": "code"}
    row = conn.execute(
        "SELECT o.*, a.alias FROM kks_aliases a JOIN kks_objects o ON o.id = a.kks_id"
        " WHERE a.site_id = ? AND a.alias_norm = ?",
        (site_id, norm),
    ).fetchone()
    if row is not None:
        return {**_object(row), "via": "alias", "alias": row["alias"]}
    return None


def search(
    conn: sqlite3.Connection, site_id: str, query: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Подсказки по части кода: сначала совпадения с начала, затем остальные."""
    needle = query.strip().upper()
    if not needle:
        return []
    rows = conn.execute(
        "SELECT o.*, MIN(rank) AS rank FROM ("
        " SELECT id AS kks_id, CASE WHEN instr(code_norm, :q) = 1 THEN 0 ELSE 1 END AS rank"
        "   FROM kks_objects WHERE site_id = :site AND instr(code_norm, :q) > 0"
        " UNION ALL"
        " SELECT kks_id, 2 AS rank FROM kks_aliases"
        "   WHERE site_id = :site AND instr(alias_norm, :q) > 0"
        ") hit JOIN kks_objects o ON o.id = hit.kks_id"
        " GROUP BY o.id ORDER BY rank, o.code_norm LIMIT :limit",
        {"q": needle, "site": site_id, "limit": max(1, min(limit, SEARCH_LIMIT))},
    ).fetchall()
    return [_object(row) for row in rows]


def create(
    tx: Tx, site_id: str, code: str, name: str | None = None, *, audit: bool = True
) -> dict[str, Any]:
    shown, norm = text.norm_kks(code)
    _require_site(tx.conn, site_id)
    _check_free(tx.conn, site_id, shown, norm, None)
    kks_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO kks_objects (id, site_id, code, code_norm, name, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?)",
        (kks_id, site_id, shown, norm, name, tx.now, tx.now),
    )
    if audit:
        tx.audit("kks.create", "kks", kks_id, details={"code": shown, "site_id": site_id})
    return get(tx.conn, kks_id)


def ensure(
    tx: Tx, site_id: str, code: str, *, create_missing: bool = True, audit: bool = True
) -> tuple[str, bool]:
    """ID объекта по коду или псевдониму и признак «создан сейчас».

    Неизвестный код создаёт объект, если это разрешено.

    При пакетной загрузке ``audit=False``: число созданных объектов попадает
    в одну строку журнала всей загрузки, а не в тысячу отдельных строк.
    """
    found = resolve(tx.conn, site_id, code)
    if found is None:
        if not create_missing:
            shown, _ = text.norm_kks(code)
            raise EngineError("kks_unknown", f"Код ККС «{shown}» не найден на площадке")
        return create(tx, site_id, code, audit=audit)["id"], True
    if found["archived_at"]:
        raise EngineError(
            "kks_archived",
            f"Объект ККС «{found['code']}» в архиве; для новой связи его нужно восстановить",
        )
    return found["id"], False


def rename(tx: Tx, kks_id: str, new_code: str, *, keep_alias: bool = True) -> dict[str, Any]:
    """Меняет код объекта. UUID и связи сохраняются, прежний код остаётся псевдонимом."""
    current = get(tx.conn, kks_id)
    shown, norm = text.norm_kks(new_code)
    if shown == current["code"]:
        return current
    site_id = current["site_id"]
    old_norm = text.norm_kks(current["code"])[1]
    if norm != old_norm:
        _check_free(tx.conn, site_id, shown, norm, kks_id)
        tx.conn.execute(
            "DELETE FROM kks_aliases WHERE kks_id = ? AND alias_norm = ?", (kks_id, norm)
        )
    tx.conn.execute(
        "UPDATE kks_objects SET code = ?, code_norm = ?, updated_at = ? WHERE id = ?",
        (shown, norm, tx.now, kks_id),
    )
    if keep_alias and norm != old_norm:
        _insert_alias(tx, kks_id, site_id, current["code"], old_norm, "former")
    _fulltext().reindex_kks(tx.conn, kks_id)
    tx.audit("kks.rename", "kks", kks_id, details={"from": current["code"], "to": shown})
    return get(tx.conn, kks_id)


def update(
    tx: Tx, kks_id: str, *, name: str | None = None, archived: bool | None = None
) -> dict[str, Any]:
    """Описание и архив. Архив сохраняет связи и удерживает код за объектом."""
    get(tx.conn, kks_id)
    if name is not None:
        tx.conn.execute(
            "UPDATE kks_objects SET name = ?, updated_at = ? WHERE id = ?",
            (name.strip() or None, tx.now, kks_id),
        )
    if archived is not None:
        tx.conn.execute(
            "UPDATE kks_objects SET archived_at = ?, updated_at = ? WHERE id = ?",
            (tx.now if archived else None, tx.now, kks_id),
        )
        tx.audit(f"kks.{'archive' if archived else 'restore'}", "kks", kks_id)
    return get(tx.conn, kks_id)


def add_alias(tx: Tx, kks_id: str, alias: str) -> dict[str, Any]:
    current = get(tx.conn, kks_id)
    shown, norm = text.norm_kks(alias)
    _check_free(tx.conn, current["site_id"], shown, norm, None)
    alias_id = _insert_alias(tx, kks_id, current["site_id"], shown, norm, "synonym")
    tx.audit("kks.alias_add", "kks", kks_id, details={"alias": shown})
    return {"id": alias_id, "kks_id": kks_id, "alias": shown, "kind": "synonym"}


def remove_alias(tx: Tx, alias_id: str) -> None:
    row = tx.conn.execute("SELECT * FROM kks_aliases WHERE id = ?", (alias_id,)).fetchone()
    if row is None:
        raise not_found("kks_alias", alias_id)
    tx.conn.execute("DELETE FROM kks_aliases WHERE id = ?", (alias_id,))
    tx.audit("kks.alias_remove", "kks", row["kks_id"], details={"alias": row["alias"]})


def card(conn: sqlite3.Connection, kks_id: str) -> dict[str, Any]:
    """Карточка ККС: объект, псевдонимы и число связанных записей по реестрам и ролям."""
    found = get(conn, kks_id)
    aliases = conn.execute(
        "SELECT id, alias, kind, created_at FROM kks_aliases WHERE kks_id = ?"
        " ORDER BY alias_norm",
        (kks_id,),
    ).fetchall()
    usage = conn.execute(
        "SELECT d.id AS dataset_id, l.role, COUNT(DISTINCT l.record_seq) AS records"
        " FROM record_kks_links l JOIN records r ON r.seq = l.record_seq"
        " JOIN datasets d ON d.seq = r.dataset_seq"
        " WHERE l.kks_id = ? AND r.archived_at IS NULL"
        " GROUP BY d.id, l.role ORDER BY d.name_norm, l.role",
        (kks_id,),
    ).fetchall()
    found["aliases"] = [dict(row) for row in aliases]
    found["usage"] = [dict(row) for row in usage]
    return found


def record_links(conn: sqlite3.Connection, record_seq: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT l.field_key, l.ordinal, l.role, l.kks_id, o.code FROM record_kks_links l"
        " JOIN kks_objects o ON o.id = l.kks_id WHERE l.record_seq = ?"
        " ORDER BY l.field_key, l.ordinal",
        (record_seq,),
    ).fetchall()
    return [dict(row) for row in rows]


# Внутреннее -----------------------------------------------------------------


def _fulltext() -> Any:
    from engine import fulltext

    return fulltext


def _object(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "site_id": row["site_id"],
        "code": row["code"],
        "name": row["name"],
        "archived_at": row["archived_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _require_site(conn: sqlite3.Connection, site_id: str) -> None:
    if conn.execute("SELECT 1 FROM sites WHERE id = ?", (site_id,)).fetchone() is None:
        raise not_found("site", site_id)


def _check_free(
    conn: sqlite3.Connection, site_id: str, shown: str, norm: str, own_id: str | None
) -> None:
    """Код не должен совпадать с кодом или псевдонимом другого объекта площадки."""
    row = conn.execute(
        "SELECT id, code FROM kks_objects WHERE site_id = ? AND code_norm = ?",
        (site_id, norm),
    ).fetchone()
    if row is not None and row["id"] != own_id:
        raise EngineError(
            "kks_code_taken",
            f"Код «{shown}» уже принадлежит объекту ККС на этой площадке",
            status=CONFLICT,
            details={"kks_id": row["id"]},
        )
    row = conn.execute(
        "SELECT a.kks_id, o.code FROM kks_aliases a JOIN kks_objects o ON o.id = a.kks_id"
        " WHERE a.site_id = ? AND a.alias_norm = ?",
        (site_id, norm),
    ).fetchone()
    if row is not None and row["kks_id"] != own_id:
        raise EngineError(
            "kks_code_taken",
            f"Код «{shown}» занят как псевдоним объекта «{row['code']}»",
            status=CONFLICT,
            details={"kks_id": row["kks_id"]},
        )


def _insert_alias(
    tx: Tx, kks_id: str, site_id: str, shown: str, norm: str, kind: str
) -> str:
    alias_id = text.new_id()
    tx.conn.execute(
        "INSERT INTO kks_aliases (id, kks_id, site_id, alias, alias_norm, kind, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (alias_id, kks_id, site_id, shown, norm, kind, tx.now),
    )
    return alias_id
