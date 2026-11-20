"""Привязки к базе, выполнение ожидаемых запросов и построение эталона."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from server.openapi import TARGET

from .answers import Canonical
from .format import (
    ApiExpected,
    Profile,
    Question,
    QuestionError,
    Step,
    Suite,
    pointer,
    render,
    validate_request,
    validate_suite,
)
from .workspace import Client, role_client, serve


class Catalog:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.sites = client.ok("GET", "/api/v1/sites")["items"]
        self.datasets = client.ok("GET", "/api/v1/datasets")["items"]
        self.types = {
            item["id"]: client.ok("GET", f"/api/v1/types/{item['id']}")
            for item in client.ok("GET", "/api/v1/types")["items"]
        }
        self.identities = {item["id"]: f"site:{item['name']}" for item in self.sites}
        self.unique: dict[str, list[str]] = {}
        for dataset in self.datasets:
            site = self.identities[dataset["site_id"]]
            self.identities[dataset["id"]] = f"dataset:{site}/{dataset['name']}"
            schema = self.types[dataset["type_id"]]
            self.unique[dataset["id"]] = [
                field["key"] for field in schema["fields"] if field["unique_values"]
            ]
        for schema in self.types.values():
            self.identities[schema["id"]] = f"type:{schema['name']}"
            for field in schema["fields"]:
                self.identities[field["id"]] = f"field:{schema['name']}/{field['key']}"

    def bindings(self, profile: Profile) -> dict:
        result: dict[str, dict] = {}
        for name, binding in profile.bindings.items():
            resource = binding.resource
            if resource in ("sites", "datasets"):
                items = self.sites if resource == "sites" else self.datasets
                matches = [
                    item
                    for item in items
                    if item["name"] == binding.name
                    and (not binding.site or item.get("site_id") == result[binding.site]["id"])
                ]
                result[name] = _single(matches, name)
            elif resource == "site_of":
                site_id = result[binding.dataset]["site_id"]
                result[name] = _single([item for item in self.sites if item["id"] == site_id], name)
            elif resource == "field":
                dataset = result[binding.dataset]
                fields = self.types[dataset["type_id"]]["fields"]
                result[name] = _single(
                    [
                        item
                        for item in fields
                        if item["name"] == binding.name and not item["archived_at"]
                    ],
                    name,
                )
            else:
                dataset, field = result[binding.dataset], result[binding.field]
                page = self.client.ok(
                    "POST",
                    f"/api/v1/datasets/{dataset['id']}/records/query",
                    {
                        "where": [{"field": field["key"], "op": "not_empty"}],
                        "sort": {"field": field["key"], "dir": "asc"},
                        "limit": 1,
                    },
                )
                if not page["items"]:
                    raise QuestionError(f"Нет записи для примера {name}")
                result[name] = {"value": page["items"][0]["doc"][field["key"]]}
        return result


def _single(items: list[dict], name: str) -> dict:
    if len(items) != 1:
        raise QuestionError(f"Привязка {name}: найдено {len(items)} объектов, нужен один")
    return items[0]


def _execute(question: Question, client: Client, bindings: dict, catalog: Catalog) -> dict:
    expected = question.expected
    assert isinstance(expected, ApiExpected)
    context = bindings.copy()
    canonical = Canonical(catalog.identities, catalog.unique)
    sources = []
    contract = json.loads(TARGET.read_text(encoding="utf-8"))
    for step in expected.steps:
        bound = Step.model_validate(render(step.model_dump(), context))
        validate_request(bound, contract)
        path = bound.path
        for key, value in bound.path_params.items():
            path = path.replace("{" + key + "}", quote(value, safe=""))
        status, reply = client.request(bound.method, path, bound.body, bound.query)
        if status != bound.status:
            raise QuestionError(
                f"{step.name}: ожидался HTTP {bound.status}, получен {status} ({reply.get('error', {}).get('code', '')})"
            )
        if bound.error_code and reply.get("error", {}).get("code") != bound.error_code:
            raise QuestionError(f"{step.name}: неверный код ожидаемого отказа")
        canonical.learn(reply)
        context[step.name] = reply
        sources.append({"request": step.model_dump(exclude_defaults=True), "status": status})
    selected = pointer(context[expected.answer.step], expected.answer.pointer)
    return {
        "status": "ok",
        "answer": canonical.normalize(selected, business=expected.answer.pointer.endswith("/doc")),
        "sources": sources,
    }


def run_suite(suite: Suite, source: Path, profiles: list[str]) -> dict[str, Any]:
    validate_suite(suite)
    if not source.is_file():
        raise QuestionError(f"Файл базы не найден: {source}")
    if not profiles or set(profiles) - set(suite.profiles):
        raise QuestionError("Неизвестный или пустой список профилей")
    selected = [question for question in suite.questions if question.profile in profiles]
    if not selected:
        raise QuestionError("В выбранных профилях нет вопросов")
    results = []
    with serve(source) as admin:
        catalog = Catalog(admin)
        contexts = {name: catalog.bindings(suite.profiles[name]) for name in profiles}
        clients: dict[tuple[str, str], Client] = {}
        for question in selected:
            bindings = contexts[question.profile]
            result = {
                "id": question.id,
                "text": render(question.text, bindings),
                "kind": question.kind,
                "role": question.role,
                "profile": question.profile,
                "origin": question.origin,
            }
            if not isinstance(question.expected, ApiExpected):
                result.update(status="manual", expected=question.expected.model_dump())
            else:
                try:
                    key = (question.profile, question.role)
                    if key not in clients:
                        clients[key] = role_client(admin, suite.roles[question.role], bindings)
                    result.update(_execute(question, clients[key], bindings, catalog))
                except (QuestionError, ValueError, KeyError, TypeError) as error:
                    result.update(status="failed", error=str(error))
            results.append(result)
    canonical_suite = json.dumps(suite.model_dump(), ensure_ascii=False, sort_keys=True)
    return {
        "version": 1,
        "suite_sha256": hashlib.sha256(canonical_suite.encode()).hexdigest(),
        "profiles": sorted(set(profiles)),
        "total_in_set": len(suite.questions),
        "excluded": len(suite.questions) - len(selected),
        "summary": {
            status: sum(item["status"] == status for item in results)
            for status in ("ok", "failed", "manual")
        },
        "results": results,
    }
