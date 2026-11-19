"""Общее для тестов ИИ: служба ИИ в том же процессе, модель по сценарию. Данные вымышленные."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from ai import agent
from ai.api import Api, Reply
from ai.model import Chunk, Model, ModelError, ToolCall
from engine import passwords
from server import ai_link
from server.app import create_app
from tests.server.asgi import Client

passwords.COST = 4  # стойкость хэша тестам не нужна

NEW_PASSWORD = "свой-пароль-пользователя"


class AsgiTransport:
    """Запросы службы ИИ к серверу без сети — в приложение напрямую."""

    def __init__(self, app: Any) -> None:
        self.client = Client(app, login=None)

    def __call__(
        self, method: str, path: str, query: dict[str, Any], body: Any, headers: dict[str, str]
    ) -> Reply:
        extra = {name.lower(): value for name, value in headers.items()}  # ASGI: строчные
        key = extra.pop("idempotency-key", "")
        reply = self.client.request(method, path, body, query=query, headers=extra, key=key)
        return Reply(reply.status, reply.json())


class Scripted(Model):
    """Модель по сценарию: функция получает сообщения и инструменты, возвращает куски ответа."""

    name = "сценарий"

    def __init__(self, script: Callable[[list[dict[str, Any]], list[dict[str, Any]]], list[Chunk]]):
        self.script = script
        self.seen: list[tuple[list[dict[str, Any]], list[str]]] = []
        self.up = True
        self.remote = False

    def available(self) -> bool:
        return self.up

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Iterator[Chunk]:
        if not self.up:
            raise ModelError("остановлена")
        names = [item["function"]["name"] for item in tools]
        self.seen.append(([dict(item) for item in messages], names))
        yield from self.script(messages, tools)


class LocalLink:
    """Связь сервера со службой ИИ в том же процессе."""

    def __init__(self, model: Model) -> None:
        self.model = model
        self.app: Any = None
        self.up = True
        self.payloads: list[dict[str, Any]] = []

    def health(self) -> dict[str, Any] | None:
        return {"service": "ai", "model": self.model.status()} if self.up else None

    def ask(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if not self.up:
            raise ai_link.AiUnavailable("остановлена")
        self.payloads.append(payload)
        api = Api(AsgiTransport(self.app), payload["token"], max_calls=payload["limits"]["calls"])
        yield from agent.answer(payload, api, self.model)


def tool(name: str, **args: Any) -> Chunk:
    return Chunk(calls=(ToolCall(name, args),))


def tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(item["content"]) for item in messages if item["role"] == "tool"]


@pytest.fixture
def model():
    return Scripted(lambda messages, tools: [Chunk("Модель отвечает.")])


@pytest.fixture
def link(model):
    return LocalLink(model)


@pytest.fixture
def app(tmp_path, link):
    created = create_app(tmp_path / "ai.sqlite3", ai=link)
    link.app = created
    return created


@pytest.fixture
def admin(app):
    return Client(app)


@pytest.fixture
def plant(admin):
    return build_plant(admin)


def build_plant(admin: Client) -> dict[str, Any]:
    """Станция: насосы со справочником состояний, кабели, сигналы со шкафами и клеммами."""
    site = admin.post("/sites", {"name": "ТЭЦ-0"}).json()
    states = admin.post("/dictionaries", {"name": "Состояние"}).json()
    for value in ("В работе", "Резерв", "Ремонт"):
        admin.post(f"/dictionaries/{states['id']}/values", {"value": value})
    pump_type = _type(admin, "Насос", [
        {"name": "Название", "kind": "text", "key": "name", "unique": True},
        {"name": "ККС", "kind": "kks", "key": "kks", "kks_role": "object"},
        {"name": "Состояние", "kind": "choice", "key": "state", "dictionary_id": states["id"]},
        {"name": "Давление", "kind": "number", "key": "pressure"},
    ])  # fmt: skip
    cable_type = _type(admin, "Кабель", [
        {"name": "Номер", "kind": "text", "key": "number", "unique": True},
        {"name": "Марка", "kind": "text", "key": "brand"},
        {"name": "Откуда", "kind": "kks", "key": "start", "kks_role": "route_start"},
    ])  # fmt: skip
    signal_type = _type(admin, "Сигнал", [
        {"name": "Код сигнала", "kind": "text", "key": "code", "unique": True},
        {"name": "ККС", "kind": "kks", "key": "kks", "kks_role": "signal"},
        {"name": "Шкаф", "kind": "text", "key": "cabinet"},
        {"name": "Клеммник", "kind": "text", "key": "block"},
        {"name": "Клеммы", "kind": "text", "key": "terminals"},
        {"name": "Кабель", "kind": "text", "key": "cable"},
    ])  # fmt: skip
    datasets = {
        name: admin.post(
            "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": name}
        ).json()
        for name, kind in (("Насосы", pump_type), ("Кабели", cable_type), ("Сигналы", signal_type))
    }
    rows: dict[str, Any] = {}
    for key, dataset, values in (
        ("pen1", "Насосы", {"name": "ПЭН-1", "kks": "10LAC10AP001", "state": "В работе", "pressure": 16}),
        ("pen2", "Насосы", {"name": "ПЭН-2", "kks": "10LAC20AP001", "state": "Резерв", "pressure": 15}),
        ("pen3", "Насосы", {"name": "ПЭН-3", "kks": "10LAC30AP001", "state": "В работе", "pressure": 16}),
        ("cable1", "Кабели", {"number": "К-101", "brand": "КВВГ 10х1,5", "start": "10LAC10AP001"}),
        ("cable2", "Кабели", {"number": "К-102", "brand": "КВВГ 4х2,5", "start": "10LAC20AP001"}),
        ("signal", "Сигналы", {"code": "10LAC10AP001XB01", "kks": "10LAC10AP001", "cabinet": "00ZZA90GH001",
                               "block": "X3", "terminals": "12, 13", "cable": "К-101"}),
    ):  # fmt: skip
        rows[key] = admin.post(
            f"/datasets/{datasets[dataset]['id']}/records", {"values": values}
        ).json()
    pump = admin.get(f"/sites/{site['id']}/kks/resolve", code="10LAC10AP001").json()
    admin.patch(f"/kks/{pump['id']}", {"name": "Питательный электронасос 1"})
    admin.post(f"/kks/{pump['id']}/aliases", {"alias": "ПЭН-1"})
    return {"site": site, "datasets": datasets, "rows": rows, "pump": pump}


def _type(admin: Client, name: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    kind = admin.post("/types", {"name": name}).json()
    for spec in fields:
        reply = admin.post(f"/types/{kind['id']}/fields", spec)
        assert reply.status == 201, reply.json()
    return kind


def user(app: Any, admin: Client, login: str, permissions: list[dict[str, Any]]) -> Client:
    role = admin.post("/roles", {"name": f"Роль {login}"}).json()
    assert admin.put(f"/roles/{role['id']}/permissions", {"permissions": permissions}).status == 200
    created = admin.post("/users", {"login": login, "name": login.title(), "role_id": role["id"]})
    client = Client(app, login=login, password=created.json()["temporary_password"])
    client.post(
        "/auth/password",
        {"current_password": created.json()["temporary_password"], "new_password": NEW_PASSWORD},
    )
    return client


def ask(
    client: Client, text: str, *, dialog: str | None = None, context: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Вопрос ИИ: события потока и сохранённый ответ (последняя строка)."""
    if dialog is None:
        dialog = client.post("/ai/dialogs", {}).json()["id"]
    body: dict[str, Any] = {"text": text}
    if context:
        body["context"] = context
    reply = client.post(f"/ai/dialogs/{dialog}/messages", body)
    assert reply.status == 200, reply.body
    events = [json.loads(line) for line in reply.body.decode().splitlines() if line.strip()]
    assert events[0]["type"] == "question" and events[-1]["type"] == "message"
    return events, events[-1]["message"]
