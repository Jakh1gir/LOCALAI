"""Сверка переноса «было и стало» по каждому реестру (MIG-13).

Значения источника приводятся к единому написанию прямо из базы Local_DB,
значения LOCALAI — из перенесённых документов. Совпасть должны число
записей, архивных записей, номера версий, число строк истории и
контрольная сумма значений. Расхождение — ошибка: перенос отменяется.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from engine import kks, structure
from engine.database import Tx
from engine.errors import EngineError
from engine.validation import coerce_scalar
from server.localdb_import.convert import FieldPlan, canonical
from server.localdb_import.report import Issues
from server.localdb_import.source import SourceRecord


@dataclass
class Stats:
    """Итог по одному реестру: что сделано с записями и что сверять."""

    dataset_id: str
    name: str
    records_new: int = 0
    records_extended: int = 0
    records_unchanged: int = 0
    records_skipped: int = 0
    versions: int = 0
    ids: list[str] = field(default_factory=list)
    source_lines: list[str] = field(default_factory=list)
    source_archived: int = 0
    source_version_sum: int = 0
    source_version_rows: int = 0

    def summary(self) -> dict[str, Any]:
        keys = (
            "dataset_id",
            "name",
            "records_new",
            "records_extended",
            "records_unchanged",
            "records_skipped",
            "versions",
        )
        return {key: getattr(self, key) for key in keys}


class Reconciler:
    def __init__(
        self, tx: Tx, site_id: str, source_texts: dict[int, str], final_text: dict[int, str]
    ) -> None:
        self.tx = tx
        self.site_id = site_id
        self.source_texts = source_texts  # значение Local_DB → его текст
        self.final_text = final_text  # значение Local_DB → текст конечного значения объединения
        self._labels: dict[str, str] = {}
        self._kks: dict[str, str] = {}

    def remember(
        self,
        stats: Stats,
        record: SourceRecord,
        plans: dict[int, FieldPlan],
        version_rows: int,
        final_version: int,
    ) -> None:
        stats.ids.append(record.uuid)
        stats.source_lines.extend(self.source_lines(record, plans))
        stats.source_archived += int(record.archived_at is not None)
        stats.source_version_sum += final_version
        stats.source_version_rows += version_rows

    def source_lines(self, record: SourceRecord, plans: dict[int, FieldPlan]) -> list[str]:
        grouped: dict[str, tuple[FieldPlan, list[str]]] = {}
        for field_id, _ordinal, raw, value_id in record.values:
            plan = plans.get(field_id)
            if plan is None:
                continue
            shown = self._source_display(plan, raw, value_id)
            if shown:
                grouped.setdefault(plan.key, (plan, []))[1].append(shown)
        lines = []
        for key, (plan, values) in grouped.items():
            kept = values if plan.source.multiple else values[-1:]
            lines.append(f"{record.uuid}\t{key}\t" + "\x1f".join(kept))
        return lines

    def check(self, stats: Stats, issues: Issues) -> dict[str, Any]:
        target_lines: list[str] = []
        archived = version_sum = count = rows = 0
        dataset = structure.load_dataset(self.tx.conn, stats.dataset_id)
        schema = structure.load_schema(self.tx.conn, dataset["type_id"])
        kinds = {item.key: item.kind for item in schema.fields}
        for start in range(0, len(stats.ids), 500):
            chunk = stats.ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.tx.conn.execute(
                "SELECT r.id, r.version, r.doc, r.archived_at, (SELECT COUNT(*) FROM"
                " record_versions v WHERE v.record_seq = r.seq) AS kept FROM records r"
                f" WHERE r.id IN ({marks})",
                chunk,
            ):
                count += 1
                archived += int(row["archived_at"] is not None)
                version_sum += row["version"]
                rows += row["kept"]
                target_lines.extend(self._target_lines(row["id"], row["doc"], kinds))
        result = {
            "dataset": stats.name,
            "records_source": len(stats.ids),
            "records_target": count,
            "records_skipped": stats.records_skipped,
            "archived_source": stats.source_archived,
            "archived_target": archived,
            "versions_source": stats.source_version_sum,
            "versions_target": version_sum,
            "history_rows_source": stats.source_version_rows,
            "history_rows_target": rows,
            "checksum_source": _digest(stats.source_lines),
            "checksum_target": _digest(target_lines),
        }
        result["ok"] = all(
            result[f"{name}_source"] == result[f"{name}_target"]
            for name in ("records", "archived", "versions", "history_rows", "checksum")
        )
        if not result["ok"]:
            differing = sorted(set(stats.source_lines) ^ set(target_lines))[:3]
            issues.add(
                "error",
                "reconciliation_mismatch",
                "Сверка не сошлась: перенос отменён",
                {"dataset": stats.name, "lines": differing},
            )
        return result

    # Единое написание ------------------------------------------------------------

    def _source_display(self, plan: FieldPlan, raw: Any, value_id: int | None) -> str:
        if value_id is not None:
            if plan.kind == "choice":
                shown = self.final_text.get(value_id, "")
                return canonical("choice", shown) if shown.strip() else ""
            raw = self.source_texts.get(value_id, "")
        if raw in (None, "") or (isinstance(raw, str) and not raw.strip()):
            return ""
        if plan.kind == "kks":
            return self._kks_object(str(raw))
        if plan.kind == "choice":
            return canonical("choice", str(raw).strip())
        try:
            raw = coerce_scalar(plan.kind, str(raw) if plan.kind == "text" else raw, plan.key)
        except EngineError:
            return str(raw)
        return "" if raw is None else canonical(plan.kind, raw)

    def _kks_object(self, code: str) -> str:
        """Код источника сверяется по объекту, а не по тексту.

        Объект находится по коду или псевдониму, поэтому переименование
        кода в LOCALAI после прошлого переноса сверку не ломает.
        """
        if code not in self._kks:
            found = kks.resolve(self.tx.conn, self.site_id, code)
            self._kks[code] = found["id"] if found else f"нет объекта: {code.strip()}"
        return self._kks[code]

    def _target_lines(self, record_id: str, doc_text: str, kinds: dict[str, str]) -> list[str]:
        lines = []
        for key, value in json.loads(doc_text).items():
            items = value if isinstance(value, list) else [value]
            shown = [self._target_display(kinds[key], item) for item in items]
            lines.append(f"{record_id}\t{key}\t" + "\x1f".join(shown))
        return lines

    def _target_display(self, kind: str, value: Any) -> str:
        if kind == "kks":
            return str(value)
        if kind != "choice":
            return canonical(kind, value)
        if value not in self._labels:
            row = self.tx.conn.execute(
                "SELECT value FROM dictionary_values WHERE id = ?", (value,)
            ).fetchone()
            self._labels[value] = row[0] if row else "?"
        return canonical("choice", self._labels[value])


def _digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()
