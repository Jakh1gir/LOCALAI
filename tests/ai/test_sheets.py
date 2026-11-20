"""Граф листа схемы: приём от процесса схем, соединения, путь, ККС на листах (ТЗ 10).

VIS-04…06, VIS-09, AT-31 (через API: верные элементы и путь для подсветки).
Лист вымышленный: насос → клеммник → кабель → шкаф, отдельно — резервный насос.
"""

from __future__ import annotations

import pytest

from ai.model import Chunk

from .conftest import ask, tool, tool_results, user

SHEET = {
    "page": "1", "width": 841, "height": 594, "parser": "vision-test 1", "source": "vector",
    "elements": [
        {"id": "e1", "kind": "symbol", "label": "10LAC10AP001", "bbox": [100, 100, 140, 140]},
        {"id": "e2", "kind": "terminal", "label": "X3:12", "bbox": [200, 110, 210, 120]},
        {"id": "e3", "kind": "wire", "label": "К-101", "bbox": [210, 112, 400, 118]},
        {"id": "e4", "kind": "symbol", "label": "Шкаф 00ZZA90GH001", "bbox": [400, 90, 480, 160]},
        {"id": "e5", "kind": "symbol", "label": "ПЭН-1 резерв 10LAC20AP001",
         "bbox": [100, 300, 140, 340]},
        {"id": "e6", "kind": "label", "label": "Примечание", "bbox": [600, 500, 700, 520]},
    ],
    "edges": [
        {"from": "e1", "to": "e2", "points": [[140, 120], [200, 115]]},
        {"from": "e2", "to": "e3"}, {"from": "e3", "to": "e4", "kind": "cable"},
        {"from": "e5", "to": "e5"}, {"from": "e5", "to": "нет"},
    ],
}  # fmt: skip


@pytest.fixture
def drawing(app, admin, plant):
    worker = user(app, admin, "vision", [{"scope": "section", "target": "worker", "action": "use"}])
    document = admin.request(
        "POST", "/documents", raw=b"%PDF-1.4 fake", query={"name": "scheme.pdf"}, key=""
    ).json()
    job = worker.post("/jobs/claim", {"reader": "vision"}, key="").json()["job"]
    done = worker.post(
        f"/jobs/{job['id']}/complete",
        {"pages": [{"page": "1", "text": "Схема ПЭН-1"}], "sheets": [SHEET]}, key="",
    ).json()  # fmt: skip
    assert done["result"]["sheets"] == 6
    return document


def test_sheet_is_stored_and_kks_matched(admin, plant, drawing):
    listed = admin.get(f"/documents/{drawing['id']}/sheets").json()["items"]
    assert listed == [{"page": "1", "width": 841, "height": 594, "source": "vector",
                       "parser": "vision-test 1", "elements": 6, "edges": 3, "proposed": 2}]  # fmt: skip
    sheet = admin.get(f"/documents/{drawing['id']}/sheets/1").json()
    by_id = {item["id"]: item for item in sheet["elements"]}
    assert by_id["e1"]["kks"] == {"id": plant["pump"]["id"], "code": "10LAC10AP001",
                                  "state": "proposed", "confidence": 1.0}  # fmt: skip
    assert by_id["e5"]["kks"]["code"] == "10LAC20AP001"  # код важнее псевдонима «ПЭН-1»
    assert by_id["e6"]["kks"] is None and by_id["e1"]["bbox"] == [100, 100, 140, 140]
    assert {"from": "e1", "to": "e2", "kind": None, "points": [[140, 120], [200, 115]]} in (
        sheet["edges"]
    )
    assert admin.get(f"/documents/{drawing['id']}/sheets/9").status == 404


def test_connections_and_path_for_highlight(admin, drawing):
    base = f"/documents/{drawing['id']}/sheets/1"
    near = admin.get(base + "/connections", element="10LAC10AP001").json()
    assert near["start"] == "e1"
    assert [(item["id"], item["distance"]) for item in near["elements"]] == [("e1", 0), ("e2", 1)]
    far = admin.get(base + "/connections", element="e1", depth=3).json()
    assert [item["id"] for item in far["elements"]] == ["e1", "e2", "e3", "e4"]
    found = admin.get(base + "/path", **{"from": "10LAC10AP001", "to": "шкаф"}).json()
    assert found["found"] and [item["id"] for item in found["elements"]] == ["e1", "e2", "e3", "e4"]
    assert len(found["edges"]) == 3  # линии пути — для подсветки на листе (AT-31)
    none = admin.get(base + "/path", **{"from": "e1", "to": "e5"}).json()
    assert none["found"] is False
    assert admin.get(base + "/connections", element="e1", depth=9).status == 400
    assert admin.get(base + "/connections", element="нет такого").status == 404


def test_kks_occurrences_and_confirmation(app, admin, plant, drawing):
    pump = plant["pump"]["id"]
    places = admin.get(f"/kks/{pump}/sheets").json()["items"]
    assert [(item["document_id"], item["page"], item["element"], item["state"])
            for item in places] == [(drawing["id"], "1", "e1", "proposed")]  # fmt: skip
    assert places[0]["confidence"] == 1.0
    pumps = plant["datasets"]["Насосы"]["id"]
    reader = user(app, admin, "reader", [{"scope": "dataset", "target": pumps, "action": "view"}])
    refused = reader.post(f"/documents/{drawing['id']}/sheets/1/elements/e1/decision",
                          {"decision": "confirm"})  # fmt: skip
    assert refused.status == 403
    confirmed = admin.post(f"/documents/{drawing['id']}/sheets/1/elements/e1/decision",
                           {"decision": "confirm"}).json()  # fmt: skip
    assert confirmed["kks"]["state"] == "confirmed"
    links = admin.get(f"/documents/{drawing['id']}").json()["links"]
    assert [(item["kind"], item["id"]) for item in links] == [("kks", pump)]  # стала привязкой
    rejected = admin.post(f"/documents/{drawing['id']}/sheets/1/elements/e5/decision",
                          {"decision": "reject"}).json()  # fmt: skip
    assert rejected["kks"]["state"] == "rejected"
    assert admin.get(f"/kks/{rejected['kks']['id']}/sheets").json()["items"] == []


def test_new_version_hides_old_graph(admin, drawing):
    admin.request(
        "PUT", f"/documents/{drawing['id']}/file", raw=b"%PDF-1.4 v2",
        query={"name": "scheme.pdf", "version": 1}, key="",
    )  # fmt: skip
    assert admin.get(f"/documents/{drawing['id']}/sheets").json()["items"] == []


def test_ai_answers_what_is_connected(admin, plant, drawing, model):
    model.script = lambda messages, tools: (
        [tool("sheet_graph", code="10LAC10AP001", depth=3)] if not tool_results(messages)
        else [Chunk("Насос соединён через X3:12 и кабель К-101 со шкафом [1].")]
    )  # fmt: skip
    _, message = ask(admin, "С чем соединён насос 10LAC10AP001 на схеме и почему так?")
    assert "sheet_graph" in model.seen[0][1]
    result = tool_results(model.seen[-1][0])[0]
    sheet = result["sheets"][0]
    assert [item["id"] for item in sheet["elements"]] == ["e1", "e2", "e3", "e4"]
    assert sheet["by_scan"] is False and sheet["confirmed"] is False
    assert message["sources"][0] == {"n": 1, "kind": "document", "id": drawing["id"],
                                     "label": drawing["title"], "page": "1"}  # fmt: skip
