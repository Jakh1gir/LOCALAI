"""Один процесс OCR на все задачи. Авария нативного разборщика не останавливает очередь."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import InvalidDocument


def _parse(reader, source, sha, cache):
    folder = cache / reader.version / sha[:2]
    folder.mkdir(parents=True, exist_ok=True)
    kind = source.suffix if source.suffix in (".pdf", ".dxf") else ".image"
    target = folder / (sha + kind + ".json")
    try:
        with target.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (FileNotFoundError, ValueError):
        pass
    pages = reader.read(source)
    with NamedTemporaryFile(mode="w", dir=folder, delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        try:
            json.dump(pages, stream, ensure_ascii=False)
            stream.close()
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return pages


def _serve(pipe, tessdata, cache):
    try:
        from .reader import Reader

        reader = Reader(tessdata)
        pipe.send((True, None))
        while message := pipe.recv():
            try:
                pipe.send(("ok", _parse(reader, *message, cache)))
            except InvalidDocument as exc:
                pipe.send(("invalid", str(exc)))
            except Exception as exc:
                logging.getLogger(__name__).exception("Сбой при обработке документа")
                pipe.send(("retry", f"Ошибка разборщика: {exc}"))
    except (EOFError, BrokenPipeError):
        pass
    except (OSError, ImportError, ValueError, RuntimeError) as exc:
        pipe.send((False, str(exc)))
    finally:
        pipe.close()


class Parser:
    def __init__(self, tessdata: Path, cache: Path):
        self.tessdata = tessdata
        self.cache = cache
        self.process = None

    def start(self):
        if self.process and self.process.is_alive():
            return
        self.close()
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_serve, args=(child, self.tessdata, self.cache))
        self.process.start()
        child.close()
        if self.pipe.poll(90):
            ok, reason = self.pipe.recv()
            if ok:
                return
        else:
            reason = "истекло время загрузки моделей"
        self.close()
        raise RuntimeError(f"Запуск распознавания: {reason}")

    def read(self, source, sha, deadline):
        try:
            self.pipe.send((source, sha))
            if not self.pipe.poll(max(0, deadline - time.monotonic())):
                self.close()
                raise TimeoutError("Чтение не завершилось за время аренды задачи")
            state, value = self.pipe.recv()
        except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
            self.close()
            raise RuntimeError("Процесс распознавания неожиданно остановился") from exc
        if state == "invalid":
            raise InvalidDocument(value)
        if state != "ok":
            raise RuntimeError(value)
        return value

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join()
            self.process.close()
            self.pipe.close()
            self.process = None
