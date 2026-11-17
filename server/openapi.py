"""Описание API версии 1, построенное из объявлений маршрутов (API-11).

``python -m server.openapi`` записывает ``docs/contract/openapi.json``;
``--check`` сравнивает файл с кодом и завершается с ошибкой при расхождении.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import engine
from server.api import PERMISSIONS, PREFIX, Endpoint
from server.routes import ENDPOINTS

TARGET = Path(__file__).resolve().parents[1] / "docs" / "contract" / "openapi.json"
_PATH_PARAM = re.compile(r"\{(\w+)\}")
_ERROR = {
    "description": "Ошибка: код, текст на русском, поле",
    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
}


def build(endpoints: list[Endpoint] = ENDPOINTS) -> dict[str, Any]:
    schemas: dict[str, Any] = {"Error": _error_schema()}
    paths: dict[str, dict[str, Any]] = {}
    for endpoint in endpoints:
        operation: dict[str, Any] = {
            "operationId": f"{endpoint.handler.__module__.rsplit('.', 1)[-1]}_{endpoint.handler.__name__}",
            "summary": endpoint.summary,
            "tags": [endpoint.tag],
            "x-permission": endpoint.permission,
            "parameters": _parameters(endpoint),
            "responses": _responses(endpoint),
        }
        if endpoint.body is not None:
            name = endpoint.body.__name__
            schema = endpoint.body.model_json_schema(ref_template="#/components/schemas/{model}")
            schemas.update(schema.pop("$defs", {}))
            schemas[name] = schema
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}},
            }
        paths.setdefault(PREFIX + endpoint.path, {})[endpoint.method.lower()] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "LOCALAI API", "version": f"1 ({engine.__version__})"},
        "paths": paths,
        "components": {"schemas": schemas},
        "x-permissions": PERMISSIONS,
    }


def render() -> str:
    return json.dumps(build(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _parameters(endpoint: Endpoint) -> list[dict[str, Any]]:
    found = [
        {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
        for name in _PATH_PARAM.findall(endpoint.path)
    ]
    found += [
        {"name": name, "in": "query", "required": False, "schema": {"type": "string"}}
        for name in endpoint.query
    ]
    if endpoint.writes:
        found.append({
            "name": "Idempotency-Key", "in": "header", "required": False,
            "schema": {"type": "string", "maxLength": 128},
            "description": "Повтор запроса с тем же ключом возвращает прежний ответ",
        })  # fmt: skip
    return found


def _responses(endpoint: Endpoint) -> dict[str, Any]:
    done = "Выполнено" if endpoint.status != 204 else "Выполнено, без тела ответа"
    responses: dict[str, Any] = {str(endpoint.status): {"description": done}}
    for status in ("400", "404", "409"):
        responses[status] = _ERROR
    return responses


def _error_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "field": {"type": "string"},
                    "details": {"type": "object"},
                },
            }
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.openapi")
    parser.add_argument("--check", action="store_true", help="сравнить файл с кодом, не записывая")
    args = parser.parse_args(argv)
    fresh = render()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != fresh:
            print(f"{TARGET} не совпадает с кодом: выполните python -m server.openapi",
                  file=sys.stderr)  # fmt: skip
            return 1
        return 0
    TARGET.write_text(fresh, encoding="utf-8")
    print(f"Записано: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
