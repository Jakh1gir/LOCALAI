"""Книга для браузерного сценария: вымышленные строки или правка шаблона LOCALAI."""

from __future__ import annotations

import base64
import io
import json
import sys

from openpyxl import Workbook, load_workbook


def main() -> None:
    spec = json.load(sys.stdin)
    if "template" in spec:
        book = load_workbook(io.BytesIO(base64.b64decode(spec["template"])))
        page = book.active
        page[spec["cell"]] = spec["value"]
    else:
        book = Workbook()
        page = book.active
        page.title = "Данные"
        for row in spec["rows"]:
            page.append(row)
        for area in spec.get("merge", []):
            page.merge_cells(area)
    output = io.BytesIO()
    book.save(output)
    sys.stdout.buffer.write(output.getvalue())


if __name__ == "__main__":
    main()
