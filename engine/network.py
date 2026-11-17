"""Разрешённые адреса рабочих мест (SEC-04): дополнительная мера к HTTPS и входу."""

from __future__ import annotations

import ipaddress
import sqlite3
from typing import Any

from engine import text
from engine.database import Tx
from engine.errors import CONFLICT, FORBIDDEN, EngineError, not_found

MAX_NETWORKS = 500


def list_networks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM allowed_ips ORDER BY network").fetchall()
    return [dict(row) for row in rows]


def allowed(conn: sqlite3.Connection, address: str | None) -> bool:
    """Пустой список пропускает всех; этот компьютер пропускается всегда."""
    networks = [row[0] for row in conn.execute("SELECT network FROM allowed_ips")]
    if not networks:
        return True
    ip = _ip(address)
    if ip is None:
        return False
    if ip.is_loopback:
        return True
    return any(ip in ipaddress.ip_network(network) for network in networks)


def check(conn: sqlite3.Connection, address: str | None) -> None:
    if not allowed(conn, address):
        raise EngineError(
            "address_not_allowed",
            "Вход с этого компьютера не разрешён. Обратитесь к администратору",
            status=FORBIDDEN,
        )


def add_network(tx: Tx, network: str, note: str | None, requester: str | None) -> dict[str, Any]:
    canonical = _network(network)
    if tx.conn.execute("SELECT 1 FROM allowed_ips WHERE network = ?", (canonical,)).fetchone():
        raise EngineError("network_exists", f"Сеть {canonical} уже в списке", field="network")
    if tx.conn.execute("SELECT COUNT(*) FROM allowed_ips").fetchone()[0] >= MAX_NETWORKS:
        raise EngineError("network_limit", f"В списке не больше {MAX_NETWORKS} сетей")
    entry_id = text.new_id()
    shown_note = text.clean_name(note, "Примечание") if note and note.strip() else None
    tx.conn.execute(
        "INSERT INTO allowed_ips (id, network, note, created_at) VALUES (?,?,?,?)",
        (entry_id, canonical, shown_note, tx.now),
    )
    _keeps_requester(tx, requester)
    tx.audit("network.allow", "network", entry_id, details={"network": canonical})
    return dict(tx.conn.execute("SELECT * FROM allowed_ips WHERE id = ?", (entry_id,)).fetchone())


def remove_network(tx: Tx, entry_id: str, requester: str | None) -> None:
    row = tx.conn.execute("SELECT network FROM allowed_ips WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise not_found("network", entry_id)
    tx.conn.execute("DELETE FROM allowed_ips WHERE id = ?", (entry_id,))
    _keeps_requester(tx, requester)
    tx.audit("network.remove", "network", entry_id, details={"network": row[0]})


def _keeps_requester(tx: Tx, requester: str | None) -> None:
    """Администратор не может закрыть доступ компьютеру, с которого работает."""
    if not allowed(tx.conn, requester):
        raise EngineError(
            "network_lockout",
            f"После изменения ваш адрес {requester} окажется вне списка. Сначала добавьте его",
            status=CONFLICT,
            field="network",
        )


def _network(value: object) -> str:
    try:
        parsed = ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError:
        raise EngineError(
            "network_invalid",
            "Сеть: адрес вида 10.0.5.7 или сеть вида 10.0.5.0/24",
            field="network",
        ) from None
    return str(parsed)


def _ip(address: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip
