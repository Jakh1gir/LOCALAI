"""Мастер импорта в режиме магнита ККС (IMP-13…16).

Таблица «строка на сигнал» раскладывается на реестры «<имя> — сигналы» и
«<имя> — кабели»; таблица аппаратов (ККС уникален) — в реестр «<имя>». Это
тот же код, что переносит выгрузку Local_DB (``server.localdb_export``):
повтор той же книги ничего не создаёт и не меняет версий (IMP-06).

План — полное выполнение раскладки с откатом: его счётчики точны. При
применении раскладка выполняется снова; если с плана в целевых реестрах,
структуре или правиле кода что-то изменилось или счётчики разошлись, план
устарел и ничего не записывается (IMP-04).
"""

from __future__ import annotations

from typing import Any

from engine import EngineError, Tx, kks_rules, magnet, structure, text
from engine.access import Access
from engine.errors import CONFLICT, FORBIDDEN
from server import xlsx
from server.localdb_export.importer import is_signal_table, load_group, target_names

_COUNTED = ("create", "update", "unchanged", "conflict", "error")


class _Undo(Exception):
    pass


def splitter(conn: Any) -> Any:
    return kks_rules.splitter(conn)


def plan(
    tx: Tx, access: Access, page: xlsx.Sheet, settings: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, int]]:
    site, name, layout = _inputs(tx, page, settings)
    signals = is_signal_table(layout, page.rows)
    _check_rights(tx, access, site, name, signals)
    report: dict[str, Any] = {"write_errors": []}
    result: dict[str, Any] = {}
    try:
        with tx.savepoint():
            result = load_group(tx, site, name, page.headers, page.rows, layout, report)
            raise _Undo
    except _Undo:
        pass
    summary = {
        "mode": "magnet",
        "rows": len(page.rows),
        "counts": _counts(result, report),
        "datasets": target_names(name, signals),
        "layout": layout.to_dict(),
        "plan": result.get("plan"),
        "issues": result.get("issues", [])[:200],
        "write_errors": report["write_errors"][:200],
    }
    return summary, {}


def apply(
    tx: Tx, access: Access, page: xlsx.Sheet, settings: dict[str, Any], batch: dict[str, Any]
) -> dict[str, Any]:
    site, name, layout = _inputs(tx, page, settings)
    signals = is_signal_table(layout, page.rows)
    _check_rights(tx, access, site, name, signals)
    if _touched_since(tx, site, target_names(name, signals), batch["plan_cursor"] or 0):
        raise _stale("Целевые реестры, структура или правило кода изменились после проверки")
    report: dict[str, Any] = {"write_errors": []}
    result = load_group(tx, site, name, page.headers, page.rows, layout, report)
    if report["write_errors"]:
        first = report["write_errors"][0]
        raise EngineError(
            "import_failed",
            f"Строка {first['row']}: {first['message']}. Ничего не записано",
            status=CONFLICT,
            details={"row": first["row"], "code": first["code"]},
        )
    counts = _counts(result, report)
    if counts != batch["summary"]["counts"]:
        raise _stale("Итог раскладки разошёлся с планом")
    return {
        "counts": counts,
        "datasets": target_names(name, signals),
        "result": result,
    }


def _inputs(
    tx: Tx, page: xlsx.Sheet, settings: dict[str, Any]
) -> tuple[dict[str, Any], str, magnet.Layout]:
    site = _site(tx, settings.get("site_id"))
    name = text.clean_name(settings.get("name") or page.name, "Название реестра")
    given = settings.get("layout")
    try:
        layout = (
            magnet.from_dict(given)
            if given
            else magnet.suggest_layout(
                page.headers, [row for _, row in page.rows], splitter(tx.conn)
            )
        )
    except (TypeError, ValueError) as error:
        raise EngineError("magnet_layout_invalid", f"Раскладка: {error}", field="layout") from None
    unknown = [column for column in _columns(layout) if column not in page.headers]
    if unknown:
        raise EngineError(
            "magnet_layout_invalid",
            f"Столбцов нет на листе: {', '.join(unknown)}",
            field="layout",
        )
    return site, name, layout


def _columns(layout: magnet.Layout) -> list[str]:
    found = [item for item in (layout.kks, layout.signal, layout.cable) if item]
    found += list(layout.places) + list(layout.channel) + list(layout.service)
    found += [column for columns in layout.contacts.values() for column in columns]
    return found


def _site(tx: Tx, site_id: str | None) -> dict[str, Any]:
    if site_id:
        site = structure.get_site(tx.conn, site_id)
        if site["archived_at"]:
            raise EngineError("import_target", "Площадка в архиве", field="site_id")
        return site
    active = [item for item in structure.list_sites(tx.conn) if not item["archived_at"]]
    if len(active) != 1:
        raise EngineError("import_target", "Выберите площадку", field="site_id")
    return active[0]


def _existing(tx: Tx, site: dict[str, Any], names: list[str]) -> list[dict[str, Any] | None]:
    by_name = {
        text.norm_name(item["name"]): item
        for item in structure.list_datasets(tx.conn, site_id=site["id"])
    }
    return [by_name.get(text.norm_name(name)) for name in names]


def _check_rights(tx: Tx, access: Access, site: dict[str, Any], name: str, signals: bool) -> None:
    """С разделом «Структура» реестры создаются; без него нужны готовые реестры с правом импорта."""
    if access.has("structure"):
        return
    found = _existing(tx, site, target_names(name, signals))
    if all(item is not None and access.can(item["id"], "import") for item in found):
        return
    raise EngineError(
        "forbidden",
        "Недостаточно прав: нужны реестры раскладки с правом импорта или раздел «Структура»",
        status=FORBIDDEN,
    )


def _touched_since(tx: Tx, site: dict[str, Any], names: list[str], cursor: int) -> bool:
    ids = [item["id"] for item in _existing(tx, site, names) if item is not None]
    marks = ",".join("?" * len(ids)) or "NULL"
    row = tx.conn.execute(
        "SELECT 1 FROM audit_log WHERE seq > ? AND origin <> 'localdb' AND ("
        " entity_kind IN ('type', 'field', 'kks_rules')"
        f" OR dataset_id IN ({marks}) OR (entity_kind = 'dataset' AND entity_id IN ({marks})))"
        " LIMIT 1",
        (cursor, *ids, *ids),
    ).fetchone()
    return row is not None


def _counts(result: dict[str, Any], report: dict[str, Any]) -> dict[str, int]:
    counts = dict.fromkeys(_COUNTED, 0)
    parts = result.get("records", {})
    groups = parts.values() if "signals" in parts else [parts]
    for group in groups:
        counts["create"] += group.get("created", 0)
        counts["update"] += group.get("updated", 0)
        counts["unchanged"] += group.get("unchanged", 0)
    counts["error"] = len(report.get("write_errors", []))
    return counts


def _stale(message: str) -> EngineError:
    return EngineError(
        "import_stale", f"{message}. План устарел: постройте его заново", status=CONFLICT
    )
