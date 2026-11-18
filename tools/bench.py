"""Стенд замеров LOCALAI через ядро и HTTP."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import platform
import secrets
import shutil
import socket
import sqlite3
import ssl
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from engine import (
    SYSTEM,
    Database,
    EngineError,
    access,
    kks,
    open_database,
    query,
    records,
    structure,
)

REGISTRY = "В эксплуатации"
PAGE_SIZE = 50
DEEP_PAGES = 20
BATCH_SIZE = 1000
WARMUP = 5
MIN_REPEATS = 30
SEARCH = "насос питательн"
FILTER = [
    {"field": "manufacturer", "op": "in", "value": ["Siemens", "ABB"]},
    {"field": "quantity", "op": "gte", "value": 10},
    {"field": "quantity", "op": "lte", "value": 25},
]
DATE_SORT = {"field": "next_inspection", "dir": "asc"}


class BenchError(RuntimeError):
    """Ошибка подготовки или выполнения замера."""


@dataclass(frozen=True)
class Metric:
    label: str
    target: tuple[str, int] | None = None
    unit: str = "мс"


METRICS = {
    "first_page": Metric("Первая страница, 50 записей", ("PERF-01", 500)),
    "deep_page": Metric("Страница после 20 страниц"),
    "kks_resolve": Metric("Точный ККС без учёта регистра", ("PERF-02", 300)),
    "kks_card": Metric("Карточка ККС"),
    "search": Metric("Поиск подстроки, два слова", ("PERF-07", 300)),
    "filter": Metric("Список и диапазон с сортировкой по дате"),
    "count": Metric("Подсчёт по фильтру"),
    "batch": Metric("Загрузка 1000 записей одной транзакцией", ("PERF-04", 5000)),
    "edit": Metric("Правка одной записи"),
    "size": Metric("Размер файла на запись", ("NFR-02", 3000), "байт/зап."),
}


@dataclass(frozen=True)
class Fixture:
    dataset_id: str
    site_id: str
    database_id: str
    kks_id: str
    kks_code: str
    rows: list[dict[str, Any]]
    fields: dict[str, str]
    database: dict[str, Any]


class EngineAccess:
    def __init__(self, db: Database) -> None:
        self.db = db

    def page(self, dataset_id: str, options: dict) -> dict:
        with self.db.read() as conn:
            return query.list_records(conn, dataset_id, options)

    def count(self, dataset_id: str, options: dict) -> int:
        with self.db.read() as conn:
            return query.count_records(conn, dataset_id, options)

    def resolve(self, site_id: str, code: str) -> dict:
        with self.db.read() as conn:
            found = kks.resolve(conn, site_id, code)
        if found is None:
            raise BenchError(f"Код ККС «{code}» отсутствует.")
        return found

    def card(self, object_id: str) -> dict:
        with self.db.read() as conn:
            return kks.card(conn, object_id)

    def edit(self, record_id: str, version: int, values: dict) -> dict:
        with self.db.write(SYSTEM) as tx:
            return records.update(tx, record_id, values, expected_version=version)


class HttpAccess:
    """Клиент API для замеров. Вход выполняется один раз до замеров: стойкий
    хэш пароля занимает около 0,1 с и не должен попадать во время операций."""

    def __init__(self, url: str, cafile: str | None = None) -> None:
        self.base = url.rstrip("/").removesuffix("/api/v1") + "/api/v1"
        handlers: list[Any] = [ProxyHandler({})]
        if cafile:  # самоподписанный сертификат сервера (SEC-04)
            handlers.append(HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))
        self.opener = build_opener(*handlers)
        self.token = ""

    def login(self, login: str, password: str) -> None:
        body = json.dumps({"login": login, "password": password}, ensure_ascii=False).encode()
        request = Request(
            self.base + "/auth/login",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                cookies = response.headers.get_all("Set-Cookie") or []
        except HTTPError as error:
            raise BenchError(f"Вход «{login}» не выполнен: {_error_message(error)}") from error
        except URLError as error:
            raise BenchError(f"Сервер {self.base} недоступен. {error.reason}") from error
        for cookie in cookies:
            name, _, value = cookie.split(";", 1)[0].partition("=")
            if name.strip() == "localai_session" and value:
                self.token = value.strip('"')
                return
        raise BenchError("Сервер не выдал сессию после входа.")

    def request(self, method: str, path: str, body: dict | None = None, key: str = "") -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as error:
            raise BenchError(
                f"{method} {path} вернул HTTP {error.code}. {_error_message(error)}"
            ) from error
        except URLError as error:
            raise BenchError(f"Сервер {self.base} недоступен. {error.reason}") from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BenchError(f"{method} {path} вернул некорректный JSON.") from error
        if not isinstance(result, dict):
            raise BenchError(f"{method} {path} вернул JSON неожиданного вида.")
        return result

    def page(self, dataset_id: str, options: dict) -> dict:
        return self.request("POST", f"/datasets/{dataset_id}/records/query", options)

    def count(self, dataset_id: str, options: dict) -> int:
        return self.request("POST", f"/datasets/{dataset_id}/records/count", options)["count"]

    def resolve(self, site_id: str, code: str) -> dict:
        return self.request("GET", f"/sites/{site_id}/kks/resolve?{urlencode({'code': code})}")

    def card(self, object_id: str) -> dict:
        return self.request("GET", f"/kks/{object_id}")

    def edit(self, record_id: str, version: int, values: dict) -> dict:
        body = {"expected_version": version, "values": values}
        return self.request("PATCH", f"/records/{record_id}", body, f"bench-{record_id}-{version}")


def _error_message(error: HTTPError) -> str:
    """Текст ошибки из ответа API, если он есть."""
    try:
        return str(json.load(error)["error"]["message"])
    except (ValueError, KeyError, TypeError, UnicodeError):
        return ""


@contextmanager
def _open(path: Path) -> Iterator[Database]:
    db = open_database(str(path))
    try:
        yield db
    finally:
        db.close()


def _snapshot(source: Path, target: Path, live: bool) -> None:
    # Неизменяемый режим уместен для закрытой базы без журналов.
    has_journal = any(
        source.with_name(source.name + suffix).exists() for suffix in ("-wal", "-journal")
    )
    mode = "mode=ro" if live or has_journal else "mode=ro&immutable=1"
    with (
        closing(sqlite3.connect(source.as_uri() + "?" + mode, uri=True)) as original,
        closing(sqlite3.connect(target)) as copy,
    ):
        original.backup(copy)


def _fixture(db: Database, source: Path, file_bytes: int) -> Fixture:
    with db.read() as conn:
        dataset = next(
            (item for item in structure.list_datasets(conn) if item["name"] == REGISTRY), None
        )
        if dataset is None:
            raise BenchError(f"В базе C-101 ожидается реестр «{REGISTRY}».")
        dataset_id = dataset["id"]
        site_id = dataset["site_id"]
        schema = structure.load_schema(conn, dataset["type_id"])
        fields = {field.key: field.kind for field in schema.active()}
        expected = {
            "manufacturer": "choice",
            "quantity": "integer",
            "next_inspection": "date",
            "kks": "kks",
            "serial": "text",
        }
        if any(fields.get(key) != kind for key, kind in expected.items()):
            raise BenchError(f"Поля реестра «{REGISTRY}» не соответствуют базе C-101.")
        database_id = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'database_id'"
        ).fetchone()[0]
        metadata = {
            "path": str(source),
            "records": conn.execute("SELECT count(*) FROM records").fetchone()[0],
            "registry": REGISTRY,
            "registry_records": query.count_records(conn, dataset_id, {"archived": "any"}),
            "active_registry_records": query.count_records(conn, dataset_id),
            "fields": len(fields),
            "links": conn.execute("SELECT count(*) FROM record_kks_links").fetchone()[0],
            "file_bytes": file_bytes,
        }
        page = query.list_records(conn, dataset_id, {"limit": PAGE_SIZE})
    rows = page["items"]
    if not rows:
        raise BenchError(f"В реестре «{REGISTRY}» нет действующих записей.")
    value = rows[0]["doc"].get("kks")
    object_id = value[0] if isinstance(value, list) and value else value
    if not isinstance(object_id, str) or object_id not in page["refs"]["kks"]:
        raise BenchError("У первой действующей записи ожидается код ККС.")
    return Fixture(
        dataset_id,
        site_id,
        database_id,
        object_id,
        page["refs"]["kks"][object_id],
        rows,
        fields,
        metadata,
    )


def _verify_http(client: HttpAccess, fixture: Fixture) -> None:
    health = client.request("GET", "/system/health")
    if health.get("database_id") != fixture.database_id:
        raise BenchError("Сервер HTTP открыт на другой базе. Проверьте --db и --http.")
    page = client.page(fixture.dataset_id, {"limit": PAGE_SIZE})
    total = client.count(fixture.dataset_id, {"archived": "any"})
    active = client.count(fixture.dataset_id, {})
    if (
        page["items"] != fixture.rows
        or total != fixture.database["registry_records"]
        or active != fixture.database["active_registry_records"]
    ):
        raise BenchError("Снимок --db расходится с данными сервера HTTP.")


@contextmanager
def _serve_copy(path: Path) -> Iterator[HttpAccess]:
    from uvicorn import Config, Server

    from server.app import create_app

    with _open(path) as db, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_app(db)
        # Во временной копии стенд заводит себе администратора со случайным
        # паролем: в базе C-101 пользователей нет, а без входа API отвечает 401.
        password = secrets.token_urlsafe(24)
        with db.write(SYSTEM) as tx:
            access.create_admin(tx, "bench", "Стенд замеров", password)
        server = Server(Config(app, log_level="warning", access_log=False))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not server.started:
                raise BenchError("Локальный сервер временной копии не запустился.")
            client = HttpAccess(f"http://127.0.0.1:{port}")
            client.login("bench", password)
            yield client
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            if worker.is_alive():
                raise BenchError("Локальный сервер временной копии не завершился.")


def _time_one(action: Callable[[], Any]) -> float:
    start = time.perf_counter_ns()
    action()
    return (time.perf_counter_ns() - start) / 1_000_000


def _sample_rounds(actions: dict[str, Callable[[], Any]], repeats: int) -> dict[str, list[float]]:
    # Чередуем операции, чтобы короткий всплеск нагрузки не пришёлся на один замер.
    for _ in range(WARMUP):
        for action in actions.values():
            action()
    values: dict[str, list[float]] = {name: [] for name in actions}
    for _ in range(repeats):
        for name, action in actions.items():
            values[name].append(_time_one(action))
    return values


def _result(name: str, values: list[float], reason: str = "") -> dict:
    metric = METRICS[name]
    requirement, limit = metric.target or (None, None)
    p95 = sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else None
    status = "не измерено"
    if p95 is not None:
        status = "—" if limit is None else ("в норме" if p95 <= limit else "не в норме")
    return {
        "id": name,
        "label": metric.label,
        "unit": metric.unit,
        "samples": len(values),
        "p50": statistics.median(values) if values else None,
        "p95": p95,
        "max": max(values) if values else None,
        "requirement": requirement,
        "limit": limit,
        "status": status,
        "reason": reason,
    }


def _read_actions(
    reader: EngineAccess | HttpAccess, fixture: Fixture
) -> dict[str, Callable[[], Any]]:
    dataset_id = fixture.dataset_id
    page = {"limit": PAGE_SIZE}
    search = page | {"search": SEARCH}
    condition = {"where": FILTER}
    filtered = page | condition | {"sort": DATE_SORT}
    code = fixture.kks_code.lower()
    cursor = None
    for _ in range(DEEP_PAGES):
        cursor = reader.page(dataset_id, page | {"cursor": cursor})["next_cursor"]
        if cursor is None:
            break
    actions: dict[str, Callable[[], Any]] = {"first_page": lambda: reader.page(dataset_id, page)}
    if cursor:
        actions["deep_page"] = lambda: reader.page(dataset_id, page | {"cursor": cursor})
    return actions | {
        "kks_resolve": lambda: reader.resolve(fixture.site_id, code),
        "kks_card": lambda: reader.card(fixture.kks_id),
        "search": lambda: reader.page(dataset_id, search),
        "filter": lambda: reader.page(dataset_id, filtered),
        "count": lambda: reader.count(dataset_id, condition),
    }


def _batch_values(fixture: Fixture) -> list[dict]:
    batch = []
    for number in range(BATCH_SIZE):
        doc = fixture.rows[number % len(fixture.rows)]["doc"]
        values = dict(doc)
        for key, value in doc.items():
            if fixture.fields[key] == "choice":
                values[key] = (
                    [{"id": item} for item in value] if isinstance(value, list) else {"id": value}
                )
        code = f"BENCH-C102-{number:04d}"
        sample = doc.get("kks")
        if isinstance(sample, list):
            values["kks"] = [f"{code}-{i}" for i in range(max(1, len(sample)))]
        else:
            values["kks"] = code
        values["serial"] = f"СН-{code}"
        batch.append(values)
    return batch


def _batch_metric(snapshot: Path, fixture: Fixture, repeats: int) -> dict:
    rows = _batch_values(fixture)

    def insert(db: Database) -> None:
        with db.write(SYSTEM) as tx:
            ids = records.create_many(tx, fixture.dataset_id, rows)
        if len(ids) != BATCH_SIZE:
            raise BenchError("Пакетная вставка вернула неожиданное число записей.")

    values = []
    for index in range(WARMUP + repeats):
        with TemporaryDirectory(prefix="batch-", dir=snapshot.parent) as folder:
            path = Path(folder) / "database.sqlite3"
            shutil.copyfile(snapshot, path)
            with _open(path) as db:
                duration = _time_one(lambda: insert(db))
            if index >= WARMUP:
                values.append(duration)
    return _result("batch", values)


def _edit_action(reader: EngineAccess | HttpAccess, fixture: Fixture) -> Callable[[], None]:
    first = fixture.rows[0]
    record_id, version = first["id"], first["version"]
    quantity = original = first["doc"]["quantity"]

    def edit() -> None:
        nonlocal version, quantity
        next_quantity = original + 1 if quantity == original else original
        row = reader.edit(record_id, version, {"quantity": next_quantity})
        if row["version"] != version + 1 or row["doc"]["quantity"] != next_quantity:
            raise BenchError("Правка не сохранила ожидаемое значение и новую версию.")
        version = row["version"]
        quantity = next_quantity

    return edit


def _memory_bytes() -> int | None:
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint32),
                ("load", ctypes.c_uint32),
                ("total_physical", ctypes.c_uint64),
                ("other_sizes", ctypes.c_uint64 * 6),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.total_physical
        return None
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


def _environment() -> dict:
    cpu = platform.processor() or platform.machine()
    if sys.platform == "linux":
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
    return {
        "os": platform.platform(),
        "cpu": cpu,
        "logical_cpus": os.process_cpu_count(),
        "memory_bytes": _memory_bytes(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
    }


@dataclass(frozen=True)
class Credentials:
    login: str
    password: str
    cafile: str | None = None


def run_bench(
    source: Path, http: str | None, repeats: int, credentials: Credentials | None = None
) -> dict:
    with TemporaryDirectory(prefix="localai-bench-") as folder:
        snapshot = Path(folder) / "snapshot.sqlite3"
        _snapshot(source, snapshot, live=http is not None)
        file_bytes = snapshot.stat().st_size
        write_copy = Path(folder) / "edit.sqlite3"
        shutil.copyfile(snapshot, write_copy)
        with _open(snapshot) as db, ExitStack() as stack:
            fixture = _fixture(db, source, file_bytes)
            reader: EngineAccess | HttpAccess
            writer: EngineAccess | HttpAccess
            if http:
                if credentials is None:
                    raise BenchError("Для --http нужны логин и пароль: --login и LOCALAI_PASSWORD.")
                reader = HttpAccess(http, credentials.cafile)
                reader.login(credentials.login, credentials.password)
                _verify_http(reader, fixture)
                writer = stack.enter_context(_serve_copy(write_copy))
            else:
                reader = EngineAccess(db)
                writer = EngineAccess(stack.enter_context(_open(write_copy)))
            actions = _read_actions(reader, fixture)
            actions["edit"] = _edit_action(writer, fixture)
            timings = _sample_rounds(actions, repeats)
        results = {name: _result(name, values) for name, values in timings.items()}
        if "deep_page" not in results:
            results["deep_page"] = _result("deep_page", [], "В реестре меньше 21 страницы.")
        if http:
            results["batch"] = _result("batch", [], "В API v1 отсутствует пакетный маршрут.")
        else:
            results["batch"] = _batch_metric(snapshot, fixture, repeats)
        results["size"] = _result("size", [file_bytes / fixture.database["records"]])
        return {
            "mode": "HTTP" if http else "engine",
            "warmup": WARMUP,
            "repeats": repeats,
            "environment": _environment(),
            "database": fixture.database,
            "measurements": [results[name] for name in METRICS],
        }


def _markdown(report: dict) -> str:
    db, env = report["database"], report["environment"]
    ram = (
        f"{env['memory_bytes'] / 2**30:.1f} ГиБ ОЗУ"
        if env["memory_bytes"]
        else "объём ОЗУ неизвестен"
    )
    lines = [
        f"## {report['mode']} · {db['records']:,} записей",
        "",
        (
            f"{env['os']}, {env['cpu']}, {env['logical_cpus']} лог. CPU, {ram}, "
            f"Python {env['python']}, SQLite {env['sqlite']}."
        ),
        (
            f"«{db['registry']}» — {db['registry_records']:,} записей, "
            f"из них {db['active_registry_records']:,} действующих. "
            f"Поля {db['fields']}, связи ККС {db['links']:,}, файл {db['file_bytes']:,} байт."
        ),
        f"Прогрев {report['warmup']}, повторов {report['repeats']}.",
        "",
        "| Операция | Ед. | P50 | P95 | Макс. | Цель | Итог |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for item in report["measurements"]:
        p50, p95, maximum = [
            f"{item[key]:.3f}" if item[key] is not None else "—" for key in ("p50", "p95", "max")
        ]
        target = f"{item['requirement']} ≤ {item['limit']}" if item["limit"] is not None else "—"
        lines.append(
            f"| {item['label']} | {item['unit']} | {p50} | {p95} | {maximum} | {target} "
            f"| {item['status']} |"
        )
    reasons = [
        f"- {item['label']}. {item['reason']}" for item in report["measurements"] if item["reason"]
    ]
    return "\n".join(lines + (["", *reasons] if reasons else []))


def _write_report(path: Path, report: dict) -> None:
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.bench", description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="файл базы C-101")
    parser.add_argument("--http", help="адрес API, например http://127.0.0.1:8730")
    parser.add_argument("--json", type=Path, default=Path("bench.json"), help="путь отчёта JSON")
    parser.add_argument("--repeats", type=int, default=MIN_REPEATS, help="число повторов")
    parser.add_argument(
        "--login",
        default=os.environ.get("LOCALAI_LOGIN"),
        help="логин для --http; по умолчанию LOCALAI_LOGIN. Пароль — LOCALAI_PASSWORD или ввод",
    )
    parser.add_argument("--cafile", help="сертификат сервера для HTTPS, если он самоподписанный")
    args = parser.parse_args(argv)
    source = args.db.resolve()
    if args.repeats < MIN_REPEATS:
        parser.error(f"нужно не менее {MIN_REPEATS} повторов")
    if not source.is_file():
        parser.error(f"файл базы «{source}» не найден")
    if args.http is not None:
        try:
            url = urlsplit(args.http)
            http_ok = (
                url.scheme in {"http", "https"} and url.hostname and not (url.query or url.fragment)
            )
        except ValueError:
            http_ok = False
        if not http_ok:
            parser.error("--http ожидает адрес сервера без параметров запроса")
        if not args.login:
            parser.error("для --http нужен --login (или LOCALAI_LOGIN): API требует входа")
    protected = [
        source,
        *(
            source.with_name(source.name + suffix)
            for suffix in (
                "-wal",
                "-shm",
                "-journal",
            )
        ),
    ]
    report_path = args.json.resolve()
    if any(
        report_path == path
        or (report_path.exists() and path.exists() and report_path.samefile(path))
        for path in protected
    ):
        parser.error("путь отчёта совпадает с файлом базы или её журналом")
    credentials = None
    if args.http is not None:
        # Пароль не принимается параметром: он остался бы в истории команд.
        password = os.environ.get("LOCALAI_PASSWORD") or getpass.getpass(f"Пароль «{args.login}»: ")
        credentials = Credentials(args.login, password, args.cafile)
    try:
        report = run_bench(source, args.http, args.repeats, credentials)
        _write_report(report_path, report)
    except (BenchError, EngineError, OSError, sqlite3.Error, ImportError) as error:
        print(f"Стенд не завершился. {error}", file=sys.stderr)
        return 1
    print(_markdown(report))
    print(f"\nJSON-отчёт сохранён в «{report_path}».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
