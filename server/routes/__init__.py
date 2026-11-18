"""Все маршруты API версии 1, по модулям областей (ARCH-04)."""

from server.routes import access, auth, kks, records, structure, system

ENDPOINTS = [
    *auth.ENDPOINTS,
    *access.ENDPOINTS,
    *structure.ENDPOINTS,
    *records.ENDPOINTS,
    *kks.ENDPOINTS,
    *system.ENDPOINTS,
]
