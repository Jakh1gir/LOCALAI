# server — сервер LOCALAI

Исполнитель: Claude. Единственный процесс, который пишет в базу: API версии 1, права, импорт и экспорт, резервные копии, аудит, очередь задач. Импортёр базы Local_DB — `server/localdb_import/`.

Кода пока нет. Контракт первых маршрутов: [docs/contract/api-v1.md](../docs/contract/api-v1.md). Задачи: [docs/STATE.md](../docs/STATE.md).
