"""C-401: очередь, PDF, OCR, DXF и замена версии через настоящий HTTP."""

import io
import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request

import pytest

from engine import open_database
from tools.questions.workspace import Client as SetupApi
from tools.questions.workspace import serve


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    for package in ("pypdfium2", "PIL", "ezdxf", "tesserocr", "reportlab"):
        pytest.importorskip(package)
    from vision.client import Client
    from vision.parsing import Parser

    data = Path(os.getenv("TESSDATA_PREFIX", "/usr/share/tesseract-ocr/5/tessdata"))
    if not all((data / f"{name}.traineddata").is_file() for name in ("rus", "eng")):
        pytest.skip("Для C-401 нужны модели rus и eng в TESSDATA_PREFIX")
    root = tmp_path_factory.mktemp("vision")
    with closing(open_database(str(root / "seed.db"))):
        pass
    with serve(root / "seed.db") as admin:
        role = admin.ok("POST", "/api/v1/roles", {"name": "Чтение документов"})
        admin.ok(
            "PUT",
            f"/api/v1/roles/{role['id']}/permissions",
            {
                "permissions": [{"scope": "section", "target": "worker", "action": "use"}],
            },
        )
        created = admin.ok(
            "POST",
            "/api/v1/users",
            {
                "login": "reader",
                "name": "Чтение",
                "role_id": role["id"],
            },
        )
        password = secrets.token_urlsafe(24)
        user = SetupApi(admin.url)
        user.login("reader", created["temporary_password"])
        user.ok(
            "POST",
            "/api/v1/auth/password",
            {
                "current_password": created["temporary_password"],
                "new_password": password,
            },
        )
        api = Client(admin.url, "reader", password)
        parser = Parser(data, root / "cache")
        try:
            parser.start()
            yield admin, api, parser, root
        finally:
            parser.close()


def upload(admin, data, name, *, replace=None):
    path = "/api/v1/documents"
    query = {"name": name}
    if replace:
        path += "/" + replace["id"] + "/file"
        query["version"] = replace["version"]
    request = Request(
        admin.url + path + "?" + urlencode(query),
        data=data,
        method="PUT" if replace else "POST",
        headers={"Cookie": admin.token, "Content-Type": "application/octet-stream"},
    )
    with admin.opener.open(request, timeout=10) as response:
        return json.load(response)


def pdf(count, *, last="hydroturbine"):
    from reportlab.pdfgen.canvas import Canvas

    stream = io.BytesIO()
    doc = Canvas(stream)
    for i in range(count):
        doc.drawString(65, 700, last if i + 1 == count else f"Drawing index {i + 1}")
        doc.showPage()
    doc.save()
    return stream.getvalue()


def test_linked_pdf_speed_and_reuse(stack):
    from vision.__main__ import work_once

    admin, api, parser, root = stack
    payload = pdf(50)
    start = time.monotonic()
    d = upload(admin, payload, "manual.pdf")
    path = "/api/v1/documents/" + d["id"]
    site = admin.ok("POST", "/api/v1/sites", {"name": "ТЭЦ"})
    kks = admin.ok("POST", f"/api/v1/sites/{site['id']}/kks", {"code": "10LAC10AP001"})
    admin.ok("POST", path + "/links", {"kind": "kks", "id": kks["id"]})
    with ThreadPoolExecutor(1) as pool:
        reading = pool.submit(work_once, api, parser, root)
        timings = []
        while not reading.done() or not timings:
            before = time.monotonic()
            admin.ok("GET", path)
            timings.append(time.monotonic() - before)
            time.sleep(0.01)
        assert reading.result()
    assert admin.ok("GET", path)["state"] == "ready"
    hit = admin.ok("GET", "/api/v1/search?q=hydroturbine")["documents"][0]
    assert (hit["document_id"], hit["page"]) == (d["id"], "50")
    selected = admin.ok("GET", path + "/text?page=" + hit["page"])["items"]
    assert len(selected) == 1 and "hydroturbine" in selected[0]["text"]
    elapsed = time.monotonic() - start
    print(f"PERF-12: {elapsed:.3f} s; GET n={len(timings)}, max={max(timings) * 1000:.1f} ms")
    assert elapsed < 60
    (cached,) = (root / "cache").rglob("*.pdf.json")
    modified = cached.stat().st_mtime_ns
    duplicate = upload(admin, payload, "copy.pdf")
    assert work_once(api, parser, root)
    assert admin.ok("GET", "/api/v1/documents/" + duplicate["id"])["state"] == "ready"
    assert cached.stat().st_mtime_ns == modified


@pytest.mark.parametrize("fmt", ["PNG", "PDF"])
def test_scan(stack, fmt):
    from PIL import Image, ImageDraw, ImageFont

    from vision.__main__ import work_once

    admin, api, parser, root = stack
    image = Image.new("RGB", (1200, 240), "white")
    for font_name in ("DejaVuSans.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(font_name, 60)
            break
        except OSError:
            continue
    else:
        pytest.fail("Для генерации скана нужен DejaVu Sans или Arial")
    ImageDraw.Draw(image).text((60, 70), "Насос PUMP discharge pressure", font=font, fill="black")
    data = io.BytesIO()
    image.save(data, format=fmt, resolution=150)
    doc = upload(admin, data.getvalue(), "scan." + fmt.lower())
    assert work_once(api, parser, root)
    (part,) = admin.ok("GET", f"/api/v1/documents/{doc['id']}/text?page=1")["items"]
    assert "насос" in part["text"].lower() and "discharge pressure" in part["text"].lower()
    # API хранит точные дроби как десятичные строки.
    box = {key: float(value) for key, value in part["area"].items()}
    assert 0 <= box["x"] < box["x"] + box["w"] <= 1
    assert 0 <= box["y"] < box["y"] + box["h"] <= 1


def test_dxf_and_damaged_pdf(stack):
    import ezdxf

    from vision.__main__ import work_once

    admin, api, parser, root = stack
    drawing = ezdxf.new()
    block = drawing.blocks.new("panel")
    block.add_text("INSIDE PANEL")
    drawing.modelspace().add_blockref("panel", (0, 0))
    drawing.layouts.new("Panel-A").add_mtext("Cabinet 309")
    file = root / "panel.dxf"
    drawing.saveas(file)
    doc = upload(admin, file.read_bytes(), file.name)
    assert work_once(api, parser, root)
    chunks = admin.ok("GET", f"/api/v1/documents/{doc['id']}/text")["items"]
    assert {(c["page"], c["text"]) for c in chunks} == {
        ("Model", "INSIDE PANEL"),
        ("Panel-A", "Cabinet 309"),
    }
    broken = upload(admin, b"%PDF-1.4 damaged", "broken.pdf")
    assert work_once(api, parser, root)
    failed = admin.ok("GET", "/api/v1/documents/" + broken["id"])
    assert failed["state"] == "error" and "прочитать" in failed["error"]
    assert not work_once(api, parser, root)


def test_replace_during_read(stack, monkeypatch):
    from vision.__main__ import work_once

    admin, api, parser, root = stack
    doc = upload(admin, pdf(1, last="superseded"), "version.pdf")
    original = parser.read

    def replace(*args):
        pages = original(*args)
        upload(admin, pdf(1, last="replacement"), "version.pdf", replace=doc)
        return pages

    with monkeypatch.context() as m:
        m.setattr(parser, "read", replace)
        assert work_once(api, parser, root)
    assert admin.ok("GET", "/api/v1/search?q=superseded")["documents"] == []
    assert work_once(api, parser, root)
    (part,) = admin.ok("GET", f"/api/v1/documents/{doc['id']}/text")["items"]
    assert part["text"] == "replacement"


def test_running_service_picks_up_next_document(stack, tmp_path):
    import subprocess
    import sys

    admin, api, parser, _ = stack
    config = tmp_path / "worker.json"
    config.write_text(
        json.dumps(
            {
                "url": api.url,
                "login": api.login,
                "password": api.password,
                "tessdata": str(parser.tessdata),
                "cache": str(tmp_path / "cache"),
            }
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen([sys.executable, "-m", "vision", "--config", str(config)])
    try:
        for term in ("firstpass", "secondpass"):
            doc = upload(admin, pdf(1, last=term), term + ".pdf")
            deadline = time.monotonic() + 20
            while admin.ok("GET", "/api/v1/documents/" + doc["id"])["state"] != "ready":
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
            assert (
                admin.ok("GET", "/api/v1/search?q=" + term)["documents"][0]["document_id"]
                == doc["id"]
            )
    finally:
        process.terminate()
        process.wait(timeout=20)
