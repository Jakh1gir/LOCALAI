"""Журнал и состояние сервера."""

from __future__ import annotations

from typing import Any

import engine
from engine import access, audit, migrator, network
from server import schemas
from server.api import Call, Endpoint


def list_audit(call: Call) -> dict[str, Any]:
    """События журнала от новых к старым."""
    before = call.query_int("before", 0) or None
    return {
        "items": audit.list_events(
            call.conn,
            entity_id=call.query.get("entity_id"),
            dataset_id=call.query.get("dataset_id"),
            before_seq=before,
            limit=call.query_int("limit", 50),
        )
    }


def health(call: Call) -> dict[str, Any]:
    """Состояние сервера и базы."""
    row = call.conn.execute("SELECT value FROM app_meta WHERE key = 'database_id'").fetchone()
    return {
        "status": "ok",
        "version": engine.__version__,
        "schema_version": migrator.current_version(call.conn),
        "database_id": row[0] if row else None,
        # Нет ни одного пользователя: установщик ещё не создал администратора.
        "setup_required": not access.has_users(call.conn),
    }


def list_networks(call: Call) -> dict[str, Any]:
    """Разрешённые сети рабочих мест; пустой список — разрешены все."""
    return {"items": network.list_networks(call.conn)}


def allow_network(call: Call) -> dict[str, Any]:
    """Добавить адрес или сеть; свой адрес из списка выпасть не может."""
    return network.add_network(call.tx, call.body.network, call.body.note, call.client)


def remove_network(call: Call) -> None:
    """Убрать сеть из списка."""
    network.remove_network(call.tx, call.params["id"], call.client)


ENDPOINTS = [
    Endpoint(
        "GET",
        "/audit",
        list_audit,
        "section.admin",
        query=("entity_id", "dataset_id", "before", "limit"),
        tag="Журнал",
    ),
    Endpoint("GET", "/system/health", health, "public", tag="Система"),
    Endpoint("GET", "/system/allowed-ips", list_networks, "section.admin", tag="Система"),
    Endpoint(
        "POST",
        "/system/allowed-ips",
        allow_network,
        "section.admin",
        body=schemas.NetworkCreate,
        status=201,
        tag="Система",
    ),
    Endpoint(
        "DELETE",
        "/system/allowed-ips/{id}",
        remove_network,
        "section.admin",
        status=204,
        tag="Система",
    ),
]
