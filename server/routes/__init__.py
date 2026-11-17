"""Все маршруты API версии 1, по модулям областей (ARCH-04)."""

from server.routes import kks, records, structure, system

ENDPOINTS = [*structure.ENDPOINTS, *records.ENDPOINTS, *kks.ENDPOINTS, *system.ENDPOINTS]
