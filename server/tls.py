"""Сертификат HTTPS для сетевого режима (SEC-04).

Сертификат создаётся при установке командой ``python -m server.admin
create-certificate`` и подписан сам собой: в закрытой сети нет удостоверяющего
центра. Браузер рабочего места один раз спросит о доверии; сверить, что это
именно сервер LOCALAI, можно по отпечатку SHA-256, который команда печатает.
"""

from __future__ import annotations

import ipaddress
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from engine import EngineError

CERT = "server.crt"
KEY = "server.key"
LIFETIME = timedelta(days=3650)
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def files(folder: Path) -> tuple[Path, Path] | None:
    """Сертификат и ключ, если оба есть в папке."""
    cert, key = folder / CERT, folder / KEY
    return (cert, key) if cert.is_file() and key.is_file() else None


def create(folder: Path, hosts: list[str], *, replace: bool = False) -> str:
    """Ключ и самоподписанный сертификат для имён и адресов сервера; возвращает отпечаток."""
    names = [host.strip() for host in hosts if host.strip()]
    if not names:
        raise EngineError("tls_hosts", "Укажите имя или адрес сервера: --host")
    if files(folder) and not replace:
        raise EngineError("tls_exists", f"Сертификат уже есть в {folder}; заменить — с --replace")
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64])])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + LIFETIME)
        .add_extension(x509.SubjectAlternativeName([_name(host) for host in names]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    folder.mkdir(parents=True, exist_ok=True)
    key_path = folder / KEY
    # Ключ читает только владелец файла: так он не уходит с общими правами папки.
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    (folder / CERT).write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return fingerprint(folder / CERT)


def fingerprint(cert: Path) -> str:
    loaded = x509.load_pem_x509_certificate(cert.read_bytes())
    return loaded.fingerprint(hashes.SHA256()).hex(":").upper()


def server_options(host: str, folder: Path) -> dict[str, str]:
    """Параметры Uvicorn: HTTPS, если сертификат есть; без него — только этот компьютер."""
    found = files(folder)
    if found:
        return {"ssl_certfile": str(found[0]), "ssl_keyfile": str(found[1])}
    if host not in LOOPBACK:
        raise EngineError(
            "tls_required",
            f"В сети сервер работает только по HTTPS (SEC-04), а сертификата в {folder} нет."
            " Создайте его: python -m server.admin --db … create-certificate --host <имя или адрес>",
        )
    return {}


def _name(host: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)
