"""Формат набора, ссылки на данные и проверка запросов по контракту сервера."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from server.api import PREFIX
from server.openapi import TARGET
from server.routes import ENDPOINTS

DEFAULT_SET = Path(__file__).parent / "sets" / "starter.json"
NAME = r"^[a-z][a-z0-9_]*$"
REFERENCE = re.compile(r"\{\{([a-z][a-z0-9_]*(?:\.[a-zA-Z0-9_$]+)*)\}\}")


class QuestionError(ValueError):
    """Набор или база не позволяют построить проверяемый эталон."""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NamedBinding(Model):
    resource: Literal["sites", "datasets"]
    name: str = Field(min_length=1)
    site: str | None = None


class SiteBinding(Model):
    resource: Literal["site_of"]
    dataset: str


class FieldBinding(Model):
    resource: Literal["field"]
    dataset: str
    name: str = Field(min_length=1)


class SampleBinding(Model):
    resource: Literal["sample"]
    dataset: str
    field: str


Binding = Annotated[
    NamedBinding | SiteBinding | FieldBinding | SampleBinding, Field(discriminator="resource")
]


class Profile(Model):
    description: str = Field(min_length=1)
    bindings: dict[str, Binding]


class Role(Model):
    description: str = Field(min_length=1)
    datasets: list[str]


class Step(Model):
    name: str = Field(pattern=NAME)
    method: Literal["GET", "POST"]
    path: str
    path_params: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str | int | bool] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    status: Literal[200, 403, 404] = 200
    error_code: str | None = None


class Answer(Model):
    step: str
    pointer: str = ""


class ApiExpected(Model):
    type: Literal["api"]
    steps: list[Step] = Field(min_length=1, max_length=24)
    answer: Answer


class Source(Model):
    document: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None

    @model_validator(mode="after")
    def location(self) -> Source:
        if self.page is None and not self.sheet:
            raise ValueError("У источника нужна страница или лист")
        return self


class TextExpected(Model):
    type: Literal["text"]
    answer: str = Field(min_length=1)
    source: Source


class Question(Model):
    id: str = Field(pattern=r"^Q-[0-9]{3,}$")
    text: str = Field(min_length=1)
    kind: Literal["exact_kks", "count", "filter", "signal_code", "document", "diagram"]
    profile: str
    role: str
    origin: Literal["придуман по базе генератора", "вопрос инженера станции"]
    expected: Annotated[ApiExpected | TextExpected, Field(discriminator="type")]


class Suite(Model):
    version: Literal[1]
    profiles: dict[str, Profile]
    roles: dict[str, Role]
    questions: list[Question] = Field(min_length=1)


def render(value: Any, context: dict[str, Any]) -> Any:
    """Подстановка ссылок без eval; полная ссылка сохраняет тип значения."""
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context) for item in value]
    if not isinstance(value, str):
        return value

    def lookup(match: re.Match[str]) -> Any:
        current: Any = context
        try:
            for part in match[1].split("."):
                current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise QuestionError(f"Не найдена ссылка {match[0]}") from None
        return current

    exact = REFERENCE.fullmatch(value)
    return lookup(exact) if exact else REFERENCE.sub(lambda match: str(lookup(match)), value)


def pointer(value: Any, path: str) -> Any:
    """JSON Pointer RFC 6901, включая ключи с / и ~."""
    if path and not path.startswith("/"):
        raise QuestionError("Указатель ответа должен начинаться с /")
    for part in path.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise QuestionError(f"Ответ не содержит {path}") from None
    return value


def _condition(item: Any, nested: bool = False) -> None:
    if not isinstance(item, dict):
        raise QuestionError("Условие where должно быть объектом")
    if "any" in item:
        if nested or set(item) != {"any"} or not isinstance(item["any"], list):
            raise QuestionError("Группа any содержит только список простых условий")
        if not 1 <= len(item["any"]) <= 30:
            raise QuestionError("В any нужно от 1 до 30 условий")
        for child in item["any"]:
            _condition(child, True)
        return
    op = item.get("op")
    if not isinstance(op, str):
        raise QuestionError("Операция условия должна быть строкой")
    unary = op in ("empty", "not_empty")
    if op not in {
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "contains",
        "starts",
        "empty",
        "not_empty",
    }:
        raise QuestionError(f"Неизвестная операция условия: {op}")
    if set(item) != ({"field", "op"} if unary else {"field", "op", "value"}):
        raise QuestionError("У условия нужны field, op и value, кроме empty/not_empty")
    if not isinstance(item["field"], str) or not item["field"].strip():
        raise QuestionError("У условия нужен ключ поля")
    if op == "in" and (not isinstance(item["value"], list) or not 1 <= len(item["value"]) <= 500):
        raise QuestionError("Операция in принимает от 1 до 500 значений")


def validate_request(step: Step, contract: dict) -> None:
    operation = contract["paths"].get(step.path, {}).get(step.method.lower())
    endpoint = next(
        (
            item
            for item in ENDPOINTS
            if PREFIX + item.path == step.path and item.method == step.method
        ),
        None,
    )
    if operation is None or endpoint is None:
        raise QuestionError(f"Нет маршрута в контракте: {step.method} {step.path}")
    # Эталон строится только чтением инженерных данных; входом управляет сам стенд.
    if endpoint.writes or step.path.startswith(
        (PREFIX + "/auth", PREFIX + "/backups", PREFIX + "/system", PREFIX + "/ai")
    ):
        raise QuestionError(f"Этот маршрут нельзя использовать для эталона: {step.path}")
    params = operation.get("parameters", [])
    names = {item["name"] for item in params if item["in"] == "path"}
    allowed = {item["name"] for item in params if item["in"] == "query"}
    if set(step.path_params) != names or set(step.query) - allowed:
        raise QuestionError(f"Параметры не соответствуют контракту: {step.path}")
    if any(not value.strip() for value in step.path_params.values()):
        raise QuestionError("Параметр пути не может быть пустым")
    if (step.body is None) != (endpoint.body is None):
        raise QuestionError(f"Неверное наличие тела запроса: {step.path}")
    if endpoint.body is not None:
        endpoint.body.model_validate(step.body)
    if step.status >= 400 and not step.error_code:
        raise QuestionError("У ожидаемого отказа нужен error_code")
    if step.status == 200 and step.error_code:
        raise QuestionError("У успешного запроса не может быть error_code")
    body = step.body or {}
    where = body.get("where")
    if where is not None:
        if not isinstance(where, list) or len(where) > 30:
            raise QuestionError("В where допускается до 30 условий")
        for condition in where:
            _condition(condition)
    if "limit" in body and body["limit"] is not None and not 1 <= body["limit"] <= 200:
        raise QuestionError("Размер страницы — от 1 до 200")
    sort = body.get("sort")
    if sort is not None and (
        not isinstance(sort, dict)
        or set(sort) != {"field", "dir"}
        or sort["dir"] not in ("asc", "desc")
    ):
        raise QuestionError("sort содержит одно поле field и направление asc/desc")
    if step.path.endswith("/resolve") and not str(step.query.get("code", "")).strip():
        raise QuestionError("Для resolve нужен code")
    if (
        "depth" in step.query
        and not REFERENCE.fullmatch(str(step.query["depth"]))
        and str(step.query["depth"]) not in {"1", "2", "3", "4"}
    ):
        raise QuestionError("Глубина графа — от 1 до 4")


def _references(value: Any, available: set[str]) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _references(child, available)
    elif isinstance(value, list):
        for child in value:
            _references(child, available)
    elif isinstance(value, str):
        for match in REFERENCE.finditer(value):
            if match[1].split(".")[0] not in available:
                raise QuestionError(f"Ссылка на неизвестное или следующее значение: {match[0]}")
        if "{{" in REFERENCE.sub("", value) or "}}" in REFERENCE.sub("", value):
            raise QuestionError("Некорректная ссылка {{имя.поле}}")


def validate_suite(suite: Suite) -> None:
    contract = json.loads(TARGET.read_text(encoding="utf-8"))
    seen: set[int] = set()
    for profile in suite.profiles.values():
        previous: set[str] = set()
        for name, binding in profile.bindings.items():
            if not re.fullmatch(NAME, name):
                raise QuestionError(f"Некорректное имя привязки: {name}")
            for key in ("dataset", "site", "field"):
                value = getattr(binding, key, None)
                if value and value not in previous:
                    raise QuestionError(f"{name}: сначала объявите привязку {value}")
                if value:
                    kinds = {
                        "dataset": {"datasets"},
                        "site": {"sites", "site_of"},
                        "field": {"field"},
                    }
                    if profile.bindings[value].resource not in kinds[key]:
                        raise QuestionError(f"{name}: неверный вид привязки {value}")
            previous.add(name)
    for question in suite.questions:
        number = int(question.id.removeprefix("Q-"))
        if number in seen:
            raise QuestionError(f"Повтор номера {question.id}")
        seen.add(number)
        if question.profile not in suite.profiles or question.role not in suite.roles:
            raise QuestionError(f"{question.id}: неизвестный профиль или роль")
        available = set(suite.profiles[question.profile].bindings)
        role = suite.roles[question.role]
        if set(role.datasets) - (available | {"*"}):
            raise QuestionError(f"{question.id}: реестры роли не объявлены в профиле")
        if any(
            name != "*" and suite.profiles[question.profile].bindings[name].resource != "datasets"
            for name in role.datasets
        ):
            raise QuestionError(f"{question.id}: право чтения выдаётся только реестру")
        _references(question.text, available)
        expected = question.expected
        if isinstance(expected, TextExpected):
            if question.kind not in ("document", "diagram"):
                raise QuestionError(f"{question.id}: текстовый эталон нужен документу или схеме")
            continue
        names: set[str] = set()
        for step in expected.steps:
            if step.name in available:
                raise QuestionError(f"{question.id}: имя шага уже занято: {step.name}")
            _references(step.model_dump(), available)
            validate_request(step, contract)
            available.add(step.name)
            names.add(step.name)
        if expected.answer.step not in names:
            raise QuestionError(f"{question.id}: неизвестный шаг ответа")
        if expected.answer.pointer and not expected.answer.pointer.startswith("/"):
            raise QuestionError(f"{question.id}: нужен JSON Pointer ответа")


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QuestionError(f"Повтор ключа JSON: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise QuestionError(f"JSON не поддерживает число {value}")


def load_suite(path: Path = DEFAULT_SET) -> Suite:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique, parse_constant=_constant
        )
        suite = Suite.model_validate(raw)
        validate_suite(suite)
        return suite
    except (OSError, UnicodeError, ValueError, ValidationError) as error:
        raise QuestionError(f"Набор {path.name}: {error}") from error
