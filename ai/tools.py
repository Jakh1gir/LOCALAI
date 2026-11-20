"""Инструменты ИИ (ТЗ 9.1), построенные из схемы реестров (AI-02).

Каждый инструмент — обращение к API сервера с правами спросившего. Описания
инструментов собираются на каждый вопрос из ``/ai/schema``: новый реестр
виден ИИ сразу, недоступный пользователю реестр не упоминается вовсе
(AT-28).

Всё, что инструмент вернул, — данные, а не указания: текст документа или
записи не может включить инструмент изменения (AT-40). ``propose`` есть в
наборе, только если сам вопрос пользователя просит изменить данные.

Каждый найденный предмет получает номер источника ``ref``; модель ссылается
на него как ``[n]``, и ответ хранит список источников (AI-07).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from ai.api import Api, Reply
from engine.text import fold, is_uuid

MAX_ROWS = 20
MAX_SOURCES = 12
_CITE = re.compile(r"\[(\d{1,3})\]")
_CHANGE = re.compile(
    r"\b(измени|изменить|исправь|исправить|поправь|замени|заменить|обнови|обновить|"
    r"установи|поставь|запиши|записать|добавь|добавить|внеси|внести|предложи|"
    r"привяжи|привязать|свяжи|связать|сопоставь|сопоставить|отметь|сделай заметку|"
    r"change|update|set|fix|map|link)\w*",
    re.IGNORECASE,
)


def wants_change(question: str) -> bool:
    """Просит ли сам пользователь изменить данные: только тогда ИИ может предложить правку."""
    return _CHANGE.search(question) is not None


# Источники ответа (AI-07) ---------------------------------------------------------


class Sources:
    """Найденные предметы с номерами; одинаковый предмет получает тот же номер."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._index: dict[tuple[str, str, Any], int] = {}

    def add(self, kind: str, entity_id: str, label: str, **extra: Any) -> int:
        key = (kind, entity_id, extra.get("page"))
        if key in self._index:
            return self._index[key]
        number = len(self.items) + 1
        self.items.append({"n": number, "kind": kind, "id": entity_id, "label": label, **extra})
        self._index[key] = number
        return number

    def cited(self, answer: str) -> list[dict[str, Any]]:
        """Источники, на которые ссылается ответ; без ссылок — все найденные (до предела)."""
        numbers = {int(item) for item in _CITE.findall(answer)}
        chosen = [item for item in self.items if item["n"] in numbers]
        return chosen or self.items[:MAX_SOURCES]


# Схема: что пользователь видит ----------------------------------------------------


class Catalog:
    """Реестры и поля из ``/ai/schema``; поиск реестра и поля по названию или ключу."""

    def __init__(self, schema: dict[str, Any]) -> None:
        self.sites = schema.get("sites", [])
        self.datasets: list[dict[str, Any]] = schema.get("datasets", [])
        self.by_id = {item["id"]: item for item in self.datasets}

    def dataset(self, ref: Any) -> dict[str, Any] | None:
        if not isinstance(ref, str) or not ref.strip():
            return None
        if ref in self.by_id:
            return self.by_id[ref]
        wanted = fold(ref).strip()
        exact = [item for item in self.datasets if fold(item["name"]) == wanted]
        if len(exact) == 1:
            return exact[0]
        words = wanted.split()
        close = [item for item in self.datasets if name_matches(item["name"], words)]
        return close[0] if len(close) == 1 else None

    @staticmethod
    def field(dataset: dict[str, Any], ref: Any) -> dict[str, Any] | None:
        if not isinstance(ref, str):
            return None
        for item in dataset["fields"]:
            if ref == item["key"]:
                return item
        wanted = fold(ref).strip()
        for item in dataset["fields"]:
            if fold(item["name"]) == wanted:
                return item
        return None

    def describe(self) -> str:
        """Текст схемы для модели: реестры, поля, виды, значения справочников."""
        if not self.datasets:
            return "Пользователю не доступен ни один реестр."
        lines = []
        for item in self.datasets:
            fields = []
            for field in item["fields"]:
                shown = f"{field['name']} ({field['key']}, {field['kind']}"
                if field.get("unique"):
                    shown += ", уникальное"
                if field.get("choices"):
                    shown += ": " + ", ".join(field["choices"][:15])
                fields.append(shown + ")")
            lines.append(f"- «{item['name']}» [{item['site']}]: " + "; ".join(fields))
        return "\n".join(lines)


def stem_equal(left: str, right: str) -> bool:
    """Слова совпадают с точностью до окончания: «кабелей» — «кабели», «муфт» — «муфты»."""
    if left == right:
        return True
    shorter = min(len(left), len(right))
    if shorter < 3:
        return False
    common = 0
    for one, two in zip(left, right, strict=False):
        if one != two:
            break
        common += 1
    return common >= max(3, shorter - 2)


def words(value: str) -> list[str]:
    return re.findall(r"[0-9a-zа-я]+", fold(value))


def name_matches(name: str, question_words: list[str]) -> bool:
    """Все слова названия реестра есть в вопросе (с точностью до окончания)."""
    wanted = [item for item in words(name) if len(item) >= 3] or words(name)
    return bool(wanted) and all(
        any(stem_equal(item, word) for word in question_words) for item in wanted
    )


# Инструменты ----------------------------------------------------------------------


class Toolbox:
    """Набор инструментов на один ответ."""

    def __init__(
        self,
        api: Api,
        catalog: Catalog,
        sources: Sources,
        *,
        allow_propose: bool = False,
        on_draft: Callable[[dict[str, Any]], None] | None = None,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.api = api
        self.on_tool = on_tool
        self.catalog = catalog
        self.sources = sources
        self.allow_propose = allow_propose
        self.on_draft = on_draft
        self.timings: list[float] = []  # время каждого вызова, мс (PERF-11)
        self.import_id: str | None = None  # открытый мастер импорта (AI-06)
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "schema": self.schema,
            "search": self.search,
            "search_documents": self.search_documents,
            "kks_card": self.kks_card,
            "record": self.record,
            "query": self.query,
            "aggregate": self.aggregate,
            "trace": self.trace,
            "import_preview": self.import_preview,
        }
        if allow_propose:
            self._handlers["propose"] = self.propose

    @property
    def names(self) -> list[str]:
        return list(self._handlers)

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if self.on_tool is not None:
            self.on_tool(name, args if isinstance(args, dict) else {})
        started = time.perf_counter()
        try:
            if handler is None:
                return {"error": "unknown_tool", "message": f"Инструмента «{name}» нет"}
            return handler(args if isinstance(args, dict) else {})
        except (KeyError, TypeError, ValueError) as error:
            return {"error": "bad_arguments", "message": str(error)}
        finally:
            self.timings.append(round((time.perf_counter() - started) * 1000, 1))

    # Описания для модели ----------------------------------------------------------

    def specs(self) -> list[dict[str, Any]]:
        names = [item["name"] for item in self.catalog.datasets]
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
        if self.import_id:
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
        if self.allow_propose:
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

    # Реализация ---------------------------------------------------------------------

    def schema(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"datasets": self.catalog.describe()}

    def search(self, args: dict[str, Any], *, documents_only: bool = False) -> dict[str, Any]:
        needle = str(args.get("text") or "").strip()[:200]
        if not needle:
            return {"error": "bad_arguments", "message": "Нужен текст поиска"}
        reply = self.api.get("/search", q=needle, limit=10, mode="hybrid")
        if not reply.ok:
            return _failure(reply)
        found = reply.body
        result: dict[str, Any] = {}
        if not documents_only:
            result["kks"] = [self._kks_item(item) for item in found["kks"][:8]]
            result["records"] = [
                {
                    "ref": self.sources.add(
                        "record", item["id"], item["label"], dataset_id=item["dataset_id"]
                    ),
                    "dataset": item["dataset"],
                    "label": item["label"],
                    "match": item.get("match"),
                }
                for item in found["records"]
            ]
            result["glossary"] = [
                {"term": item["term"], "meaning": item["meaning"]} for item in found["glossary"]
            ]
        result["documents"] = [
            {
                "ref": self.sources.add(
                    "document", item["document_id"], item["title"], page=item["page"]
                ),
                "title": item["title"],
                "page": item["page"],
                "text": item["snippet"],
                "found_by": item.get("via", "text"),
            }
            for item in found["documents"]
        ]
        result["notes"] = [
            {
                "ref": self.sources.add("note", item["id"], item["title"]),
                "title": item["title"],
                "text": item["snippet"],
            }
            for item in found["notes"]
        ]
        result["found"] = any(result.get(key) for key in ("kks", "records", "documents", "notes"))
        return result

    def search_documents(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.search(args, documents_only=True)

    def resolve(self, code: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Точный объект ККС по коду, псевдониму или коду сигнала и записи, где код встречается."""
        needle = str(code or "").strip()[:200]
        if not needle:
            return None, []
        reply = self.api.get("/search", q=needle, limit=10)
        if not reply.ok:
            return None, []
        exact = [item for item in reply.body["kks"] if item.get("exact")]
        return (exact[0] if exact else None), reply.body["records"]

    def kks_card(self, args: dict[str, Any]) -> dict[str, Any]:
        code = str(args.get("code") or "").strip()
        if is_uuid(code):  # открытая в интерфейсе карточка (AI-06)
            found: dict[str, Any] | None = {"id": code}
            mentions: list[dict[str, Any]] = []
        else:
            found, mentions = self.resolve(code)
        if found is None:
            return {"error": "not_found", "message": f"Код «{code}» не найден в каталоге ККС"}
        reply = self.api.get(f"/kks/{found['id']}")
        if not reply.ok:
            return _failure(reply)
        card = reply.body
        result: dict[str, Any] = {
            "ref": self.sources.add("kks", card["id"], card["code"]),
            "code": card["code"],
            "name": card.get("name"),
            "site": found.get("site"),
            "aliases": [item["alias"] for item in card.get("aliases", [])],
            "usage": [
                {
                    "dataset": self.catalog.by_id[item["dataset_id"]]["name"],
                    "role": item["role"],
                    "records": item["records"],
                }
                for item in card.get("usage", [])
                if item["dataset_id"] in self.catalog.by_id
            ],
            "linked": [
                {
                    "dataset": self.catalog.by_id[item["dataset_id"]]["name"],
                    "records": item["records"],
                }
                for item in card.get("via_links", [])
                if item["dataset_id"] in self.catalog.by_id
            ],
        }
        if found.get("via") == "signal":
            result["signal"] = found.get("signal")
            row = _signal_row(code, mentions)
            if row is not None:
                result["signal_row"] = self.record({"id": row["id"]})
        elif found.get("via") == "alias":  # записи ищутся по официальному коду
            _, mentions = self.resolve(card["code"])
        result["records"] = [
            {
                "ref": self.sources.add(
                    "record", item["id"], item["label"], dataset_id=item["dataset_id"]
                ),
                "dataset": item["dataset"],
                "label": item["label"],
            }
            for item in mentions[:10]
        ]
        papers = self.api.get("/documents", target_kind="kks", target_id=card["id"], limit=10)
        if papers.ok:  # без права на документы — без них, а не ошибка
            result["documents"] = [
                {"ref": self.sources.add("document", item["id"], item["title"]),
                 "title": item["title"]}
                for item in papers.body.get("items", [])
            ]  # fmt: skip
        return result

    def record(self, args: dict[str, Any]) -> dict[str, Any]:
        record_id = str(args.get("id") or "")
        result = self._record(record_id)
        if "error" in result or not args.get("history"):
            return result
        reply = self.api.get(f"/records/{record_id}/history")
        if reply.ok:
            fields = {item["key"]: item["name"] for item in
                      self.catalog.by_id[result["dataset_id"]]["fields"]}  # fmt: skip
            result["history"] = [
                {"version": item["version"], "at": item["at"], "who": item["actor_label"],
                 "op": item["op"],
                 "changes": {fields.get(key, key): value for key, value in item["changes"].items()
                             if key in fields}}
                for item in reply.body.get("items", [])[:10]
            ]  # fmt: skip
        return result

    def _record(self, record_id: str) -> dict[str, Any]:
        reply = self.api.get(f"/records/{record_id}")
        if not reply.ok:
            return _failure(reply)
        record, refs = reply.body["record"], reply.body["refs"]
        dataset = self.catalog.by_id.get(record["dataset_id"])
        if dataset is None:
            return {"error": "no_access", "message": "Реестр записи недоступен"}
        values = render(dataset, record["doc"], refs)
        label = label_of(dataset, values) or record["id"]
        return {
            "ref": self.sources.add("record", record["id"], label, dataset_id=dataset["id"]),
            "id": record["id"],
            "label": label,
            "dataset": dataset["name"],
            "dataset_id": dataset["id"],
            "version": record["version"],
            "archived": record.get("archived_at") is not None,
            "values": values,
            "linked_from": [
                {
                    "dataset": self.catalog.by_id.get(item["dataset_id"], {}).get("name"),
                    "label": reply.body["backlink_refs"].get(item["record_id"], item["record_id"]),
                }
                for item in reply.body.get("backlinks", [])[:20]
            ],
        }

    def query(self, args: dict[str, Any]) -> dict[str, Any]:
        dataset = self.catalog.dataset(args.get("dataset"))
        if dataset is None:
            return _no_dataset(args.get("dataset"))
        request, problem = self._request(dataset, args)
        if problem:
            return problem
        limit = args.get("limit") if isinstance(args.get("limit"), int) else MAX_ROWS
        request["limit"] = max(1, min(limit, MAX_ROWS))
        reply = self.api.post(f"/datasets/{dataset['id']}/records/query", request)
        if not reply.ok:
            return _failure(reply)
        items = []
        for item in reply.body["items"]:
            values = render(dataset, item["doc"], reply.body["refs"])
            label = label_of(dataset, values) or item["id"]
            items.append(
                {
                    "ref": self.sources.add("record", item["id"], label, dataset_id=dataset["id"]),
                    "id": item["id"],
                    "values": values,
                }
            )
        return {
            "dataset": dataset["name"],
            "items": items,
            "more": reply.body.get("next_cursor") is not None,
        }

    def aggregate(self, args: dict[str, Any]) -> dict[str, Any]:
        dataset = self.catalog.dataset(args.get("dataset"))
        if dataset is None:
            return _no_dataset(args.get("dataset"))
        request, problem = self._request(dataset, args)
        if problem:
            return problem
        stat = args.get("stat") or "count"
        if stat != "count":
            return self._stat(dataset, request, stat, args.get("field"))
        if args.get("group_by"):
            field = Catalog.field(dataset, args["group_by"])
            if field is None:
                return {"error": "bad_arguments", "message": f"Нет поля «{args['group_by']}»"}
            reply = self.api.post(
                f"/datasets/{dataset['id']}/records/values",
                {**request, "field": field["key"], "limit": 50},
            )
            if not reply.ok:
                return _failure(reply)
            refs = reply.body["refs"]
            groups = [
                {"value": _shown_value(field, item["value"], refs), "count": item["count"]}
                for item in reply.body["items"]
            ]
            ref = self.sources.add(
                "dataset", dataset["id"], dataset["name"], filter=request,
                group_by=field["key"], value=groups,
            )  # fmt: skip
            return {
                "ref": ref,
                "dataset": dataset["name"],
                "field": field["name"],
                "groups": groups,
                "distinct": reply.body["distinct"],
            }
        reply = self.api.post(f"/datasets/{dataset['id']}/records/count", request)
        if not reply.ok:
            return _failure(reply)
        count = reply.body["count"]
        ref = self.sources.add("dataset", dataset["id"], dataset["name"], filter=request,
                               value=count)  # fmt: skip
        return {"ref": ref, "dataset": dataset["name"], "count": count}

    def _stat(
        self, dataset: dict[str, Any], request: dict[str, Any], stat: str, name: Any
    ) -> dict[str, Any]:
        if stat not in ("min", "max", "sum", "avg"):
            return {"error": "bad_arguments", "message": "stat: count, min, max, sum, avg"}
        field = Catalog.field(dataset, name)
        if field is None:
            return {"error": "bad_arguments", "message": f"Для {stat} нужно поле; «{name}» нет"}
        reply = self.api.post(f"/datasets/{dataset['id']}/records/stats",
                              {**request, "field": field["key"]})  # fmt: skip
        if not reply.ok:
            return _failure(reply)
        found = reply.body
        value = found.get(stat)
        ref = self.sources.add("dataset", dataset["id"], dataset["name"], filter=request,
                               stat=stat, field=field["key"], value=value)  # fmt: skip
        result: dict[str, Any] = {"ref": ref, "dataset": dataset["name"], "field": field["name"],
                                  stat: value, "records_with_value": found["count"]}  # fmt: skip
        edge = found.get(f"{stat}_id")
        if edge:  # запись с минимумом или максимумом — отдельный источник
            shown = self._record(edge)
            if "error" not in shown:
                result["record"] = {"ref": shown["ref"], "label": shown["label"]}
        return result

    def trace(self, args: dict[str, Any]) -> dict[str, Any]:
        start, _ = self.resolve(args.get("from"))
        if start is None:
            return {"error": "not_found", "message": f"Код «{args.get('from')}» не найден"}
        if args.get("to"):
            finish, _ = self.resolve(args.get("to"))
            if finish is None:
                return {"error": "not_found", "message": f"Код «{args.get('to')}» не найден"}
            reply = self.api.get("/graph/path", **{"from": f"kks:{start['id']}",
                                                   "to": f"kks:{finish['id']}"})  # fmt: skip
        else:
            reply = self.api.get("/graph/neighbourhood", node=f"kks:{start['id']}", depth=1)
        if not reply.ok:
            return _failure(reply)
        nodes = {}
        for node in reply.body.get("nodes", []):
            kind, _, entity = node["id"].partition(":")
            nodes[node["id"]] = {
                "ref": self.sources.add(kind, entity, node.get("label") or entity),
                "kind": kind,
                "label": node.get("label"),
            }
        edges = [
            {
                "from": nodes.get(edge["from"], {}).get("label"),
                "to": nodes.get(edge["to"], {}).get("label"),
                "role": edge.get("role"),
                "contact": edge.get("contact"),
            }
            for edge in reply.body.get("edges", [])[:60]
        ]
        return {"nodes": list(nodes.values())[:60], "edges": edges,
                "found": reply.body.get("found", True)}  # fmt: skip

    def propose(self, args: dict[str, Any]) -> dict[str, Any]:
        kind = args.get("kind")
        if kind == "record_edit":
            payload = {"record_id": args.get("record_id"), "values": args.get("values") or {}}
        elif kind == "document_link":
            payload = {"document_id": args.get("document_id"), "page": args.get("page")}
            if args.get("record_id"):
                payload.update({"kind": "record", "target_id": args["record_id"]})
            else:
                found, _ = self.resolve(args.get("code"))
                if found is None:
                    return {"error": "not_found", "message": f"Код «{args.get('code')}» не найден"}
                payload.update({"kind": "kks", "target_id": found["id"]})
        elif kind == "column_mapping":
            dataset = self.catalog.dataset(args.get("dataset"))
            if dataset is None:
                return _no_dataset(args.get("dataset"))
            mapping = {}
            for column, name in (args.get("mapping") or {}).items():
                field = Catalog.field(dataset, name)
                mapping[column] = field["key"] if field else name
            key = Catalog.field(dataset, args.get("key")) if args.get("key") else None
            payload = {"import_id": self.import_id, "sheet": args.get("sheet"),
                       "header_row": args.get("header_row") or 1, "dataset_id": dataset["id"],
                       "mapping": mapping, "key": key["key"] if key else None}  # fmt: skip
        else:
            payload = {"title": args.get("title"), "body": args.get("body")}
        reply = self.api.post(
            "/ai/drafts",
            {"kind": kind, "summary": str(args.get("summary") or "Предложение ИИ")[:500],
             "payload": payload},
        )  # fmt: skip
        if not reply.ok:
            return _failure(reply)
        if self.on_draft is not None:
            self.on_draft(reply.body)
        return {"draft": reply.body["id"], "state": reply.body["state"],
                "note": "Черновик создан; изменение произойдёт, только если человек его примет"}  # fmt: skip

    def import_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.import_id:
            return {"error": "not_found", "message": "Мастер импорта не открыт"}
        dataset = self.catalog.dataset(args.get("dataset")) if args.get("dataset") else None
        reply = self.api.get(
            f"/imports/{self.import_id}/preview",
            sheet=str(args.get("sheet") or ""),
            header_row=args.get("header_row") or 1,
            dataset_id=dataset["id"] if dataset else None,
        )
        if not reply.ok:
            return _failure(reply)
        found = reply.body
        return {key: found.get(key) for key in ("sheet", "header_row", "headers", "sample",
                                                "suggested", "mapping") if key in found}  # fmt: skip

    # Внутреннее ---------------------------------------------------------------------

    def _kks_item(self, item: dict[str, Any]) -> dict[str, Any]:
        shown = {
            "ref": self.sources.add("kks", item["id"], item["code"]),
            "code": item["code"],
            "name": item.get("name"),
            "exact": item.get("exact", False),
        }
        if item.get("via"):
            shown["via"] = item["via"]
        return shown

    def _request(
        self, dataset: dict[str, Any], args: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        request: dict[str, Any] = {}
        where = []
        for item in args.get("where") or []:
            if not isinstance(item, dict):
                return {}, {"error": "bad_arguments", "message": "Условие — объект"}
            field = Catalog.field(dataset, item.get("field"))
            if field is None:
                return {}, {"error": "bad_arguments", "message": f"Нет поля «{item.get('field')}»"}
            where.append({**item, "field": field["key"]})
        if where:
            request["where"] = where
        if args.get("search"):
            request["search"] = str(args["search"])
        if args.get("kks"):
            found, _ = self.resolve(args["kks"])
            if found is None:
                return {}, {"error": "not_found", "message": f"Код «{args['kks']}» не найден"}
            request["kks"] = found["id"]
        return request, None


def render(dataset: dict[str, Any], doc: dict[str, Any], refs: dict[str, Any]) -> dict[str, Any]:
    """Значения записи по названиям полей; ID справочников, ККС и записей — названиями."""
    shown: dict[str, Any] = {}
    for field in dataset["fields"]:
        if field["key"] not in doc or doc[field["key"]] in (None, "", []):
            continue
        shown[field["name"]] = _shown_value(field, doc[field["key"]], refs)
    return shown


def label_of(dataset: dict[str, Any], values: dict[str, Any]) -> str | None:
    for field in dataset["fields"]:
        if field.get("unique") and field["name"] in values:
            return str(values[field["name"]])
    for field in dataset["fields"]:
        if field["name"] in values:
            return str(values[field["name"]])
    return None


def _shown_value(field: dict[str, Any], value: Any, refs: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_shown_value(field, item, refs) for item in value]
    table = {"choice": "choices", "kks": "kks", "record": "records"}.get(field["kind"])
    if table and value is not None:
        return refs.get(table, {}).get(value, value)
    return value


def _signal_row(code: str, mentions: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = fold(code)
    for item in mentions:
        match = item.get("match") or {}
        if fold(str(item.get("label", ""))) == wanted or fold(str(match.get("text", ""))) == wanted:
            return item
    return None


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


def _no_dataset(name: Any) -> dict[str, Any]:
    return {
        "error": "no_dataset",
        "message": f"Реестра «{name}» нет среди доступных пользователю: его нет или нет доступа",
    }


def _failure(reply: Reply) -> dict[str, Any]:
    """Ошибка сервера для модели: «нет доступа» и «не найдено» различаются (AI-08)."""
    if reply.status == 403:
        return {"error": "no_access", "message": "Нет доступа"}
    if reply.status == 404:
        return {"error": "not_found", "message": "Не найдено или недоступно"}
    message = reply.body.get("error", {}).get("message") if isinstance(reply.body, dict) else None
    return {"error": reply.error or "failed", "message": message or "Сервер отказал"}
