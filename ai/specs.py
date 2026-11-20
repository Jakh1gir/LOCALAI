"""Описания инструментов ИИ для модели (ТЗ 9.1, AI-02): строятся на каждый вопрос.

Список реестров берётся из схемы пользователя, поэтому новый реестр виден ИИ
сразу, а недоступный не упоминается. ``propose`` описывается, только если
вопрос просит изменение; ``import_preview`` — только при открытом мастере.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ai.tools import Toolbox


def build(box: Toolbox) -> list[dict[str, Any]]:
    names = [item["name"] for item in box.catalog.datasets]
    dataset = {
        "type": "string",
        "description": "Реестр: " + (", ".join(f"«{name}»" for name in names) or "нет"),
    }
    if names:
        dataset["enum"] = names
    where = {
        "type": "array",
        "description": "Условия «И»: {field: название или ключ поля, op: eq|ne|lt|lte|gt|gte|"
        "in|contains|starts|empty|not_empty, value}",
        "items": {"type": "object"},
    }
    specs = [
        _spec("schema", "Реестры и поля, доступные пользователю", {}),
        _spec(
            "search",
            "Общий поиск: коды ККС, записи всех реестров, документы, заметки, словарь",
            {"text": {"type": "string"}},
            ["text"],
        ),
        _spec(
            "search_documents",
            "Поиск по тексту документов и подтверждённым заметкам знаний",
            {"text": {"type": "string"}},
            ["text"],
        ),
        _spec(
            "kks_card",
            "Объект по коду ККС, псевдониму или коду сигнала: карточка, связанные записи;"
            " для кода сигнала — строка сигнала (шкаф, клеммы, кабель)",
            {"code": {"type": "string"}},
            ["code"],
        ),
        _spec(
            "record",
            "Запись целиком по её id (ref из результатов); history=true — кто и что менял",
            {"id": {"type": "string"}, "history": {"type": "boolean"}},
            ["id"],
        ),
        _spec(
            "query",
            "Записи реестра по условиям (до 20)",
            {
                "dataset": dataset,
                "where": where,
                "search": {"type": "string"},
                "kks": {"type": "string", "description": "код ККС: записи, связанные с ним"},
                "limit": {"type": "integer"},
            },
            ["dataset"],
        ),
        _spec(
            "aggregate",
            "Подсчёт записей реестра по условиям; с group_by — число записей по значениям"
            " поля; stat (min, max, sum, avg) с field — итог числового поля или даты."
            " Любые числа в ответе бери только отсюда",
            {
                "dataset": dataset,
                "where": where,
                "search": {"type": "string"},
                "kks": {"type": "string"},
                "group_by": {"type": "string", "description": "название или ключ поля"},
                "stat": {"type": "string", "enum": ["count", "min", "max", "sum", "avg"]},
                "field": {"type": "string", "description": "поле для stat"},
            },
            ["dataset"],
        ),
        _spec(
            "trace",
            "Связи объекта: всё, что связано с кодом ККС (to пусто), или путь между двумя кодами",
            {"from": {"type": "string"}, "to": {"type": "string"}},
            ["from"],
        ),
    ]
    specs.append(
        _spec(
            "sheet_graph",
            "Схемы: на каких листах объект ККС и что с ним соединено (code); или соединения"
            " элемента на листе (document_id, page, element), путь до элемента to; depth — шагов",
            {
                "code": {"type": "string"},
                "document_id": {"type": "string"},
                "page": {"type": "string"},
                "element": {"type": "string"},
                "to": {"type": "string"},
                "depth": {"type": "integer"},
            },
        )
    )
    if box.import_id:
        specs.append(
            _spec(
                "import_preview",
                "Открытый в мастере импорта лист Excel: заголовки, первые строки, подсказки"
                " сопоставления с полями реестра",
                {
                    "sheet": {"type": "string"},
                    "header_row": {"type": "integer"},
                    "dataset": dataset,
                },
                ["sheet"],
            )
        )
    if box.allow_propose:
        specs.append(
            _spec(
                "propose",
                "Предложить изменение, которое человек примет или отклонит: правка записи"
                " (kind=record_edit, record_id, values: {поле: значение}); заметка знаний"
                " (kind=note, title, body); связь документа с объектом (kind=document_link,"
                " document_id, code — код ККС, или record_id, page); сопоставление столбцов"
                " Excel в мастере импорта (kind=column_mapping, sheet, dataset,"
                " mapping: {столбец: поле}, key)",
                {
                    "kind": {
                        "type": "string",
                        "enum": ["record_edit", "note", "document_link", "column_mapping"],
                    },
                    "summary": {"type": "string"},
                    "record_id": {"type": "string"},
                    "values": {"type": "object"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "document_id": {"type": "string"},
                    "code": {"type": "string"},
                    "page": {"type": "string"},
                    "sheet": {"type": "string"},
                    "header_row": {"type": "integer"},
                    "dataset": dataset,
                    "mapping": {"type": "object"},
                    "key": {"type": "string"},
                },
                ["kind", "summary"],
            )
        )
    return specs


def _spec(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }
