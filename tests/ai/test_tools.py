"""Инструменты ИИ, добавленные в K-502: итоги поля, история записи, документы объекта,
черновики «связь документа» и «сопоставление столбцов» (ТЗ 9.1, AI-04, AI-06, AI-09).
Данные вымышленные.
"""

from __future__ import annotations

import io

from openpyxl import Workbook

from ai.model import Chunk
from server.worker import run_pending

from .conftest import ask, tool, tool_results, user

# Итоги поля средствами SQL (AI-04) -------------------------------------------------------


def test_stats_endpoint_counts_in_sql(admin, plant):
    pumps = plant["datasets"]["Насосы"]["id"]
    stats = admin.post(f"/datasets/{pumps}/records/stats", {"field": "pressure"}).json()
    assert stats == {
        "field": "pressure", "count": 3, "min": 15, "max": 16, "sum": 47,
        "avg": round(47 / 3, 6), "min_id": plant["rows"]["pen2"]["id"],
        "max_id": plant["rows"]["pen1"]["id"],
    }  # fmt: skip
    working = admin.post(
        f"/datasets/{pumps}/records/stats",
        {"field": "pressure", "where": [{"field": "state", "op": "eq", "value": "Резерв"}]},
    ).json()
    assert working["count"] == 1 and working["max"] == 15
    wrong = admin.post(f"/datasets/{pumps}/records/stats", {"field": "name"})
    assert wrong.status == 400 and wrong.json()["error"]["field"] == "field"


def test_model_takes_maximum_from_aggregate(admin, plant, model):
    def script(messages, tools):
        results = tool_results(messages)
        if not results:
            return [tool("aggregate", dataset="Насосы", stat="max", field="Давление")]
        found = results[0]
        return [Chunk(f"Наибольшее давление {found['max']} [{found['ref']}] — у "
                      f"{found['record']['label']} [{found['record']['ref']}].")]  # fmt: skip

    model.script = script
    _, message = ask(admin, "У какого насоса самое высокое давление и почему?")
    assert message["text"] == "Наибольшее давление 16 [1] — у ПЭН-1 [2]."
    first, second = message["sources"]
    assert first["kind"] == "dataset" and first["stat"] == "max" and first["value"] == 16
    assert second == {"n": 2, "kind": "record", "id": plant["rows"]["pen1"]["id"],
                      "label": "ПЭН-1", "dataset_id": plant["datasets"]["Насосы"]["id"]}  # fmt: skip


# История записи и документы объекта --------------------------------------------------------


def test_record_history_and_object_documents(app, admin, plant, model):
    pen2 = plant["rows"]["pen2"]
    admin.patch(f"/records/{pen2['id']}", {"values": {"pressure": 14},
                                           "expected_version": pen2["version"]})  # fmt: skip
    document = admin.request(
        "POST", "/documents", raw="Паспорт ПЭН".encode(), query={"name": "pen.txt"}, key=""
    ).json()
    run_pending(app.state.db, app.state.files)
    admin.post(f"/documents/{document['id']}/links", {"kind": "kks", "id": plant["pump"]["id"]})

    model.script = lambda messages, tools: (
        [tool("record", id=pen2["id"], history=True), tool("kks_card", code="10LAC10AP001")]
        if not tool_results(messages) else [Chunk("Готово.")]
    )  # fmt: skip
    ask(admin, "Кто и когда менял ПЭН-2 и какие документы есть по ПЭН-1?")
    record, card = tool_results(model.seen[-1][0])
    assert record["history"][0]["changes"] == {"Давление": [15, 14]}
    assert record["history"][0]["who"] == "Тестовый администратор"
    assert [item["title"] for item in card["documents"]] == [document["title"]]


# Связь документа с ККС (propose: document_link) ------------------------------------------------


def test_ai_proposes_document_link_and_human_accepts(app, admin, plant, model):
    document = admin.request(
        "POST", "/documents", raw="Схема насоса 10LAC10AP001".encode(),
        query={"name": "scheme.txt"}, key="",
    ).json()  # fmt: skip
    model.script = lambda messages, tools: (
        [tool("propose", kind="document_link", summary="Схема — к ПЭН-1",
              document_id=document["id"], code="ПЭН-1", page="1")]
        if not tool_results(messages) else [Chunk("Предложил связь.")]
    )  # fmt: skip
    events, _ = ask(admin, "Привяжи схему к ПЭН-1")
    draft = next(item["draft"] for item in events if item["type"] == "draft")
    assert draft["kind"] == "document_link"
    assert draft["payload"] == {"document_id": document["id"], "kind": "kks",
                                "target_id": plant["pump"]["id"], "page": "1"}  # fmt: skip
    assert admin.get(f"/documents/{document['id']}").json()["links"] == []  # ещё не связан
    admin.post(f"/ai/drafts/{draft['id']}/accept")
    links = admin.get(f"/documents/{document['id']}").json()["links"]
    assert [(item["kind"], item["id"]) for item in links] == [("kks", plant["pump"]["id"])]

    pumps = plant["datasets"]["Насосы"]["id"]
    reader = user(app, admin, "reader", [
        {"scope": "section", "target": "ai", "action": "use"},
        {"scope": "dataset", "target": pumps, "action": "view"},
    ])  # fmt: skip
    events, _ = ask(reader, "Привяжи схему к ПЭН-1")
    assert all(item["type"] != "draft" for item in events)  # без раздела «Документы» — нельзя


# Сопоставление столбцов Excel (propose: column_mapping, AI-06) --------------------------------


def _book() -> bytes:
    book = Workbook()
    page = book.active
    page.title = "Насосы"
    page.append(["Наименование", "Код KKS", "Давл., кгс/см2"])
    page.append(["ПЭН-4", "10LAC40AP001", 17])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_ai_proposes_column_mapping_for_open_import(app, admin, plant, model):
    batch = admin.request(
        "POST", "/imports", raw=_book(), query={"name": "насосы.xlsx"}, key=""
    ).json()
    pumps = plant["datasets"]["Насосы"]["id"]
    mapping = {"Наименование": "Название", "Код KKS": "ККС", "Давл., кгс/см2": "Давление"}

    def script(messages, tools):
        names = [item["function"]["name"] for item in tools]
        results = tool_results(messages)
        if not results:
            assert "import_preview" in names and "propose" in names
            return [tool("import_preview", sheet="Насосы", dataset="Насосы")]
        if len(results) == 1:
            assert results[0]["headers"] == list(mapping)
            return [
                tool(
                    "propose",
                    kind="column_mapping",
                    summary="Столбцы листа «Насосы»",
                    sheet="Насосы",
                    dataset="Насосы",
                    mapping=mapping,
                    key="Название",
                )
            ]
        return [Chunk("Предложил сопоставление.")]  # fmt: skip

    model.script = script
    events, message = ask(admin, "Сопоставь столбцы этого листа с реестром насосов",
                          context={"import_id": batch["id"]})  # fmt: skip
    assert message["status"] == "done", message["text"]  # проверки внутри сценария прошли
    draft = next(item["draft"] for item in events if item["type"] == "draft")
    assert draft["payload"] == {
        "import_id": batch["id"], "sheet": "Насосы", "header_row": 1, "dataset_id": pumps,
        "mapping": {"Наименование": "name", "Код KKS": "kks", "Давл., кгс/см2": "pressure"},
        "key": "name",
    }  # fmt: skip
    accepted = admin.post(f"/ai/drafts/{draft['id']}/accept").json()
    assert accepted["state"] == "accepted" and accepted["result_id"] == batch["id"]
    payload = accepted["payload"]  # человек применяет сопоставление в мастере импорта
    planned = admin.request(
        "POST", f"/imports/{batch['id']}/plan",
        {"sheet": payload["sheet"], "header_row": payload["header_row"], "mode": "records",
         "dataset_id": payload["dataset_id"], "mapping": payload["mapping"], "key": payload["key"]},
        key="",
    )  # fmt: skip
    assert planned.status == 200, planned.json()
    assert planned.json()["summary"]["counts"]["create"] == 1

    other = user(app, admin, "petrov", [{"scope": "section", "target": "ai", "action": "use"}])
    model.script = lambda messages, tools: (
        [tool("import_preview", sheet="Насосы")] if not tool_results(messages)
        else [Chunk("Нет доступа.")]
    )  # fmt: skip
    ask(other, "Что в этом листе?", context={"import_id": batch["id"]})
    assert tool_results(model.seen[-1][0])[0]["error"] in ("not_found", "no_access")  # чужой пакет
