"""Идентификаторы, время и единые правила нормализации текста."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime, timezone

from engine.errors import EngineError

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SPACES_RE = re.compile(r"\s+")

MAX_NAME = 200
MAX_KKS = 128


def new_id() -> str:
    return str(uuid.uuid4())


def is_uuid(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def now() -> str:
    """Время UTC с миллисекундами; строки сравниваются как даты."""
    stamp = datetime.now(timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def fold(value: object) -> str:
    """Свёртка для поиска без учёта регистра; «ё» приравнена к «е»."""
    text = unicodedata.normalize("NFC", str(value)).casefold()
    return text.replace("ё", "е")


def clean_name(value: object, what: str = "Название", limit: int = MAX_NAME) -> str:
    """Название для показа: без внешних пробелов, внутренние схлопнуты."""
    if not isinstance(value, str):
        raise EngineError("name_invalid", f"{what}: ожидается текст", field="name")
    text = _SPACES_RE.sub(" ", unicodedata.normalize("NFC", value)).strip()
    if not text:
        raise EngineError("name_empty", f"{what} не может быть пустым", field="name")
    if len(text) > limit:
        raise EngineError("name_too_long", f"{what} длиннее {limit} символов", field="name")
    return text


def norm_name(value: str) -> str:
    """Ключ уникальности названия."""
    return fold(_SPACES_RE.sub(" ", value).strip())


def norm_kks(value: object) -> tuple[str, str]:
    """Код ККС: (написание для показа, нормализованный ключ).

    Убираются только внешние пробелы и различие регистра. Ведущие нули,
    внутренние разделители и различия алфавитов сохраняются.
    """
    if not isinstance(value, str):
        raise EngineError("kks_invalid", "Код ККС: ожидается текст")
    shown = unicodedata.normalize("NFC", value).strip()
    if not shown:
        raise EngineError("kks_empty", "Код ККС не может быть пустым")
    if len(shown) > MAX_KKS:
        raise EngineError("kks_too_long", f"Код ККС длиннее {MAX_KKS} символов")
    return shown, shown.upper()
