"""Точные вопросы без модели (AI-03, PERF-10: до 300 мс).

Три вида вопросов разбираются правилами и выполняются теми же инструментами,
что и у модели:

- **код ККС или псевдоним** — карточка объекта (AT-25);
- **код сигнала** — строка сигнала: шкаф, клеммник, клеммы, кабель (AT-43,
  AI-16); код разбирается по правилу станции на сервере;
- **«сколько …»** — подсчёт по реестру, названному в вопросе, с условиями по
  значениям справочников и коду ККС (AT-26). Число считает сервер.

Строгий режим (модель есть): вопрос отвечается без модели, только если в нём
не осталось непонятых слов. Иначе он уходит модели, у которой те же
инструменты. Без модели правила отвечают на всё, что поняли.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ai.tools import Catalog, Toolbox, stem_equal, words

COUNT = frozenset({"сколько", "количество", "число", "посчитай", "подсчитай", "подсчет"})
# Слова, которые не меняют смысла точного вопроса.
STOP = frozenset(
    [
        "а",
        "в",
        "во",
        "все",
        "всего",
        "всех",
        "вообще",
        "где",
        "почему",
        "зачем",
        "когда",
        "кто",
        "чем",
        "как",
        "какое",
        "каких",
        "да",
        "дай",
        "для",
        "его",
        "ее",
        "есть",
        "же",
        "за",
        "и",
        "из",
        "или",
        "им",
        "их",
        "к",
        "как",
        "какая",
        "какие",
        "каким",
        "какой",
        "ко",
        "куда",
        "ли",
        "мне",
        "на",
        "нам",
        "нас",
        "найди",
        "о",
        "об",
        "объект",
        "объекта",
        "объекту",
        "от",
        "по",
        "под",
        "покажи",
        "про",
        "реестр",
        "реестре",
        "реестра",
        "реестрах",
        "сейчас",
        "скажи",
        "с",
        "со",
        "сигнал",
        "сигнала",
        "станции",
        "таблице",
        "таблица",
        "там",
        "то",
        "у",
        "что",
        "это",
        "эта",
        "этот",
        "базе",
        "база",
        "записей",
        "записи",
        "запись",
        "строк",
        "строки",
        "штук",
        "имеется",
        "имеются",
        "числится",
        "числятся",
        "находится",
        "находятся",
        "заведен",
        "заведена",
        "заведено",
        "заведены",
        "подключен",
        "подключена",
        "подключено",
        "подключены",
        "идет",
        "идут",
        "карточка",
        "карточку",
        "код",
        "кода",
        "ккс",
        "kks",
        "такое",
        "такой",
        "информация",
        "расскажи",
        "описание",
    ]
)
ROLES = {
    "object": "объект",
    "signal": "сигнал",
    "route_start": "начало трассы",
    "route_end": "конец трассы",
    "route_via": "по трассе",
    "related": "связь",
}
# Кандидат в код: буквы и цифры вместе, 4–40 знаков (10LAC10AP001, ПЭН-1, 01MAJ81AA203XB01).
_CODE = re.compile(r"(?<![\w-])(?=[\w-]*\d)(?=[\w-]*[^\W\d_])[\w][\w.-]{3,39}(?![\w-])")


@dataclass
class Fast:
    text: str


def answer(question: str, box: Toolbox, *, strict: bool) -> Fast | None:
    asked = words(question)
    found = codes(question)
    if COUNT & set(asked):
        return _count(asked, found, box, strict=strict)
    if not found:
        return None
    code_words = {word for code in found for word in words(code)}
    rest = [word for word in asked if word not in STOP and word not in code_words]
    if strict and rest:
        return None
    for code in found[:2]:
        card = box.run("kks_card", {"code": code})
        if "error" not in card:
            return Fast(_card_text(card, code))
    return None


def codes(question: str) -> list[str]:
    """Кандидаты в код ККС, псевдоним или код сигнала: буквы вместе с цифрами."""
    return [item for item in _CODE.findall(question) if not item.isdigit()]


# Подсчёт --------------------------------------------------------------------------


def _count(asked: list[str], codes: list[str], box: Toolbox, *, strict: bool) -> Fast | None:
    scored: list[tuple[int, dict[str, Any], set[str]]] = []
    for dataset in box.catalog.datasets:
        named = [item for item in words(dataset["name"]) if len(item) >= 3] or words(
            dataset["name"]
        )
        used = {word for word in asked for item in named if stem_equal(item, word)}
        if named and all(any(stem_equal(item, word) for word in asked) for item in named):
            scored.append((len(named), dataset, used))
    if not scored:
        return None
    best = max(score for score, _, _ in scored)
    chosen = [(dataset, used) for score, dataset, used in scored if score == best]
    dataset, used = chosen[0]
    used = set(used) | COUNT
    where, shown = [], []
    if len(chosen) == 1:
        for field in dataset["fields"]:
            for label in field.get("choices", []):
                label_words = words(label)
                if label_words and all(
                    any(stem_equal(item, word) for word in asked) for item in label_words
                ):
                    where.append({"field": field["key"], "op": "eq", "value": label})
                    shown.append(f"{field['name']} = {label}")
                    used |= {
                        word for word in asked for item in label_words if stem_equal(item, word)
                    }
    code = codes[0] if codes else None
    if code:
        used |= set(words(code))
    rest = [word for word in asked if word not in used and word not in STOP]
    if strict and rest:
        return None
    lines = []
    for item, _ in chosen[:3]:
        args: dict[str, Any] = {"dataset": item["id"]}
        if where and item is dataset:
            args["where"] = where
        if code:
            args["kks"] = code
        result = box.run("aggregate", args)
        if "error" in result:
            if result["error"] == "not_found" and code:
                return Fast(f"Код «{code}» не найден в каталоге ККС.")
            continue
        condition = ", ".join([*shown, *([f"объект {code}"] if code else [])])
        where_text = f" с условием «{condition}»" if condition and item is dataset else ""
        if code and item is not dataset:
            where_text = f" с условием «объект {code}»"
        site = f" ({item['site']})" if len(chosen) > 1 else ""
        lines.append(
            f"В реестре «{item['name']}»{site}{where_text} — {_records(result['count'])}"
            f" [{result['ref']}]."
        )
    return Fast("\n".join(lines)) if lines else None


def _records(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        word = "записей"
    elif count % 10 == 1:
        word = "запись"
    elif 2 <= count % 10 <= 4:
        word = "записи"
    else:
        word = "записей"
    return f"{count} {word}"


# Карточка ККС и сигнал --------------------------------------------------------------


def _card_text(card: dict[str, Any], code: str) -> str:
    title = f"**{card['code']}**" + (f" — {card['name']}" if card.get("name") else "")
    lines = []
    if "signal" in card:
        lines.append(
            f"Сигнал **{code.strip()}**: объект {title} [{card['ref']}],"
            f" признак сигнала {card['signal']}."
        )
        row = card.get("signal_row")
        if row and "error" not in row:
            lines.append(
                f"Строка сигнала «{row['label']}» в реестре «{row['dataset']}» [{row['ref']}]:"
            )
            lines += [f"- {name}: {_value(value)}" for name, value in row["values"].items()]
        else:
            lines.append("Строки этого сигнала в доступных вам реестрах нет.")
        return "\n".join(lines)
    lines.append(
        f"{title} [{card['ref']}]" + (f", площадка {card['site']}." if card.get("site") else ".")
    )
    if card["aliases"]:
        lines.append("Псевдонимы: " + ", ".join(card["aliases"]) + ".")
    if card["usage"]:
        lines.append(
            "Связанные записи: "
            + "; ".join(
                f"«{item['dataset']}» — {item['records']} ({ROLES.get(item['role'], item['role'])})"
                for item in card["usage"]
            )
            + "."
        )
    if card["linked"]:
        lines.append(
            "Через ссылки записей: "
            + "; ".join(f"«{item['dataset']}» — {item['records']}" for item in card["linked"])
            + "."
        )
    if not card["usage"] and not card["linked"]:
        lines.append("Записей, связанных с объектом, в доступных вам реестрах нет.")
    if card["records"]:
        lines.append(
            "Записи: "
            + ", ".join(
                f"{item['label']} [{item['ref']}] («{item['dataset']}»)"
                for item in card["records"][:5]
            )
            + "."
        )
    return "\n".join(lines)


def _value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_value(item) for item in value)
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


__all__ = ["Catalog", "Fast", "answer"]
