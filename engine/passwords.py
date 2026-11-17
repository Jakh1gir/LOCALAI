"""Пароли: стойкий хэш, проверка, правила (SEC-01, SEC-03).

Хэш — scrypt из стандартной библиотеки: требование SEC-01 называет Argon2, но
для него нужен сторонний пакет, а ``engine`` зависит только от стандартной
библиотеки. scrypt, как и Argon2, требует много памяти на каждую попытку и
поэтому дорог для перебора на видеокарте. Хэш хранит свои параметры, так что
переход на другой алгоритм или более тяжёлые параметры произойдёт при
следующем входе пользователя (``needs_rehash``), без сброса паролей.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import unicodedata

from engine.errors import EngineError

# 2**15 × 8 × 128 байт = 32 МиБ памяти и около 0,1 с на проверку пароля.
COST = 15
BLOCK = 8
PARALLEL = 1
MIN_LENGTH = 12
MAX_LENGTH = 256
_ALGORITHM = "scrypt"
_TEMPORARY_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, COST, BLOCK, PARALLEL)
    return "$".join([_ALGORITHM, str(COST), str(BLOCK), str(PARALLEL), _b64(salt), _b64(digest)])


def verify(password: str, stored: str) -> bool:
    """Совпадает ли пароль с хэшем; время проверки не зависит от места расхождения."""
    try:
        algorithm, cost, block, parallel, salt, digest = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        actual = _scrypt(password, _unb64(salt), int(cost), int(block), int(parallel))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, _unb64(digest))


def needs_rehash(stored: str) -> bool:
    return stored.split("$")[:4] != [_ALGORITHM, str(COST), str(BLOCK), str(PARALLEL)]


def check_new(password: object, *, login: str = "") -> str:
    """Правила нового пароля; возвращает пароль как есть."""
    if not isinstance(password, str):
        raise EngineError("password_invalid", "Пароль: ожидается текст", field="password")
    if len(password) < MIN_LENGTH:
        raise EngineError(
            "password_too_short",
            f"Пароль короче {MIN_LENGTH} символов",
            field="password",
        )
    if len(password) > MAX_LENGTH:
        raise EngineError(
            "password_too_long", f"Пароль длиннее {MAX_LENGTH} символов", field="password"
        )
    if login and _norm(login) in _norm(password):
        raise EngineError(
            "password_contains_login", "Пароль не должен содержать логин", field="password"
        )
    return password


def temporary() -> str:
    """Временный пароль после сброса: 16 знаков без похожих букв и цифр."""
    return "".join(secrets.choice(_TEMPORARY_ALPHABET) for _ in range(16))


def burn_time() -> None:
    """Проверка пароля для несуществующего логина: ответ не выдаёт, что логина нет."""
    _scrypt("-", b"0" * 16, COST, BLOCK, PARALLEL)


def _scrypt(password: str, salt: bytes, cost: int, block: int, parallel: int) -> bytes:
    data = unicodedata.normalize("NFC", password).encode()
    memory = 128 * block * (2**cost) + 1024 * 1024
    return hashlib.scrypt(data, salt=salt, n=2**cost, r=block, p=parallel, maxmem=memory, dklen=32)


def _norm(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4))
