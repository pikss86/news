# News Telegram Collector

Первая рабочая версия ingestion-фундамента для наблюдения за Telegram. Сервис входит в
Telegram как обычный пользователь через официальный JSON-интерфейс TDLib, сохраняет входящие
TDLib updates в PostgreSQL и поддерживает удобную текущую проекцию сообщений с неизменяемой
историей версий.

Collector делает только три вещи: **collect, preserve, normalize**. Он не проверяет истинность
публикаций, не извлекает claims и не выполняет аналитику. Telegram-сообщение здесь — наблюдаемый
информационный артефакт, а не установленный факт.

## Архитектура

```text
Telegram → libtdjson → asyncio collector ┬→ td_events (raw JSONB)
                                        └→ chats/messages/version history
```

Raw и normalized storage разделены намеренно:

- `td_events` — append-oriented журнал оригинальных JSON-объектов, полученных от TDLib. Точные
  повторы дедуплицируются по SHA-256 канонического JSON. Неизвестные типы также сохраняются.
- `telegram_chats` — последняя известная проекция чатов. Если сообщение пришло раньше полного
  `updateNewChat`, создаётся допустимая неполная запись, которая позднее обогащается.
- `telegram_messages` — идентичность `(chat_id, message_id)` и последнее наблюдаемое состояние.
- `telegram_message_versions` — неизменяемые снимки значимых состояний. Создаются версии
  `created`, `edited`, `metadata` и `deleted`; одинаковый снимок повторной версии не создаёт.
- `telegram_files` — последнее состояние обнаруженных файлов, локальный путь, прогресс и
  устойчивая отметка о запросе скачивания.

`source_event_id` у каждой версии ведёт к исходному `td_events`. Версия удаления является
tombstone: она устанавливает `is_deleted/deleted_at`, но сохраняет последний известный текст и
metadata. Поэтому цепочка `версия → raw update → chat/message → исходное содержимое` не теряется.

Raw event и изменение нормализованной проекции фиксируются одной транзакцией. Если TDLib
повторит уже закоммиченный update после рестарта, unique fingerprint не позволит повторно
применить его. Если известный update получил новую несовместимую форму, savepoint откатывает
только нормализацию: raw JSON сохраняется, а ошибка видна в structured logs. При обрыве соединения
с PostgreSQL операции повторяются с exponential backoff; constraint/programming errors не
маскируются под временную недоступность БД.

## Требования и Telegram credentials

Нужны Linux-host, Docker Engine, Docker Compose и действующее подключение Amnezia VPN на этом
host. Панель намеренно не слушает публичный адрес, loopback или обычный LAN-интерфейс. При запуске
она выбирает ровно один активный private-интерфейс с консервативным Amnezia/WireGuard/TUN-именем;
при отсутствии или неоднозначности кандидатов завершает работу с ошибкой.

Создайте Telegram application на [my.telegram.org/apps](https://my.telegram.org/apps) и получите
`api_id` и `api_hash`. Используйте номер аккаунта, уже подписанного на нужные каналы. Эти значения,
номер телефона, код входа и 2FA-пароль вводятся в браузере и не должны коммититься.

Обычный first run не требует `.env`. Файл `.env.example` содержит только необязательные deployment
overrides и аварийный environment-only режим. Не копируйте его без необходимости.

## Сборка и первый запуск

Образ собирает официальный JSON-интерфейс TDLib из закреплённого commit. Первая сборка может
занять заметное время.

```bash
docker compose up -d --build
docker compose ps
```

При первом запуске bootstrap один раз создаёт внутренний пароль PostgreSQL, ключ шифрования
настроек и ключ подписания управляющих команд. Они сохраняются в отдельном volume с правами `0600`.
Collector после чистого запуска находится в состоянии `stopped`: Telegram и ingestion не
запускаются до явного действия администратора.

Узнайте VPN-адрес host, например:

```bash
ip -brief address | grep -Ei 'amn|amnezia|awg|wg|tun'
```

Откройте с телефона, подключённого к той же Amnezia VPN, адрес вида
`http://<VPN-IP-СЕРВЕРА>:8080`. Дальнейший сценарий полностью браузерный:

1. Создайте пароль администратора длиной не менее 12 символов.
2. Введите `api_id`, `api_hash`, телефон и остальные настройки. Встроенный Database URL уже
   заполнен внутренними credentials.
3. Нажмите «Сохранить черновик». Сохранение не запускает сбор.
4. Нажмите «Войти в Telegram», дождитесь кода в официальном приложении и введите его на отдельной
   странице. Поле облачного пароля или дополнительные шаги появятся только если Telegram их
   действительно запросит.
5. Нажмите «Проверить подключения». Панель отдельно покажет PostgreSQL, состояние миграций,
   загрузку TDLib, Telegram session и доступность storage.
6. Только после зелёных обязательных проверок нажмите «Сохранить и запустить сбор».

Страница статуса показывает draft/active/applied revision, состояние collector и время последнего
успешно сохранённого update. Там же доступны stop, restart, смена admin password и rollback старой
ревизии в новый черновик. Rollback ничего не запускает автоматически.

Кнопка «Открыть собранные данные» ведёт в read-only браузер PostgreSQL. В нём доступны общий
счётчик, чаты, поиск и фильтрация сообщений, история каждой версии, удалённые сообщения,
оригинальные TDLib JSON events и состояние media-файлов. Списки выводятся постранично. Браузер
защищён той же VPN-границей и admin-сессией и не содержит операций изменения или удаления данных.

Основная навигация браузера иерархическая: `чат → сообщение → прикреплённые файлы`. Для доступного
файла администратор может вручную нажать «Скачать в кэш». Панель передаёт подписанную команду
работающему collector-у; отдельный TDLib-процесс не запускается. Файл ставится в ту же устойчивую
очередь, что и автоматические media downloads, даже если глобальная автозагрузка выключена.

Скачивание media включается флагом на странице. Приоритет 1–32 задаёт порядок запросов; фильтров
размера и типа пока нет, поэтому следите за свободным местом.

Логи и остановка:

```bash
docker compose logs -f admin collector postgres
docker compose stop
```

Volumes `tdlib_data`, `postgres_data`, `service_settings` и `service_secrets` переживают recreate и
обычный `docker compose down`. Команда `docker compose down -v` безвозвратно удалит сообщения,
авторизацию, настройки и внутренние ключи; используйте её только для намеренного полного сброса.

## Amnezia overrides, recovery и backup

Если autodiscovery невозможен, создайте `.env` и явно задайте все значения:

```dotenv
AMNEZIA_ADMIN_INTERFACE=amn0
AMNEZIA_ADMIN_BIND_ADDRESS=10.8.0.1
AMNEZIA_ADMIN_ALLOWED_CIDRS=10.8.0.0/24
```

Адрес обязан существовать на активном интерфейсе, быть private и входить в разрешённый CIDR.
Wildcard/public/multicast адреса отклоняются. `X-Forwarded-For` не используется для допуска.

Для backup остановите сервис и сохраните все четыре volume согласованно. Нельзя восстанавливать
`service_settings` без соответствующего `service_secrets`: настройки зашифрованы, а управляющие
команды подписаны. Отдельно документируйте резервную копию PostgreSQL. Никогда не помещайте
выгруженные secrets в Git или общедоступное облако.

Environment-only recovery остаётся отдельным режимом и не смешивается с UI snapshot:

```bash
cp .env.example .env
# заполнить recovery-поля
docker compose stop collector admin
docker compose --profile recovery run --rm collector-recovery
```

Одновременно запускать recovery worker и обычный collector нельзя.

## Threat model панели

VPN boundary снижает сетевую доступность, но не заменяет пароль. Панель дополнительно использует
стойкий hash пароля, server-side sessions, one-time CSRF, rate limit и cookies без доступа из
client-side scripts. Секреты шифруются на диске и маскируются в HTML/status/errors. Root или
пользователь с доступом к Docker volumes всё равно находится внутри доверенной границы и может
получить ключи; защита от полностью скомпрометированного host не заявляется. Панель не получает
Docker socket и не выполняет произвольные shell-команды.

## Что именно нормализуется

Поддержаны `updateNewMessage`, `updateMessageContent`, `updateMessageEdited`,
`updateMessageInteractionInfo`, `updateDeleteMessages`, `updateNewChat`, `updateChatTitle` и
`updateChatPhoto`. Сохраняются sender object, publication/edit/collection timestamps, type и полный
content object, plain text/caption, forward/reply objects, media metadata, interaction info и
deleted state. Обнаруженные файловые дескрипторы записываются в `telegram_files`. При включённом
флаге незавершённые файлы ставятся в устойчивую очередь, а их `updateFile` обновляет прогресс,
статус завершения и `local_path`. Полный исходный update остаётся в `td_events`; отсутствующие в
TDLib данные не синтезируются.

Любые остальные ответы/updates сохраняются raw и могут быть нормализованы будущей миграцией или
отдельным processing pipeline. Когда скачивание выключено, collector только инвентаризирует
доступные file/media descriptors. Если позже включить флаг и перезапустить сервис, он поставит в
очередь уже известные незавершённые файлы.

## SQL-примеры

Открыть `psql`:

```bash
docker compose exec postgres psql -U news -d news
```

Последние 100 неудалённых сообщений:

```sql
SELECT chat_id, message_id, published_at, content_type, text, interaction_info
FROM telegram_messages
WHERE NOT is_deleted
ORDER BY published_at DESC NULLS LAST
LIMIT 100;
```

Сообщения конкретного чата:

```sql
SELECT message_id, published_at, edited_at, is_deleted, text
FROM telegram_messages
WHERE chat_id = -1001234567890
ORDER BY message_id DESC;
```

Полная наблюдавшаяся история сообщения с provenance:

```sql
SELECT v.version_number, v.observed_at, v.change_type, v.snapshot,
       e.event_type, e.payload AS source_update
FROM telegram_message_versions AS v
JOIN td_events AS e ON e.id = v.source_event_id
WHERE v.chat_id = -1001234567890 AND v.message_id = 12345
ORDER BY v.version_number;
```

Удалённые сообщения:

```sql
SELECT chat_id, message_id, published_at, deleted_at, text
FROM telegram_messages
WHERE is_deleted
ORDER BY deleted_at DESC;
```

Raw events заданного типа:

```sql
SELECT id, received_at, chat_id, message_id, payload
FROM td_events
WHERE event_type = 'updateMessageContent'
ORDER BY received_at DESC
LIMIT 100;
```

Последние скачанные media-файлы:

```sql
SELECT file_id, size, downloaded_size, local_path, download_completed_at
FROM telegram_files
WHERE is_downloading_completed
ORDER BY download_completed_at DESC
LIMIT 100;
```

Файлы, для которых загрузка запрошена, но ещё не завершена:

```sql
SELECT file_id, expected_size, downloaded_size,
       is_downloading_active, last_download_requested_at
FROM telegram_files
WHERE download_requested_at IS NOT NULL
  AND NOT is_downloading_completed
ORDER BY last_download_requested_at;
```

## Разработка и тесты

Проект требует Python 3.12+. Локально необходимо отдельно собрать TDLib по
[официальной инструкции](https://github.com/tdlib/td#building) и указать путь к `libtdjson` через
`TDLIB_LIBRARY_PATH`. Для unit tests сама библиотека TDLib не нужна:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest
```

`tests/test_repository_postgres.py` дополнительно проверяет реальные PostgreSQL constraints,
UPSERT/dedup и последовательность `created → edited → deleted`. По умолчанию он пропускается;
включение:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://news:password@localhost:5432/news_test pytest
```

Тест создаёт и удаляет только собственную случайно названную schema внутри указанной test DB.

В штатном режиме supervisor перед каждым запуском ingestion передаёт Alembic URL активной
ревизии и выполняет upgrade до head. Preflight показывает состояние схемы без изменения данных;
ручной URL из encrypted storage в environment не экспортируется.

## OpenSpec

Проект использует OpenSpec для управления требованиями к следующим изменениям. Это инструмент
разработки, а не runtime-зависимость collector. Постоянные спецификации фактически реализованного
поведения находятся в `openspec/specs/`, активные предложения — в `openspec/changes/`, а
завершённые предложения после архивирования сохраняются в `openspec/changes/archive/`.

Для работы нужен Node.js 20.19 или новее:

```bash
npm install -g @fission-ai/openspec@latest
openspec --version
openspec validate --all --strict
```

Интеграция с Codex хранится в `.agents/skills/`. Типичный цикл изменения начинается в чате
Codex:

```text
$openspec-explore Описать идею или проблему
$openspec-propose Предложить конкретное изменение
$openspec-apply-change <имя-изменения>
$openspec-archive-change <имя-изменения>
```

Между `propose` и `apply` необходимо проверить `proposal.md`, delta-specs, `design.md` и
`tasks.md`. Архивировать изменение следует только после реализации, тестов и проверки
соответствия спецификации. `openspec/config.yaml` задаёт постоянные ограничения проекта:
разделение артефактов и фактов, сохранение provenance и истории, минимальный scope, обязательные
миграции, тесты и документацию.

Базовые спецификации описывают авторизацию, raw event store, нормализацию, версионирование,
загрузку media и эксплуатационную надёжность. `CONCEPT.md` остаётся более широким описанием
назначения и будущего развития и не подменяет спецификации текущего поведения.

## Ограничения milestone 0.1

- Сохраняются updates, которые TDLib доставил данному аккаунту после запуска. Полный исторический
  backfill и автоматическое управление списком каналов пока не реализованы.
- Удаление/редактирование известно только если TDLib доставил соответствующий update. Telegram не
  гарантирует восстановление события, пропущенного до первой установки collector.
- Скачивание media опционально и не имеет фильтров размера или типа; secret chats отключены,
  comments отдельно не классифицируются.
- Raw dedup по полному JSON означает, что два байт-семантически одинаковых updates считаются одним
  наблюдаемым событием; время первой фиксации сохраняется.
- Нет claim extraction, story clustering, fact checking, source scoring, alerts, LLM, Kafka,
  Neo4j или вероятностной модели.

Следующий аналитический pipeline сможет читать версии как artifacts, не меняя ingestion:

```text
telegram_message_versions → stories → claims → evidence/facts
                          → temporal knowledge graph → beliefs/delta/alerts
```
