"""Правила кода ККС станции (DATA-16): из кода выделяются ККС объекта и признак сигнала.

По умолчанию признак сигнала — ``X``, латинская буква и две цифры в конце кода:
``01MAJ81AA203XB01`` → объект ``01MAJ81AA203``, сигнал ``XB01``. Правило одно на
базу (``scope = '*'``) и меняется администратором структуры без изменения кода.
Его применяют импорт, поиск, карточка ККС и ИИ.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from engine import kks, text
from engine.database import Tx
from engine.errors import EngineError

MAX_PATTERN = 200
_SAMPLE = "01MAJ81AA203XB01"


def get_rule(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM kks_rules WHERE scope = '*'").fetchone()
    return {"signal_pattern": row["signal_pattern"], "updated_at": row["updated_at"]}


def set_rule(tx: Tx, signal_pattern: Any) -> dict[str, Any]:
    _compile(signal_pattern)
    before = get_rule(tx.conn)["signal_pattern"]
    if signal_pattern != before:
        tx.conn.execute(
            "UPDATE kks_rules SET signal_pattern = ?, updated_at = ? WHERE scope = '*'",
            (signal_pattern, tx.now),
        )
        tx.audit("kks.rules", "kks_rules", None, details={"from": before, "to": signal_pattern})
    return get_rule(tx.conn)


def split(conn: sqlite3.Connection, code: Any) -> tuple[str, str | None]:
    """``(ККС объекта, признак сигнала или None)`` в нормализованном виде кода."""
    norm = text.norm_kks(str(code))[1]
    found = _compile(get_rule(conn)["signal_pattern"]).match(norm)
    if found is None:
        return norm, None
    cut = len(found.group("object"))
    return norm[:cut].rstrip(" _-"), norm[cut:].lstrip(" _-")


def resolve(conn: sqlite3.Connection, site_id: str, code: str) -> dict[str, Any] | None:
    """Объект по коду, псевдониму или коду сигнала. ``via: "signal"`` — найден по сигналу."""
    found = kks.resolve(conn, site_id, code)
    if found is not None:
        return found
    object_code, signal = split(conn, code)
    if signal is None:
        return None
    found = kks.resolve(conn, site_id, object_code)
    if found is None:
        return None
    return {**found, "via": "signal", "signal": signal, "signal_code": code.strip()}


def _compile(pattern: Any) -> re.Pattern[str]:
    """Признак сигнала проверяется на нормализованном коде (верхний регистр, латиница)."""
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN:
        raise EngineError(
            "kks_rule_invalid",
            f"Правило: выражение до {MAX_PATTERN} знаков",
            field="signal_pattern",
        )
    try:
        compiled = re.compile(rf"(?P<object>.+?)[ _-]?(?:{pattern})$")
    except re.error as error:
        raise EngineError(
            "kks_rule_invalid", f"Правило не разбирается: {error.msg}", field="signal_pattern"
        ) from None
    if re.fullmatch(pattern, "") is not None:
        raise EngineError(
            "kks_rule_invalid", "Правило не должно подходить к пустому коду", field="signal_pattern"
        )
    return compiled


def example(conn: sqlite3.Connection) -> dict[str, Any]:
    object_code, signal = split(conn, _SAMPLE)
    return {"code": _SAMPLE, "object": object_code, "signal": signal}
