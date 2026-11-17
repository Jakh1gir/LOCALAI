# API версии 1 — часть этапа 1

**Состояние: действует.** Маршруты реализованы в `server/routes/`; машинное описание строится из кода и лежит в [openapi.json](openapi.json). Тест сверяет список маршрутов в этом документе с кодом в обе стороны: маршрут без описания здесь или описание без маршрута не проходит проверку. Вход, права, импорт, документы и ИИ добавляются перед своими этапами.

Интерфейс может работать на заглушке по этому документу. Язык запросов и формат записи описаны в [query-language.md](query-language.md), хранение — в [database.md](database.md).

## Общие правила

- Основа пути: `/api/v1`. Запросы и ответы — JSON в UTF-8.
- Идентификаторы — UUID. Время — строка UTC `2026-11-17T09:30:00.000Z`.
- Список без страниц отдаётся как `{"items": [...]}`; страницы записей — как описано в языке запросов.
- **Ожидаемая версия.** Изменение записи несёт `expected_version`. Если запись успели изменить, сервер отвечает `409 version_conflict` и текущей версией в `details.current_version`; интерфейс показывает отличия, а не затирает чужую правку (DATA-04).
- **Идемпотентность.** Изменяющий запрос несёт заголовок `Idempotency-Key` (до 128 символов). Повтор с тем же ключом возвращает прежний ответ и ничего не меняет; тот же ключ с другим телом — `409 idempotency_mismatch` (API-06).
- **Одно действие — одна транзакция.** При любой ошибке не остаётся ни частичных данных, ни строки аудита.
- До этапа 2 маршруты не требуют входа: все действия записываются от имени «Локальный пользователь», а сервер по умолчанию слушает только адрес `127.0.0.1`. У каждого маршрута уже объявлено право (`x-permission` в `openapi.json`); с этапа 2 маршрут без выданного права недоступен (API-02).
- Тело запроса — до 10 МиБ. Лишние поля в теле отклоняются.
- Ответ без тела — `204`. Непредвиденная ошибка — `500` с кодом `internal` без подробностей; подробности пишутся в журнал сервера.

## Ошибка

```json
{"error": {"code": "value_number", "message": "Ожидается число", "field": "f3", "details": {"row": 30}}}
```

`code` — устойчивый машинный код, `message` — текст для человека на русском, `field` — ключ поля или параметра, `details` — дополнительные данные (номер строки пакета, текущая версия). `field` и `details` могут отсутствовать (API-08).

| HTTP | Когда | Примеры кодов |
| --- | --- | --- |
| 400 | Неверные данные или запрос | `request_invalid`, `request_json`, `value_required`, `value_number`, `value_date`, `value_choice`, `field_unknown`, `field_archived`, `name_taken`, `name_empty`, `query_invalid`, `kks_archived`, `kks_unknown`, `kks_link_duplicate` |
| 404 | Объект или адрес не найден | `route_not_found`, `record_not_found`, `dataset_not_found`, `type_not_found`, `kks_not_found` |
| 409 | Конфликт с текущим состоянием | `version_conflict`, `record_archived`, `kks_code_taken`, `idempotency_mismatch` |

## Структура

| Метод и путь | Тело запроса | Ответ |
| --- | --- | --- |
| `GET /sites` | — | список площадок |
| `POST /sites` | `{name}` | площадка |
| `PATCH /sites/{id}` | `{name}` | площадка |
| `GET /types` | — | список типов без полей |
| `POST /types` | `{name, description?}` | тип |
| `GET /types/{id}` | — | тип с полями в порядке показа |
| `PATCH /types/{id}` | `{name}` | тип |
| `POST /types/{id}/archive` | — | — ; отклоняется, пока у типа есть действующие реестры |
| `POST /types/{id}/fields` | поле, см. ниже | поле |
| `PATCH /fields/{id}` | `{name?, position?, required?, searchable?, filterable?, sortable?, input_mode?}` | поле |
| `POST /fields/{id}/archive`, `/restore` | — | поле |
| `GET /datasets?site_id=&type_id=` | — | список реестров |
| `POST /datasets` | `{site_id, type_id, name}` | реестр |
| `GET /datasets/{id}` | — | реестр |
| `PATCH /datasets/{id}` | `{name}` | реестр |
| `POST /datasets/{id}/archive`, `/restore` | — | реестр |
| `GET /dictionaries` | — | список справочников |
| `POST /dictionaries` | `{name}` | справочник |
| `PATCH /dictionaries/{id}` | `{name}` | справочник |
| `GET /dictionaries/{id}/values?archived=` | — | значения справочника |
| `POST /dictionaries/{id}/values` | `{value, synonyms?}` | значение |
| `PATCH /dictionary-values/{id}` | `{value?, synonyms?, archived?}` | значение |

Поле при создании:

```json
{
  "name": "Изготовитель",
  "kind": "choice",
  "key": null,
  "multiple": false,
  "required": false,
  "searchable": true,
  "filterable": true,
  "sortable": false,
  "dictionary_id": "…",
  "input_mode": "suggest",
  "kks_role": null
}
```

`kind` — `text`, `integer`, `number`, `boolean`, `date`, `choice`, `kks`. `key` можно не задавать: сервер выдаст `f1`, `f2`, …; заданный ключ — латинские строчные буквы, цифры и `_`, до 32 символов. `dictionary_id` и `input_mode` нужны только для `choice`, `kks_role` — только для `kks`. Вид, ключ и `multiple` после создания не меняются.

Объекты структуры в ответах:

```json
{"id": "…", "name": "ТЭЦ-1", "archived_at": null, "created_at": "…", "updated_at": "…"}
```

Тип дополнительно несёт `description`, `schema_rev` и, в `GET /types/{id}`, список `fields`. Реестр несёт `site_id` и `type_id`. Интерфейс может хранить схему типа у себя и перечитывать её, когда `schema_rev` изменился.

## Записи

| Метод и путь | Тело запроса | Ответ |
| --- | --- | --- |
| `POST /datasets/{id}/records/query` | запрос на языке запросов | страница: `items`, `next_cursor`, `refs` |
| `POST /datasets/{id}/records/count` | тот же запрос без `sort`, `limit`, `cursor` | `{"count": 1234}` |
| `POST /datasets/{id}/records` | `{values}` | запись, `201` |
| `GET /records/{id}` | — | `{record, refs}` |
| `PATCH /records/{id}` | `{expected_version, values}` | запись |
| `POST /records/{id}/archive`, `/restore` | `{expected_version}` | запись |
| `GET /records/{id}/history` | — | список версий от новой к старой |
| `GET /records/{id}/versions/{n}` | — | `{doc, refs}` — документ на версии `n` |
| `POST /records/{id}/rollback` | `{expected_version, to_version}` | запись с новой версией |

`values` — объект «ключ поля → значение»:

- простые виды — значение или текст, который сервер приведёт (`"12,5"`, `"15.03.2024"`, `"да"`);
- `choice` — `{"id": "<UUID>"}` либо название или синоним значения;
- `kks` — `{"id": "<UUID>"}` либо код; неизвестный код создаёт объект ККС на площадке реестра;
- поле с несколькими значениями — список; порядок сохраняется;
- `null` или пустая строка очищают поле.

В `PATCH` передаются только изменяемые поля. Если правка ничего не меняет, версия остаётся прежней и запись возвращается как есть.

Версия в истории:

```json
{
  "version": 2,
  "op": "update",
  "changes": {"f2": ["Насос", "Насос ПЭН-1"], "f3": [500, null]},
  "actor_id": "…",
  "actor_label": "Инженер Петров",
  "origin": "user",
  "at": "2026-11-17T10:02:11.512Z",
  "restored_from": null
}
```

`op` — `create`, `update`, `archive`, `restore`, `rollback`, `import`. В `changes` пара «было, стало»; `null` — пусто. У отката `restored_from` — номер версии, значения которой возвращены.

## ККС

| Метод и путь | Тело запроса | Ответ |
| --- | --- | --- |
| `GET /sites/{id}/kks/search?q=&limit=` | — | до 50 объектов; сначала коды, начинающиеся с `q`, затем остальные совпадения и псевдонимы |
| `GET /sites/{id}/kks/resolve?code=` | — | объект и `via`: `code` или `alias`; `404`, если не найден |
| `POST /sites/{id}/kks` | `{code, name?}` | объект |
| `GET /kks/{id}` | — | карточка: объект, `aliases`, `usage` |
| `PATCH /kks/{id}` | `{code?, name?, archived?}` | объект |
| `POST /kks/{id}/aliases` | `{alias}` | псевдоним |
| `DELETE /kks-aliases/{id}` | — | — |

- Смена `code` — переименование: UUID и связи сохраняются, прежний код становится псевдонимом. Занятый код — `409 kks_code_taken` с `details.kks_id` владельца.
- `usage` в карточке — число связанных действующих записей по реестрам и ролям: `[{"dataset_id", "role", "records"}]`.
- Записи, связанные с объектом, запрашиваются обычным запросом реестра с параметром `"kks": "<UUID>"`. Запись входит в список один раз, сколько бы связей с объектом у неё ни было (DATA-07).

## Журнал и состояние

| Метод и путь | Ответ |
| --- | --- |
| `GET /audit?entity_id=&dataset_id=&before=&limit=` | события от новых к старым; `before` — `seq` последнего события предыдущей страницы |
| `GET /system/health` | `{"status": "ok", "version": "0.1.0", "schema_version": 1, "database_id": "…"}` |

Событие журнала: `{seq, at, actor_id, actor_label, origin, action, entity_kind, entity_id, dataset_id, details}`. `action` имеет вид `record.update`, `kks.rename`, `field.create`.
