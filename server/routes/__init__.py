"""Все маршруты API версии 1, по модулям областей (ARCH-04)."""

from server.routes import (
    access,
    ai,
    auth,
    backups,
    chat,
    documents,
    graph,
    imports,
    kks,
    knowledge,
    records,
    search,
    structure,
    system,
)

ENDPOINTS = [
    *auth.ENDPOINTS,
    *access.ENDPOINTS,
    *structure.ENDPOINTS,
    *records.ENDPOINTS,
    *kks.ENDPOINTS,
    *graph.ENDPOINTS,
    *system.ENDPOINTS,
    *backups.ENDPOINTS,
    *chat.ENDPOINTS,
    *imports.ENDPOINTS,
    *search.ENDPOINTS,
    *documents.ENDPOINTS,
    *knowledge.ENDPOINTS,
    *ai.ENDPOINTS,
]
