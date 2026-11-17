"""Общее для тестов сервера."""

from engine import passwords

# Стойкий хэш пароля занимает около 0,1 с; тестам важна логика, а не стойкость.
passwords.COST = 4

from server import backups

backups.KDF = {**backups.KDF, "memory_kib": 1024, "iterations": 1}  # ключ копии — без стойкости
