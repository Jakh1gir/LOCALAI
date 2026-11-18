"""Документы: загрузка, чтение, фрагменты, поиск, упоминания ККС, очередь (ТЗ 8, KNOW-01…04, VIS-01, VIS-02).

Данные вымышленные.
"""

from __future__ import annotations

import io
import time
import zipfile

import pytest
from openpyxl import Workbook

from server.app import create_app
from server.worker import run_pending

from .asgi import Client

NEW_PASSWORD = "свой-пароль-пользователя"
MANUAL = (
    "Инструкция по насосу 10LAC10AP001.\n\nПеред пуском проверьте давление на сигнале"
    " 10LAC10AP001XQ01.\n\fСтраница два: задвижка 10LAB20AA101 и снова 10LAC10AP001."
)


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "docs.sqlite3")


@pytest.fixture
def admin(app):
    return Client(app)


@pytest.fixture
def plant(admin):
    site = admin.post("/sites", {"name": "ТЭЦ"}).json()
    pump = admin.post(f"/sites/{site['id']}/kks", {"code": "10LAC10AP001"}).json()
    kind = admin.post("/types", {"name": "Насос"}).json()
    admin.post(f"/types/{kind['id']}/fields", {"name": "Название", "kind": "text", "key": "name"})
    datasets = {
        name: admin.post(
            "/datasets", {"site_id": site["id"], "type_id": kind["id"], "name": name}
        ).json()
        for name in ("Насосы", "Закрытые")
    }
    records = {
        name: admin.post(f"/datasets/{item['id']}/records", {"values": {"name": name}}).json()
        for name, item in datasets.items()
    }
    return {"site": site, "pump": pump, "datasets": datasets, "records": records}


def _upload(client, data, name, **query):
    return client.request("POST", "/documents", raw=data, query={"name": name, **query}, key="")


def _read(app):
    return run_pending(app.state.db, app.state.files)


def _user(app, admin, login, permissions):
    role = admin.post("/roles", {"name": f"Роль {login}"}).json()
    assert admin.put(f"/roles/{role['id']}/permissions", {"permissions": permissions}).status == 200
    created = admin.post("/users", {"login": login, "name": login.title(), "role_id": role["id"]})
    client = Client(app, login=login, password=created.json()["temporary_password"])
    client.post(
        "/auth/password",
        {"current_password": created.json()["temporary_password"], "new_password": NEW_PASSWORD},
    )
    return client


def _docx(paragraphs):
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>' for text in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w='
        '"http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _xlsx():
    book = Workbook()
    book.active.title = "Перечень"
    book.active.append(["ККС", "Назначение"])
    book.active.append(["10LAC10AP001", "питательный насос"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# Чтение сервером и поиск (KNOW-01, KNOW-02) -------------------------------------------


def test_text_document_is_read_split_and_found_by_page(app, admin, plant):
    made = _upload(admin, MANUAL.encode(), "инструкция.txt", title="Инструкция ПЭН")
    assert made.status == 201, made.json()
    document = made.json()
    assert document["state"] == "queued" and document["job"]["state"] == "queued"
    assert document["reader"] == "server" and document["type"]["name"] == "Документ"
    assert _read(app) == 1
    ready = admin.get(f"/documents/{document['id']}").json()
    assert ready["state"] == "ready" and ready["pages"] == 2 and ready["job"] is None
    second = admin.get(f"/documents/{document['id']}/text", page="2").json()["items"]
    assert len(second) == 1 and second[0]["text"].startswith("Страница два")
    found = admin.get("/search", q="давление пуском").json()["documents"]
    assert found == [
        {
            "document_id": document["id"],
            "title": "Инструкция ПЭН",
            "type": "Документ",
            "page": "1",
            "snippet": found[0]["snippet"],
        }
    ]
    assert "пуском" in found[0]["snippet"]


@pytest.mark.parametrize(
    ("name", "data", "pages", "needle"),
    [
        ("паспорт.docx", _docx(["Паспорт насоса", "Подача 250 м3/ч"]), ["1"], "подача"),
        ("перечень.xlsx", _xlsx(), ["Перечень"], "питательный"),
        ("заметки.md", "# Заметка\n\nВибрация в норме".encode("cp1251"), ["1"], "вибрация"),
    ],
    ids=["docx", "xlsx", "md-cp1251"],
)
def test_office_and_plain_formats_are_read(app, admin, plant, name, data, pages, needle):
    document = _upload(admin, data, name).json()
    _read(app)
    ready = admin.get(f"/documents/{document['id']}").json()
    assert ready["state"] == "ready", ready
    texts = admin.get(f"/documents/{document['id']}/text").json()["items"]
    assert [item["page"] for item in texts] == pages
    assert admin.get("/search", q=needle).json()["documents"][0]["document_id"] == document["id"]


def test_same_file_is_stored_once_and_unknown_format_refused(app, admin, plant):
    first = _upload(admin, b"one text", "a.txt").json()
    second = _upload(admin, b"one text", "b.txt").json()
    assert first["file"]["sha256"] == second["file"]["sha256"]
    stored = [path for path in app.state.files.folder.rglob("*") if path.is_file()]
    assert len(stored) == 1  # DATA-09
    refused = _upload(admin, b"MZ", "setup.exe")
    assert refused.status == 400 and refused.json()["error"]["code"] == "document_format"


def test_empty_text_gives_empty_state(app, admin, plant):
    document = _upload(admin, b"   \n\n  ", "pusto.txt").json()
    _read(app)
    assert admin.get(f"/documents/{document['id']}").json()["state"] == "empty"


# Упоминания ККС и привязки (KNOW-03) -------------------------------------------------


def test_kks_mentions_become_links_only_after_confirmation(app, admin, plant):
    document = _upload(admin, MANUAL.encode(), "инструкция.txt").json()
    _read(app)
    found = admin.get(f"/documents/{document['id']}/mentions").json()["items"]
    assert [(item["code"], item["pages"], item["hits"], item["state"]) for item in found] == [
        ("10LAC10AP001", ["1", "2"], 3, "proposed")  # код сигнала тоже ведёт к аппарату
    ]  # 10LAB20AA101 нет в каталоге — не предлагается
    assert admin.get(f"/documents/{document['id']}").json()["links"] == []
    decided = admin.post(
        f"/documents/{document['id']}/mentions/{plant['pump']['id']}", {"decision": "confirm"}
    ).json()
    assert decided["items"][0]["state"] == "confirmed"
    links = admin.get(f"/documents/{document['id']}").json()["links"]
    assert links == [
        {"kind": "kks", "id": plant["pump"]["id"], "code": "10LAC10AP001", "access": True}
    ]
    by_kks = admin.get("/documents", target_kind="kks", target_id=plant["pump"]["id"]).json()
    assert [item["id"] for item in by_kks["items"]] == [document["id"]]


def test_record_links_follow_dataset_rights(app, admin, plant):
    document = _upload(admin, b"text", "a.txt").json()
    for record in plant["records"].values():
        linked = admin.post(
            f"/documents/{document['id']}/links", {"kind": "record", "id": record["id"]}
        )
        assert linked.status == 200, linked.json()
    reader = _user(
        app,
        admin,
        "reader",
        [
            {"scope": "dataset", "target": plant["datasets"]["Насосы"]["id"], "action": "view"},
            {"scope": "doc_type", "target": "*", "action": "view"},
        ],
    )
    links = reader.get(f"/documents/{document['id']}").json()["links"]
    assert [(item["dataset"] if item["access"] else None) for item in links] == ["Насосы", None]
    refused = reader.post(
        f"/documents/{document['id']}/links", {"kind": "kks", "id": plant["pump"]["id"]}
    )
    assert refused.status == 403  # без раздела «Документы»
    removed = admin.delete(
        f"/documents/{document['id']}/links/record/{plant['records']['Насосы']['id']}"
    ).json()
    assert len(removed["links"]) == 1


# Замена и архив (KNOW-04) ---------------------------------------------------------


def test_replacement_reindexes_and_archive_removes_from_search(app, admin, plant):
    document = _upload(admin, "старый текст про клапан".encode(), "a.txt").json()
    _read(app)
    same = admin.request(
        "PUT", f"/documents/{document['id']}/file", raw="старый текст про клапан".encode(),
        query={"name": "a.txt", "version": 1}, key="",
    ).json()  # fmt: skip
    assert same["version"] == 1 and same["state"] == "ready"
    new = admin.request(
        "PUT", f"/documents/{document['id']}/file", raw="новый текст про задвижку".encode(),
        query={"name": "a.txt", "version": 1}, key="",
    ).json()  # fmt: skip
    assert new["version"] == 2 and new["state"] == "queued"
    assert admin.get("/search", q="клапан").json()["documents"] == []  # прежний текст ушёл сразу
    stale = admin.request(
        "PUT",
        f"/documents/{document['id']}/file",
        raw=b"x",
        query={"name": "a.txt", "version": 1},
        key="",
    )
    assert stale.status == 409 and stale.json()["error"]["code"] == "version_conflict"
    _read(app)
    assert len(admin.get("/search", q="задвижку").json()["documents"]) == 1
    admin.post(f"/documents/{document['id']}/archive")
    assert admin.get("/search", q="задвижку").json()["documents"] == []
    archived = admin.get("/documents", archived="true").json()["items"]
    assert [item["id"] for item in archived] == [document["id"]]


# Права на типы документов ----------------------------------------------------------


def test_document_types_limit_who_sees_documents(app, admin, plant):
    types = {item["name"]: item["id"] for item in admin.get("/document-types").json()["items"]}
    scheme = _upload(admin, "схема насосной".encode(), "s.txt", type_id=types["Схема"]).json()
    _upload(admin, "паспорт насосной".encode(), "p.txt").json()
    _read(app)
    limited = _user(
        app,
        admin,
        "limited",
        [{"scope": "doc_type", "target": types["Документ"], "action": "view"}],
    )
    assert [item["title"] for item in limited.get("/documents").json()["items"]] == ["p"]
    assert limited.get(f"/documents/{scheme['id']}").status == 404
    assert limited.get(f"/documents/{scheme['id']}/file").status == 404
    titles = {item["title"] for item in limited.get("/search", q="насосной").json()["documents"]}
    assert titles == {"p"}
    assert [item["name"] for item in limited.get("/document-types").json()["items"]] == ["Документ"]
    assert _upload(limited, b"x", "x.txt").status == 403


# Процесс схем через очередь (VIS-01, VIS-02) ----------------------------------------------


def test_vision_reader_claims_reads_and_completes(app, admin, plant):
    worker = _user(
        app, admin, "vision", [{"scope": "section", "target": "worker", "action": "use"}]
    )
    document = _upload(admin, b"%PDF-1.4 fake", "chertezh.pdf", title="Схема ПЭН").json()
    assert document["reader"] == "vision"
    assert _read(app) == 0  # сервер PDF не берёт
    assert admin.post("/jobs/claim", {}, key="").status == 403  # у администратора нет «worker»
    claimed = worker.post("/jobs/claim", {"reader": "vision"}, key="").json()
    assert claimed["document"]["id"] == document["id"]
    assert admin.get(f"/documents/{document['id']}").json()["state"] == "reading"
    file = worker.get(claimed["document"]["file_url"].removeprefix("/api/v1"))
    assert file.status == 200 and file.body == b"%PDF-1.4 fake"
    assert worker.post("/jobs/claim", {}, key="").json() == {"job": None}  # очередь пуста
    pages = [
        {"page": "1", "text": "Насос 10LAC10AP001 подключён к шкафу"},
        {"page": "2", "text": "Лист 2"},
    ]
    done = worker.post(f"/jobs/{claimed['job']['id']}/complete", {"pages": pages}, key="").json()
    assert done["job"]["state"] == "done" and done["result"]["chunks"] == 2
    ready = admin.get(f"/documents/{document['id']}").json()
    assert ready["state"] == "ready" and ready["reader"] == "vision"
    assert admin.get("/search", q="подключён").json()["documents"][0]["page"] == "1"
    again = worker.post(f"/jobs/{claimed['job']['id']}/complete", {"pages": pages}, key="")
    assert again.status == 409 and again.json()["error"]["code"] == "job_not_held"


def test_failures_retry_then_mark_document_and_stale_results_are_dropped(app, admin, plant):
    worker = _user(
        app, admin, "vision", [{"scope": "section", "target": "worker", "action": "use"}]
    )
    document = _upload(admin, b"%PDF-1.4 a", "a.pdf").json()
    for attempt in range(3):
        job = worker.post("/jobs/claim", {}, key="").json()["job"]
        assert job["attempts"] == attempt + 1
        failed = worker.post(f"/jobs/{job['id']}/fail", {"error": "не открылся"}, key="").json()
    assert failed["job"]["state"] == "error"
    broken = admin.get(f"/documents/{document['id']}").json()
    assert broken["state"] == "error" and broken["error"] == "не открылся"
    assert [item["state"] for item in admin.get("/jobs").json()["items"]] == ["error"]

    other = _upload(admin, b"%PDF-1.4 b", "b.pdf").json()
    job = worker.post("/jobs/claim", {}, key="").json()["job"]
    admin.request(
        "PUT",
        f"/documents/{other['id']}/file",
        raw=b"%PDF-1.4 c",
        query={"name": "b.pdf", "version": 1},
        key="",
    )
    late = worker.post(f"/jobs/{job['id']}/complete", {"pages": [{"text": "старое"}]}, key="")
    assert late.status == 409  # задача отменена заменой файла
    fresh = worker.post("/jobs/claim", {}, key="").json()
    assert fresh["document"]["version"] == 2


def test_background_thread_reads_without_being_asked(app, admin, plant):
    app.state.worker.start()
    try:
        document = _upload(admin, "фоновое чтение".encode(), "bg.txt").json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if admin.get(f"/documents/{document['id']}").json()["state"] == "ready":
                break
            time.sleep(0.05)
        assert admin.get(f"/documents/{document['id']}").json()["state"] == "ready"
    finally:
        app.state.worker.stop()


def test_reading_progress_reaches_users_as_events(app, admin, plant):
    types = {item["name"]: item["id"] for item in admin.get("/document-types").json()["items"]}
    reader = _user(
        app, admin, "reader", [{"scope": "doc_type", "target": types["Документ"], "action": "view"}]
    )
    start = reader.get("/events").json()["cursor"]
    seen = _upload(admin, "текст".encode(), "a.txt").json()
    _upload(admin, "схема".encode(), "s.txt", type_id=types["Схема"])
    _read(app)
    found = reader.get("/events", after=start).json()["events"]
    assert {(item["action"], item["id"]) for item in found} == {
        ("document.create", seen["id"]),
        ("document.read", seen["id"]),
    }  # события схемы роли не видны
