"""Все маршруты API версии 1, по модулям областей (ARCH-04)."""

from server.routes import access, auth, backups, graph, kks, records, structure, system

ENDPOINTS = [
    *auth.ENDPOINTS,
    *access.ENDPOINTS,
    *structure.ENDPOINTS,
    *records.ENDPOINTS,
    *kks.ENDPOINTS,
    *graph.ENDPOINTS,
    *system.ENDPOINTS,
    *backups.ENDPOINTS,
]
