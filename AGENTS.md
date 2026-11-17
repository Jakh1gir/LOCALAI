# Инструкции для Codex

Ты — один из двух исполнителей проекта LOCALAI. Второй исполнитель — Claude; вы не видите диалоги друг друга, общая память — файлы репозитория.

Перед любой работой прочитай:

1. [docs/RULES.md](docs/RULES.md) — правила: расход GitHub Actions, зоны ответственности, порядок работы, правила кода.
2. [docs/STATE.md](docs/STATE.md) — что сделано, что в работе, твои задачи и открытые решения владельца.
3. Нужные разделы [docs/TZ.md](docs/TZ.md) и [docs/contract/](docs/contract/README.md).

Твои каталоги: `web/`, `vision/`, `packaging/`, `.github/`, `tests/e2e/`, `tools/`. Каталоги Claude (`engine/`, `server/`, `ai/`, `docs/contract/`, `tests/engine/`, `tests/server/`, `tests/ai/`) не правь — оформи запрос в `docs/requests/`.

Главное правило: без явной команды владельца не запускай workflow GitHub Actions и не делай push, который его запустит. Пока каталога `.github/workflows/` нет, push ничего не запускает; добавление первого workflow — отдельная задача с подтверждением владельца.

Этот файл не дублирует правила. Если правило нужно изменить — меняется `docs/RULES.md`.
