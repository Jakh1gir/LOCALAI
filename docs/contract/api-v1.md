# API версии 1 — этапы 1 и 2

**Состояние: действует.** Маршруты реализованы в `server/routes/`; машинное описание строится из кода и лежит в [openapi.json](openapi.json). Тест сверяет список маршрутов в этом документе с кодом в обе стороны: маршрут без описания здесь или описание без маршрута не проходит проверку. Импорт, документы и ИИ добавляются перед своими этапами.

Интерфейс может работать на заглушке по этому документу. Язык запросов и формат записи описаны в [query-language.md](query-language.md), хранение — в [database.md](database.md).

## Общие правила

- Основа пути: `/api/v1`. Запросы и ответы — JSON в UTF-8.
- Идентификаторы — UUID. Время — строка UTC `2026-11-17T09:30:00.000Z`.
- Список без страниц отдаётся как `{"items": [...]}`; страницы записей — как описано в языке запросов.
- **Ожидаемая версия.** Изменение записи несёт `expected_version`. Если запись успели изменить, сервер отвечает `409 version_conflict` и текущей версией в `details.current_version`; интерфейс показывает отличия, а не затирает чужую правку (DATA-04).
- **Идемпотентность.** Каждый изменяющий запрос обязан нести заголовок `Idempotency-Key` (до 128 символов); без него — `400 idempotency_key_required`. Повтор с тем же ключом возвращает прежний ответ и ничего не меняет; тот же ключ с другим телом — `409 idempotency_mismatch` (API-06). Чтение (`GET`, а также `POST …/records/query`, `…/records/count` и `…/records/values`) ключа не требует. Вход, выход, смена своего пароля и действия с резервными копиями ключа не требуют и не повторяются по нему: в отпечатке запроса оказался бы пароль, а результат копии — файл, а не строки базы. Ключ принадлежит пользователю: тот же ключ у другого пользователя — отдельный запрос.
- **Одно действие — одна транзакция.** При любой ошибке не остаётся ни частичных данных, ни строки аудита.
- **Вход обязателен.** Кроме `POST /auth/login` и `GET /system/health`, каждый маршрут требует сессии и права, объявленного у маршрута (`x-permission` в `openapi.json`); маршрут без права не собирается (API-02). Права и правила — в разделе «Вход и права».
- Тело запроса — до 10 МиБ; больше — `413 request_too_large`. Лишние поля в теле отклоняются.
- Ответ без тела — `204`. Непредвиденная ошибка — `500` с кодом `internal` без подробностей; подробности пишутся в журнал сервера.

## Ошибка

```json
{"error": {"code": "value_number", "message": "Ожидается число", "field": "f3", "details": {"row": 30}}}
```

`code` — устойчивый машинный код, `message` — текст для человека на русском, `field` — ключ поля или параметра, `details` — дополнительные данные (номер строки пакета, текущая версия). `field` и `details` могут отсутствовать (API-08).

| HTTP | Когда | Примеры кодов |
| --- | --- | --- |
| 400 | Неверные данные или запрос | `backup_password`, `backup_corrupt`, `backup_format`, `schema_newer`, `network_invalid`, `password_wrong`, `password_too_short`, `password_same`, `login_invalid`, `login_taken`, `permission_invalid`, `request_invalid`, `request_json`, `idempotency_key_required`, `value_required`, `value_number`, `value_precision`, `value_out_of_range`, `value_date`, `value_choice`, `value_choice_archived`, `field_unknown`, `field_archived`, `field_change`, `name_taken`, `name_empty`, `query_invalid`, `kks_archived`, `kks_unknown`, `kks_too_long`, `rollback_invalid`, `dataset_parent_archived` |
| 404 | Объект или адрес не найден | `backup_not_found`, `route_not_found`, `record_not_found`, `dataset_not_found`, `type_not_found`, `kks_not_found`, `version_missing` |
| 401 | Нет входа, сессия закрыта или истекла; неверный логин или пароль | `auth_required`, `session_invalid`, `session_expired`, `login_failed` |
| 403 | Недостаточно прав; учётная запись заблокирована; нужно сменить временный пароль; адрес не в списке разрешённых | `forbidden` (`details.permission`), `field_forbidden` (`field`), `user_blocked`, `password_change_required`, `address_not_allowed` |
| 409 | Конфликт с текущим состоянием | `version_conflict`, `record_archived`, `kks_code_taken`, `idempotency_mismatch`, `last_admin`, `network_lockout` |
| 413 | Тело запроса больше 10 МиБ | `request_too_large` |
| 429 | Слишком много неудачных входов | `login_locked` (`details.locked_until`), `login_throttled` |

## Вход и права

### Сессия

| Метод и путь | Тело запроса | Ответ |
| --- | --- | --- |
| `POST /auth/login` | `{login, password}` | права пользователя (как `GET /auth/me`); cookie сессии |
| `POST /auth/logout` | — | — ; сессия закрыта, cookie стёрта |
| `GET /auth/me` | — | `{user, role, must_change_password, permissions, session_expires_at}` |
| `POST /auth/password` | `{current_password, new_password}` | права пользователя; остальные его сессии закрыты |
| `GET /auth/permissions` | — | что можно выдать роли: `sections`, `dataset_actions`, `field_limits`, `doc_type_actions` с подписями |

- Сессия передаётся в cookie `localai_session` (`HttpOnly`, `SameSite=Strict`, `Secure` по HTTPS) или в заголовке `Authorization: Bearer <токен>`. Токен в базе хранится только как SHA-256 (SEC-01).
- Сессия живёт до 12 часов и закрывается после 15 минут без запросов (SEC-02); тогда — `401 session_expired`, интерфейс показывает вход.
- Неверный логин и неверный пароль дают одинаковый ответ `401 login_failed`. После 5 неверных паролей подряд вход в учётную запись закрыт на 15 минут — `429 login_locked`; с одного адреса — не больше 20 неудачных входов за 15 минут — `429 login_throttled`.
- После сброса пароля пользователь входит временным паролем и до смены видит только `/auth/*`; остальные маршруты отвечают `403 password_change_required` (ROLE-06).
- Пароль — от 12 до 256 символов и не содержит логина.
- Права читаются из базы на каждый запрос: изменение роли, её прав, блокировка действуют на открытые сессии со следующего запроса (ROLE-09).
- `must_change_password` и `permissions` в ответе входа подсказывают интерфейсу, какие экраны показывать; проверяет права всегда сервер.

`permissions` в ответе:

```json
{
  "sections": ["admin", "kks", "structure"],
  "datasets": {"*": ["view"], "<UUID реестра>": ["create", "edit"]},
  "fields": {"<UUID поля>": "hidden", "<UUID поля>": "readonly"}
}
```

### Права маршрутов

| Право (`x-permission`) | Кто проходит | Маршруты |
| --- | --- | --- |
| `public` | все | `POST /auth/login`, `GET /system/health` |
| `self` | вошедший, в том числе с временным паролем | `/auth/logout`, `/auth/me`, `/auth/password` |
| `session` | вошедший | чтение структуры и каталога ККС, `GET /auth/permissions` |
| `section.structure` | роль с разделом «Структура» | изменение площадок, типов, полей, реестров, справочников |
| `section.kks` | роль с разделом «Каталог ККС» | изменение каталога ККС |
| `section.admin` | роль с разделом «Администрирование» | пользователи, роли, журнал, разрешённые адреса |
| `section.backups` | роль с разделом «Копии» | резервные копии |
| `dataset.view`, `.create`, `.edit`, `.archive`, `.purge` | роль с этим действием над реестром из пути (или реестром записи) | маршруты записей, `GET /datasets/{id}` |

- `GET /datasets` показывает только реестры, которые роль может видеть. Карточка ККС считает связи только в таких реестрах.
- **Ограничения полей.** Скрытое поле (`hidden`) не приходит в документах записей, истории, версиях и в `GET /types/{id}` (кроме роли с разделом «Структура»); по нему нельзя фильтровать и сортировать (`400 query_invalid`), поиск его не видит. Изменение скрытого или закрытого для правки поля (`readonly`) — `403 field_forbidden` с ключом поля; откат, который изменил бы такое поле, тоже отклоняется. Если у роли есть скрытые поля реестра, поиск идёт перебором видимых значений, а не по индексу: он медленнее, а даты ищутся в виде `2024-03-15`.
- Импорт и экспорт (`dataset.import`, `dataset.export`), разделы «ИИ», «Инструменты», «Табель» и права на типы документов хранятся в матрице уже сейчас и начнут действовать вместе со своими маршрутами.

### Пользователи и роли

Все маршруты — `section.admin`.

| Метод и путь | Тело запроса | Ответ |
| --- | --- | --- |
| `GET /roles` | — | роли с числом пользователей `users` |
| `POST /roles` | `{name, description?, copy_from?}` | роль с правами; `copy_from` — UUID роли, права которой копируются |
| `GET /roles/{id}` | — | роль и `permissions` — список `{scope, target, action}` |
| `PATCH /roles/{id}` | `{name?, description?}` | роль; права и журнал остаются за UUID роли (ROLE-02) |
| `POST /roles/{id}/archive`, `/restore` | — | роль; пользователи отключённой роли не входят, их сессии закрыты |
| `PUT /roles/{id}/permissions` | `{permissions: [{scope, target, action}]}` | роль; матрица заменяется целиком, в журнал пишется разница |
| `GET /users` | — | учётные записи |
| `POST /users` | `{login, name, role_id}` | учётная запись и `temporary_password`, `201` |
| `GET /users/{id}` | — | учётная запись |
| `PATCH /users/{id}` | `{name?, role_id?}` | учётная запись |
| `POST /users/{id}/block`, `/unblock` | — | учётная запись; блокировка закрывает сессии |
| `POST /users/{id}/reset-password` | — | учётная запись и `temporary_password`; сессии пользователя закрыты |

Строка матрицы прав:

| `scope` | `target` | `action` |
| --- | --- | --- |
| `section` | `admin`, `structure`, `kks`, `ai`, `tools`, `timesheet`, `backups` | `use` |
| `dataset` | UUID реестра или `*` — все реестры, в том числе будущие | `view`, `create`, `edit`, `archive`, `purge` (удалять из архива окончательно), `import`, `export` |
| `field` | UUID поля | `hidden` — не видеть, `readonly` — не править; без строки поле подчиняется правам на реестр |
| `doc_type` | `*` (типы документов — с этапа 4) | `view` |

- Учётная запись: `{id, login, name, role_id, role_name, must_change_password, blocked_at, locked_until, last_login_at, password_changed_at, created_at, updated_at}`. Пароль и его хэш не выдаются никогда (ROLE-07).
- `temporary_password` показывается один раз — в ответе на создание и сброс; повтор того же запроса по ключу идемпотентности его не возвращает.
- Изменение, после которого не останется ни одного активного пользователя с разделом «Администрирование» (блокировка, смена роли, отключение роли, снятие права), отклоняется — `409 last_admin` (ROLE-08).
- Стартовые роли «Администратор», «Инженер», «Наблюдатель» создаются вместе с базой и изменяются как любые другие (ROLE-10). Пользователей в новой базе нет: первого администратора создаёт установщик (SEC-03):

```
python -m server.admin --db localai.sqlite3 create-admin --login admin --name "Иванов И. И."
```

Пароль вводится дважды или передаётся первой строкой ввода с `--password-stdin`. Команда `reset-password --login …` выдаёт временный пароль, если единственный администратор забыл свой.

## Резервные копии

Все маршруты — `section.backups`. Копии лежат в папке сервера (по умолчанию `backups` рядом с базой; `python -m server --backups <папка>`).

| Метод и путь | Тело запроса | Ответ |
| --- | --- | --- |
| `GET /backups` | — | копии от новых к старым |
| `POST /backups` | `{password, label?}` | копия, `201` |
| `GET /backups/{name}/file` | — | файл копии (`application/octet-stream`) |
| `POST /backups/{name}/restore` | `{password}` | `{restored, protective_backup, schema_version}` |

Копия в списке: `{name, size, created_at, label, schema_version, database_id, app_version}`. Файл, который не читается как копия, приходит с полем `error`.

- **Копия зашифрована** (SEC-09): AES-256-GCM, ключ из пароля через Argon2id. Пароль — от 12 символов, в базе не хранится; без него копию не восстановить. Время, подпись и версия схемы читаются без пароля, чтобы показать список.
- Копия делается из согласованного снимка базы и проверяется (`integrity_check`) до шифрования. Работа других пользователей во время копии не останавливается.
- **Восстановление** сначала расшифровывает и проверяет копию: неверный пароль — `400 backup_password`, повреждённый или обрезанный файл — `400 backup_corrupt`, копия от более новой программы — `400 schema_newer`. Пока проверка не пройдена, база не меняется.
- Перед заменой базы текущее состояние упаковывается **тем же паролем** в защитную копию с подписью «Перед восстановлением» (SEC-09); ошибочное восстановление отменяется восстановлением из неё.
- После восстановления схема доводится до текущей версии, а **все сессии закрываются**: входят заново, по учётным записям из копии. В журнал восстановленной базы пишется `backup.restore` с именем защитной копии.
- Копию с другой машины кладут в папку копий сервера; загрузки файла через API нет: тело запроса ограничено 10 МиБ.

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
| `POST /datasets/{id}/records/values` | `{field, limit?}` и условия `where`, `search`, `kks`, `archived` | `{field, items: [{value, count}], distinct, refs}` |
| `POST /datasets/{id}/records/batch-update` | `{items: [{id, expected_version}], values}` | `{changed, items: [{id, version}]}` |
| `POST /datasets/{id}/records/purge` | `{items: [{id, expected_version}]}` | `{purged}` |
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

**Значения поля** (`…/records/values`, право `dataset.view`) — список для фильтра столбца: самые частые значения поля и число записей с каждым при тех же условиях, что у подсчёта. `limit` — от 1 до 200, по умолчанию 50; `distinct` — сколько всего разных значений. Запись без значения попадает в группу `value: null`; запись с многозначным полем считается в каждом своём значении один раз. ID значений справочника и ККС подписаны в `refs`. Скрытое от роли поле — `400 query_invalid`, поле `field`.

**Пакетная правка** (`…/records/batch-update`, право `dataset.edit`, F-007) записывает одни и те же `values` в выбранные записи. До 5000 записей за раз, все из реестра пути; у каждой своя ожидаемая версия. Каждая изменённая запись получает новую версию и строку истории; запись, где значения уже такие, не меняется. Ошибка на любой записи отменяет всю правку; в `details` — `row` (номер в `items`, с нуля) и `record_id`. В журнал пишутся `record.update` по каждой записи и `record.update_many` по реестру.

**Окончательное удаление** (`…/records/purge`, право `dataset.purge`, F-009) удаляет записи из архива вместе с историей, связями ККС и строкой поиска. Действующую запись сначала отправляют в архив, иначе — `409 record_not_archived`. Объекты ККС остаются в каталоге. В журнале остаются `record.purge` (ID записи и её последняя версия, без значений) и `record.purge_many`. Действие необратимо; право по умолчанию есть только у ролей с разделом «Администрирование».

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
| `GET /system/allowed-ips` | разрешённые сети: `{id, network, note, created_at}` |
| `POST /system/allowed-ips` | `{network, note?}` — адрес `10.0.5.7` или сеть `10.0.5.0/24`; ответ — запись списка, `201` |
| `DELETE /system/allowed-ips/{id}` | — |
| `GET /system/health` | `{"status": "ok", "version": "0.1.0", "schema_version": 4, "database_id": "…", "setup_required": false}`; `setup_required` — в базе ещё нет ни одного пользователя |

Разрешённые адреса (SEC-04) — `section.admin`. Пустой список пропускает любой адрес; непустой — только адреса из своих сетей, остальным любой маршрут, в том числе вход, отвечает `403 address_not_allowed`. Адрес самого сервера (`127.0.0.1`, `::1`) пропускается всегда. Изменение, после которого адрес администратора, сделавшего его, окажется вне списка, отклоняется — `409 network_lockout`.

**HTTPS** (SEC-04). По умолчанию сервер слушает только `127.0.0.1`. В сети он запускается только с сертификатом: `python -m server.admin --db … create-certificate --host <имя> --host <адрес>` создаёт ключ и самоподписанный сертификат в папке `tls` рядом с базой и печатает отпечаток SHA-256 для сверки в браузере. Без сертификата `python -m server --host 0.0.0.0` не запускается. По HTTPS cookie сессии получает признак `Secure`.

Событие журнала: `{seq, at, actor_id, actor_label, origin, action, entity_kind, entity_id, dataset_id, details}`. `action` имеет вид `record.update`, `kks.rename`, `field.create`. События доступа: `auth.login`, `auth.login_failed` (в `details` — введённый логин и причина), `auth.logout`, `user.create`, `user.update`, `user.block`, `user.unblock`, `user.password_reset`, `user.password_change`, `role.create`, `role.update`, `role.archive`, `role.restore`, `role.permissions` (в `details` — добавленные и снятые права), `backup.create`, `backup.restore`, `network.allow`, `network.remove`. `actor_id` — UUID учётной записи (ROLE-04).
