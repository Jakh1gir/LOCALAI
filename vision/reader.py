"""Текст страницы PDF, кадра изображения или листа DXF."""

from __future__ import annotations

import hashlib
import math
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

import ezdxf
import pypdfium2 as pdfium
from ezdxf.disassemble import recursive_decompose
from PIL import Image, ImageOps, ImageSequence
from tesserocr import RIL, PyTessBaseAPI, tesseract_version

from . import InvalidDocument


class Reader:
    def __init__(self, tessdata: Path):
        signature = hashlib.sha256(b"localai-vision-reader-1")
        for name in ("rus", "eng"):
            signature.update((tessdata / f"{name}.traineddata").read_bytes())
        for package in ("pypdfium2", "tesserocr", "ezdxf", "Pillow"):
            signature.update(version(package).encode())
        signature.update(tesseract_version().encode())
        self.version = signature.hexdigest()
        self.ocr = PyTessBaseAPI(path=str(tessdata), lang="rus+eng")

    def scan(self, image, page):
        image = ImageOps.exif_transpose(image).convert("RGB")
        self.ocr.SetImage(image)
        if not self.ocr.Recognize():
            raise RuntimeError("Ошибка распознавания страницы")
        text = self.ocr.GetUTF8Text().strip()
        result = {"page": page, "text": text}
        iterator = self.ocr.GetIterator()
        boxes = []
        if text and iterator:
            while True:
                box = iterator.BoundingBox(RIL.BLOCK)
                if box:
                    boxes.append(box)
                if not iterator.Next(RIL.BLOCK):
                    break
        if boxes:
            x, y = min(b[0] for b in boxes), min(b[1] for b in boxes)
            right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
            result["area"] = {
                "x": x / image.width,
                "y": y / image.height,
                "w": (right - x) / image.width,
                "h": (bottom - y) / image.height,
            }
        self.ocr.Clear()
        return result

    def pdf(self, path):
        with pdfium.PdfDocument(path) as doc:
            for i in range(len(doc)):
                number = str(i + 1)
                with closing(doc[i]) as page, closing(page.get_textpage()) as layer:
                    text = layer.get_text_bounded().strip()
                    if text:
                        yield {"page": number, "text": text}
                    else:
                        w, h = page.get_size()
                        scale = min(3.0, math.sqrt(16_000_000 / (w * h)), 12000 / max(w, h))
                        with closing(page.render(scale=scale)) as bitmap:
                            yield self.scan(bitmap.to_pil(), number)

    def pages(self, path: Path):
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            yield from self.pdf(path)
        elif suffix == ".dxf":
            drawing = ezdxf.readfile(path)
            for layout in drawing.layouts:
                labels = []
                for entity in recursive_decompose(layout):
                    if entity.dxftype() in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
                        labels.append(entity.plain_text())
                yield {"page": layout.name, "text": "\n".join(labels)}
        elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
            with Image.open(path) as source:
                for i, frame in enumerate(ImageSequence.Iterator(source), 1):
                    yield self.scan(frame, str(i))
        else:
            raise InvalidDocument("Формат документа не поддерживается")

    def read(self, path: Path):
        result = []
        total = 0
        try:
            for page in self.pages(path):
                total += len(page["text"])
                if total > 20_000_000:
                    raise InvalidDocument("Текст документа превышает 20 млн знаков")
                result.append(page)
        except (
            ezdxf.DXFError,
            pdfium.PdfiumError,
            Image.DecompressionBombError,
            OSError,
            ValueError,
            EOFError,
        ) as exc:
            raise InvalidDocument(f"Не удалось прочитать {path.suffix}: {exc}") from exc
        return result
