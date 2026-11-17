# LOCALAI

Локальная инженерная база с каталогом ККС и встроенным ИИ. Один продукт вместо трёх: заменяет Local_DB, Core и Visual и переносит все их функции.

**Состояние: ранняя разработка.** Готово ядро хранения (`engine`); сервера, интерфейса и ИИ-службы ещё нет. Что сделано и что дальше — в [docs/STATE.md](docs/STATE.md).

## Что где

| Каталог | Содержимое | Состояние |
| --- | --- | --- |
| `engine/` | Хранение: схема базы, миграции, слой запросов, каталог ККС | работает, проверено локально |
| `server/` | Сервер: API, права, импорт и экспорт, копии, аудит | не начато |
| `ai/` | ИИ-служба | не начато |
| `vision/` | Процесс схем | не начато |
| `web/` | Интерфейс | не начато |
| `packaging/` | Установщик Windows | не начато |
| `tools/` | Генератор данных, замеры скорости | не начато |
| `tests/` | Тесты по компонентам | `tests/engine` |
| `docs/` | ТЗ, правила, состояние, контракт, реестр функций | — |

## Документы

- [docs/TZ.md](docs/TZ.md) — техническое задание версии 1.0.
- [docs/RULES.md](docs/RULES.md) — правила работы, включая расход GitHub Actions.
- [docs/STATE.md](docs/STATE.md) — состояние, задачи и открытые решения.
- [docs/contract/](docs/contract/README.md) — контракт: база, язык запросов, API.
- [docs/features.md](docs/features.md) — реестр переносимых функций.

Проект ведут два ИИ-исполнителя с разделёнными каталогами: Claude (`CLAUDE.md`) и Codex (`AGENTS.md`).

## Проверка

Нужен Python 3.13 с SQLite 3.45 или новее. У `engine` нет внешних зависимостей; для тестов нужен только `pytest`.

```shell
python -m pip install pytest
python -m pytest tests/engine
```

Проверки выполняются локально. В репозитории нет workflow GitHub Actions; push ничего не запускает.

## Пример

```python
from engine import Actor, open_database, query, records, structure

db = open_database("localai.sqlite3")
engineer = Actor(None, "Инженер")

with db.write(engineer) as tx:
    site = structure.create_site(tx, "ТЭЦ-1")
    kind = structure.create_type(tx, "Оборудование")
    kks = structure.add_field(tx, kind["id"], "ККС", "kks", required=True)
    name = structure.add_field(tx, kind["id"], "Наименование", "text", sortable=True)
    dataset = structure.create_dataset(tx, site["id"], kind["id"], "Эксплуатация")
    records.create(tx, dataset["id"], {kks["key"]: "10LAC10AP001", name["key"]: "Насос ПЭН-1"})

with db.read() as conn:
    page = query.list_records(conn, dataset["id"], {"search": "пэн"})
    print(page["items"][0]["doc"], page["refs"]["kks"])
```

## Лицензия

Лицензия не выбрана. До решения владельца все права сохраняются за ним.
