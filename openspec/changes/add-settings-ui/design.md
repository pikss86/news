## Context

Collector сейчас требует готовый `.env`, доступную PostgreSQL и интерактивный terminal для Telegram authorization до начала работы. Целевой first-run должен поднять безопасную web-панель и внутреннюю БД без пользовательских credentials, затем провести оператора через настройку, диагностику и browser-based authorization до отдельной команды запуска ingestion.

## Goals / Non-Goals

**Goals:**

- Обеспечить `docker compose up -d` на чистой установке без заполненного `.env`.
- Открыть панель и bundled PostgreSQL раньше конфигурации Telegram collector.
- Разделить draft, результаты проверок, active revision и applied revision.
- Выполнить Telegram authorization целиком через мобильный браузер.
- Дать оператору понятные start/stop/restart и component-level diagnostics.
- Сохранить аварийный environment-only запуск и существующие ingestion-данные.

**Non-Goals:**

- Панель не управляет Docker daemon, firewall, произвольным PostgreSQL server или пересборкой TDLib.
- `TDLIB_GIT_REF` остаётся build input. Для bundled PostgreSQL внутренние credentials генерируются системой и не показываются; для внешней БД оператор настраивает collector connection.
- Проверка подключения не доказывает истинность, полноту или качество Telegram-содержимого.
- Публичный reverse proxy, внешний identity provider и просмотр собранных сообщений не входят в первую версию.

## Decisions

### 1. Стандартный стек состоит из bootstrap, PostgreSQL, admin и collector supervisor

Одноразовый bootstrap-компонент до запуска зависимых сервисов создаёт внутренний PostgreSQL password и settings encryption key в отдельном named volume. Он использует криптографический генератор, атомарную запись и права только для владельца; при наличии корректных файлов ничего не меняет. Повреждение существующего secret приводит к fail-closed ошибке, а не к тихой ротации поверх данных.

PostgreSQL читает password через file-based secret. Admin не зависит от PostgreSQL и может открыть first-run setup. Collector container запускает supervisor даже при отсутствии прикладной конфигурации, но сам ingestion worker остаётся stopped/unconfigured.

Альтернатива с обязательным `.env` отклонена, потому что не решает мобильный first-run. Фиксированный default password отклонён как небезопасный.

### 2. Отдельный admin и data-only control channel

Admin и collector supervisor разделяют только `settings_data`, `bootstrap_secrets` и существующий TDLib state там, где это необходимо для authorization test. Admin записывает versioned settings, authorization responses и ограниченное desired state (`stopped`, `running`, `restart-requested`). Supervisor публикует status атомарным файлом и исполняет только заранее определённые переходы.

Docker socket, PID namespace и произвольные команды не подключаются. Control request имеет монотонный номер, тип, target revision и authenticated MAC, чтобы повтор старого файла не запускал действие повторно.

Альтернатива с HTTP control API collector отклонена из-за дополнительного listener и аутентификации между контейнерами. Прямая перезагрузка контейнера из admin отклонена как чрезмерно привилегированная.

### 3. Зашифрованные draft/active ревизии вне основной БД

Settings store размещается в named volume, потому что database URL сам является настройкой. Payload каждой ревизии шифруется authenticated encryption; manifest содержит только номера draft/active ревизий, changed-field metadata и безопасные timestamps. Запись использует temporary file, `fsync`, atomic rename и file lock.

Bootstrap по умолчанию создаёт encryption key в отдельном secrets volume. Для deployment с внешним secret manager допускается read-only key file override. Автоматический вариант защищает от попадания plaintext в backup manifest и accidental disclosure, но не считается защитой от root-доступа ко всем Docker volumes; это явно отражается в threat model.

Сохранение формы создаёт draft и не влияет на active/applied revision. Rollback копирует старый decrypted snapshot в новый draft. Только успешные обязательные checks позволяют атомарно назначить draft активным при явной команде запуска.

### 4. Единая схема параметров и проверяемые snapshots

Одна модель описывает environment fallback, HTML form, validation, masking и collector runtime. Поля имеют section, description, secret flag, default, constraints и apply class.

Приоритет:

1. active persistent revision для обычного запуска;
2. environment snapshot только в явно выбранном emergency/legacy режиме или до появления persistent revision;
3. defaults только для optional values.

Источники не смешиваются внутри snapshot. Empty secret input означает «не менять», а удаление optional secret требует отдельного подтверждения.

Каждый check run привязан к точному hash draft-ревизии. Любое изменение draft аннулирует прежние результаты. Обязательные checks:

- PostgreSQL connect и безопасный `SELECT 1`;
- Alembic current/head comparison без изменения данных;
- загрузка TDLib и валидность runtime paths;
- writable TDLib/config/media directories и доступное место;
- Telegram authorization state ready.

Optional checks не блокируют запуск, но отображаются отдельно. Запуск выполняет migrations до ingestion; preflight показывает необходимость upgrade, не применяя migration самостоятельно.

### 5. Telegram authorization как отдельная управляемая session

Authorization controller перестаёт напрямую читать stdin и вместо этого публикует typed challenge. В legacy режиме terminal adapter продолжает отвечать через input/getpass. В admin режиме authorization broker хранит только тип ожидаемого challenge, correlation id и безопасные presentation data.

Ответы code/password/email передаются через authenticated + CSRF-protected route и помещаются в одноразовый in-memory/short-lived IPC slot. Значения не входят в revisions, status или logs. Ответ принимается только для совпадающего current challenge и удаляется после передачи TDLib. Registration names могут быть отображены в текущей форме, но также не становятся постоянной конфигурацией.

TDLib database/files остаются persistent. Успешный state ready завершает Telegram check для hash текущего draft. Confirmation link другого устройства отображается только аутентифицированному оператору.

### 6. Server-rendered mobile UI и web security

Небольшое ASGI-приложение использует server-rendered templates и локальный responsive CSS без CDN/frontend build. Маршруты ограничены setup, login/logout, settings draft, checks, Telegram challenge, revisions/rollback, start/stop/restart и status.

При первом запуске оператор создаёт admin password, который хранится стойким hash. Sessions server-side, имеют idle/absolute expiry; cookie HttpOnly и SameSite=Strict, а при TLS override также Secure. Все state-changing requests используют one-time synchronizer CSRF token. Login rate limiting ограничивает повторные попытки входа.

Dashboard показывает checklist компонентов и различает `not_configured`, `checking`, `action_required`, `ready`, `starting`, `running`, `stopping`, `stopped`, `error`. Сообщения проходят централизованную redaction database URL, phone, API hash, codes и passwords.

### 7. Явный жизненный цикл collector

Supervisor по умолчанию имеет desired state `stopped`, если persistent configuration ещё не активировалась. Нажатие «Сохранить» создаёт draft; «Проверить» запускает preflight; «Сохранить и запустить сбор» после успешных checks назначает active revision и создаёт idempotent start request.

Для start supervisor применяет migrations, создаёт ровно один collector и помечает revision applied только после успешной инициализации. Stop вызывает graceful shutdown, закрывает TDLib/database и сохраняет volumes. Restart сериализуется как stop → start той же active revision. Новая команда с уже обработанным request id игнорируется.

Динамически применимые log/media параметры могут обновляться без reconnect; изменение credentials, database URL, TDLib paths/key или retry settings требует managed restart. Ошибка новой ревизии сохраняет ingestion data, оставляет desired/applied distinction видимым и не объявляет check успешным.

### 8. Тестируемость и совместимость

Environment-only entrypoint сохраняется для recovery. Существующие ingestion tables и migrations не переписываются. Новое хранилище конфигурации файловое и не требует migration основной БД.

Unit/integration tests используют временные volumes, ASGI client, fake TDLib authorization client и disposable PostgreSQL schema. Telegram ready проверяется отдельно с настоящими credentials и не симулируется как успешно пройденный.

## Risks / Trade-offs

- **[Root-доступ к обоим Docker volumes раскрывает key и ciphertext]** → threat model не обещает защиту от host root; поддерживается внешний read-only key override и резервное копирование отдельно от settings data.
- **[Telegram test отправляет реальный code request]** → UI явно предупреждает о side effect; test требует действия оператора и не запускает ingestion.
- **[Check устаревает после правки]** → результаты привязаны к cryptographic hash draft и немедленно инвалидируются при новой ревизии.
- **[Bundled database password невозможно безопасно показать]** → он считается внутренним generated secret; внешняя БД настраивается отдельным connection mode.
- **[Миграция может пройти preflight comparison, но упасть при start]** → status показывает отдельную migration error, ingestion не запускается, существующие данные сохраняются.

## Migration Plan

1. Добавить bootstrap volumes/service и подтвердить сохранение существующего PostgreSQL volume без смены credentials; для уже установленного проекта импортировать текущий password в secret file вместо генерации нового.
2. Добавить settings store, admin и supervisor за совместимыми entrypoints; environment-only режим оставить доступным.
3. Запустить admin и supervisor на чистых volumes и убедиться, что collector остаётся stopped, а панель готова к первоначальной настройке.
4. Создать admin password, сохранить draft, выполнить checks и browser Telegram authorization.
5. Активировать revision явной кнопкой, применить migrations и запустить ingestion; проверить status и последний update.
6. Проверить stop/restart, rollback-as-draft и восстановление после container recreate.
7. Для rollback software version остановить новые сервисы и запустить legacy collector с сохранённым `.env`; ingestion volumes и settings history не удалять.
