"""Отчёт переноса: замечания с примерами и текст для человека."""

from __future__ import annotations

from typing import Any

MAX_EXAMPLES = 10
LEVELS = {"error": "Ошибка", "warning": "Внимание", "info": "Сведения"}


class Issues:
    """Замечания, сгруппированные по коду: число случаев и первые примеры.

    ``error`` останавливает перенос: данные не записываются.
    """

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    def add(self, level: str, code: str, message: str, example: Any = None) -> None:
        item = self._items.setdefault(
            code, {"level": level, "code": code, "message": message, "count": 0, "examples": []}
        )
        item["count"] += 1
        if example is not None and len(item["examples"]) < MAX_EXAMPLES:
            item["examples"].append(example)

    def blocking(self) -> bool:
        return any(item["level"] == "error" for item in self._items.values())

    def to_list(self) -> list[dict[str, Any]]:
        order = {"error": 0, "warning": 1, "info": 2}
        return sorted(self._items.values(), key=lambda item: (order[item["level"]], item["code"]))


def as_text(report: dict[str, Any]) -> str:
    """Отчёт для чтения в терминале."""
    lines: list[str] = []
    mode = {
        "dry_run": "Пробный прогон: в базу LOCALAI ничего не записано.",
        "applied": "Перенос выполнен.",
        "refused": "Перенос не выполнен: есть ошибки. В базу LOCALAI ничего не записано.",
    }[report["mode"]]
    source = report["source"]
    lines += [
        mode,
        "",
        f"Источник: {source['path']}",
        (
            f"  схема Local_DB {source['schema_version']}, профиль «{source['profile'] or '—'}»,"
            f" ID базы {source['database_uuid'] or '—'}"
        ),
    ]
    if report.get("site"):
        lines.append(f"Площадка LOCALAI: {report['site']['name']}")
    structure = report.get("structure", {})
    if structure:
        fields = structure["fields"]
        values = structure["values"]
        lines += [
            "",
            (
                f"Структура: типов {structure['types']}, полей {fields['created']}"
                f" (в архиве {fields['archived']}, скрытых {fields['hidden']}),"
                f" справочников {structure['dictionaries']}"
            ),
            (
                f"  значений справочников {values['created']}, в архиве {values['archived']},"
                f" объединено {values['merged']}"
            ),
        ]
    datasets = report.get("datasets", [])
    if datasets:
        lines += ["", "Реестры:"]
        for item in datasets:
            lines.append(
                f"  {item['name']}: новых записей {item['records_new']}, дополнено"
                f" {item['records_extended']}, без изменений {item['records_unchanged']},"
                f" пропущено {item['records_skipped']}, версий {item['versions']}"
            )
    catalog = report.get("kks")
    if catalog:
        lines += [
            "",
            (
                f"Каталог ККС: новых объектов {catalog['created']}, уже были {catalog['existing']},"
                f" только из истории (в архиве) {catalog['history_only']},"
                f" групп похожих кодов {len(catalog['similar'])}"
            ),
        ]
    if report.get("audit") is not None:
        lines.append(f"Журнал: событий {report['audit']['events']}")
    checks = report.get("reconciliation", [])
    if checks:
        lines += ["", "Сверка:"]
        for check in checks:
            verdict = "совпадает" if check["ok"] else "РАСХОЖДЕНИЕ"
            lines.append(
                f"  {check['dataset']}: записей {check['records_source']} →"
                f" {check['records_target']}, сумма значений {verdict}"
            )
    later = report.get("not_transferred", {})
    if later:
        lines += ["", "Переносится на следующих этапах:"]
        for item in later.values():
            lines.append(f"  {item['title']}: {item['count']} (этап {item['stage']})")
    issues = report.get("issues", [])
    if issues:
        lines += ["", "Замечания:"]
        for item in issues:
            lines.append(f"  [{LEVELS[item['level']]}] {item['message']}: {item['count']}")
            for example in item["examples"][:3]:
                lines.append(f"      {example}")
    if report.get("snapshot"):
        lines += ["", f"Защитный снимок LOCALAI: {report['snapshot']}"]
    lines.append(f"Время: {report.get('seconds', 0):.1f} с")
    return "\n".join(lines)
