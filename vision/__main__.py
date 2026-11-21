"""Постоянное чтение очереди документов LOCALAI."""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from http.client import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory

from . import InvalidDocument
from .client import ApiError, Client
from .parsing import Parser

LOG = logging.getLogger("localai.vision")


def work_once(api, parser, scratch):
    # Модель готова до захвата. Оставляем минуту из десяти на сдачу результата.
    parser.start()
    deadline = time.monotonic() + 540
    claimed = api.post("/jobs/claim", {"reader": "vision", "kinds": ["document.read"]})
    if not claimed["job"]:
        return False
    job = "/jobs/" + claimed["job"]["id"]
    document = claimed["document"]
    try:
        with TemporaryDirectory(dir=scratch) as folder:
            path = Path(folder) / ("source" + Path(document["file_name"]).suffix.lower())
            sha = api.download(document["file_url"], path, deadline)
            if sha != document["sha256"]:
                raise RuntimeError("Файл изменился после получения задачи")
            pages = parser.read(path, sha, deadline)
        result = api.post(job + "/complete", {"pages": pages})
        LOG.info("Задача %s: %s", claimed["job"]["id"], result["result"]["state"])
    except (OSError, RuntimeError, ValueError, EOFError, HTTPException) as exc:
        if isinstance(exc, ApiError) and exc.code == "job_not_held":
            return True
        retry = not isinstance(exc, (InvalidDocument, ValueError))
        if isinstance(exc, ApiError) and exc.status in (400, 413, 422):
            retry = False
        try:
            api.post(job + "/fail", {"error": f"Чтение документа: {exc}"[:2000], "retry": retry})
        except ApiError as rejected:
            if rejected.code != "job_not_held":
                raise
    return True


def run(config, stopped):
    cache = Path(config["cache"])
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    parser = Parser(Path(config["tessdata"]), cache)
    api = Client(config["url"], config["login"], config["password"], config.get("ca"))
    try:
        while not stopped.is_set():
            try:
                if work_once(api, parser, cache):
                    continue
            except (OSError, RuntimeError, EOFError, HTTPException) as exc:
                LOG.error("Очередь документов: %s", exc)
            stopped.wait(4)
    finally:
        parser.close()


def main():
    import signal

    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--config", type=Path, required=True)
    options = args.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    config = json.loads(options.config.read_text(encoding="utf-8-sig"))
    run(config, stopped)


if __name__ == "__main__":
    main()
