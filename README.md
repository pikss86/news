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

Для Docker-варианта нужны только Docker Engine и Docker Compose. Создайте Telegram application на
[my.telegram.org/apps](https://my.telegram.org/apps), затем сохраните выданные `api_id` и
`api_hash`. Используйте номер аккаунта, который уже подписан на нужные каналы. Никогда не
коммитьте заполненный `.env`, коды входа, 2FA-пароль или ключ TDLib database.

```bash
cp .env.example .env
```

В `.env` обязательно замените:

- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE_NUMBER`;
- `TELEGRAM_DATABASE_ENCRYPTION_KEY` — стабильная случайная строка;
- `POSTGRES_PASSWORD` и пароль внутри `DATABASE_URL` — они должны совпадать.

Если пароль содержит URL-special символы, percent-encode его в `DATABASE_URL`.

## Сборка и первый запуск

Образ собирает `libtdjson.so` непосредственно из официального репозитория TDLib. По умолчанию
закреплён конкретный commit, указанный в `Dockerfile` и `docker-compose.yml`; его можно осознанно
переопределить через `TDLIB_GIT_REF` в `.env`. Первая сборка TDLib занимает заметное время.

```bash
docker compose build
docker compose up -d postgres
docker compose run --rm collector
```

Alembic автоматически выполняет `upgrade head` перед каждым запуском collector. При первом входе
TDLib запросит код в интерактивном терминале, а для защищённого аккаунта — 2FA password. Значения
не записываются приложением. После сообщения `TDLib connected` collector уже работает; дайте ему
получить первые updates, остановите интерактивный процесс `Ctrl+C` и запустите постоянный сервис:

```bash
docker compose up -d
docker compose logs -f collector
```

Named volume `tdlib_data` смонтирован в `/var/lib/tdlib` и переживает пересоздание контейнера.
PostgreSQL использует отдельный `postgres_data`. Обычные `docker compose stop/down` volumes не
удаляют. Команда `docker compose down -v` **удалит обе базы и авторизацию**, поэтому применять её
следует только при намеренном полном сбросе.

Остановка без удаления данных:

```bash
docker compose stop
```

## Что именно нормализуется

Поддержаны `updateNewMessage`, `updateMessageContent`, `updateMessageEdited`,
`updateMessageInteractionInfo`, `updateDeleteMessages`, `updateNewChat`, `updateChatTitle` и
`updateChatPhoto`. Сохраняются sender object, publication/edit/collection timestamps, type и полный
content object, plain text/caption, forward/reply objects, media metadata, interaction info и
deleted state. Полный исходный update остаётся в `td_events`; отсутствующие в TDLib данные не
синтезируются.

Любые остальные ответы/updates сохраняются raw и могут быть нормализованы будущей миграцией или
отдельным processing pipeline. Collector не скачивает media-файлы: он сохраняет доступные TDLib
file/media descriptors.

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

Миграции вручную:

```bash
docker compose run --rm collector alembic current
docker compose run --rm collector alembic upgrade head
```

## Ограничения milestone 0.1

- Сохраняются updates, которые TDLib доставил данному аккаунту после запуска. Полный исторический
  backfill и автоматическое управление списком каналов пока не реализованы.
- Удаление/редактирование известно только если TDLib доставил соответствующий update. Telegram не
  гарантирует восстановление события, пропущенного до первой установки collector.
- Media-файлы не загружаются, secret chats отключены, comments отдельно не классифицируются.
- Raw dedup по полному JSON означает, что два байт-семантически одинаковых updates считаются одним
  наблюдаемым событием; время первой фиксации сохраняется.
- Нет claim extraction, story clustering, fact checking, source scoring, alerts, LLM, Kafka,
  Neo4j или вероятностной модели.

Следующий аналитический pipeline сможет читать версии как artifacts, не меняя ingestion:

```text
telegram_message_versions → stories → claims → evidence/facts
                          → temporal knowledge graph → beliefs/delta/alerts
```
