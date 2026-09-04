## 1. Clean bootstrap и модель настроек

- [x] 1.1 Добавить минимальные ASGI/template/crypto/password-hashing зависимости и проверить их установку полной Docker build
- [x] 1.2 Выделить общую immutable модель всех collector-настроек с metadata раздела, secret-флага и apply class; проверить defaults, ranges и cross-field validation unit-тестами
- [x] 1.3 Реализовать idempotent bootstrap внутренних PostgreSQL credentials и settings encryption key в persistent secret volume; проверить clean run, повторный run, file permissions и fail-closed damaged secret
- [x] 1.4 Перевести bundled PostgreSQL и collector на file-based internal database secret без обязательного заполненного `.env`; проверить запуск чистого Compose stack и сохранение password после recreate
- [x] 1.5 Сохранить environment-only recovery loader и проверить, что persistent snapshot и environment не смешиваются внутри одной конфигурации

## 2. Зашифрованные drafts, revisions и control state

- [x] 2.1 Реализовать authenticated encryption, atomic write/fsync/rename и file locking; проверить round-trip, wrong key, corrupted ciphertext и отсутствие plaintext secrets на диске
- [x] 2.2 Реализовать append-only revisions, отдельные draft/active/applied pointers и changed-field metadata; проверить монотонную нумерацию и сохранность предыдущих revisions
- [x] 2.3 Реализовать rollback-as-new-draft и invalidation check results после изменения draft hash; проверить, что rollback не запускает collector автоматически
- [x] 2.4 Реализовать signed monotonic start/stop/restart requests и atomic supervisor status без произвольных команд; проверить повтор request id и damaged control file
- [x] 2.5 Реализовать централизованную redaction phone, API hash, database URL/password, authentication code и 2FA; проверить logs, status, HTML и errors secret-scan тестами

## 3. Web security

- [x] 3.1 Реализовать создание/замену admin password со стойким hash и проверить отсутствие plaintext password в persistent files и responses
- [x] 3.2 Реализовать server-side sessions с idle/absolute expiry и HttpOnly SameSite=Strict cookie; проверить login/logout/expiry/restart invalidation и Secure flag при TLS mode
- [x] 3.3 Добавить one-time CSRF tokens и login rate limiting; проверить missing/wrong/reused token и временную блокировку неверных входов

## 4. First-run UI и диагностика

- [x] 4.1 Создать отдельный admin entrypoint и server-rendered setup/login/settings/status/revisions routes, доступные до Telegram/PostgreSQL configuration; проверить first-run ASGI flow
- [x] 4.2 Создать responsive templates и локальный CSS без CDN для всех sections, checks и controls; проверить mobile viewport без горизонтальной прокрутки
- [x] 4.3 Реализовать masked secret inputs с семантикой «пусто — оставить», подтверждённым удалением optional secret и field-level validation; проверить отсутствие partial draft save
- [x] 4.4 Реализовать PostgreSQL connect/SELECT 1 и Alembic current/head preflight без изменения ingestion data; проверить success, unreachable DB, auth error и migration-required states
- [x] 4.5 Реализовать TDLib load, runtime path, writable directories и disk-space preflight; проверить component-level results и блокировку запуска при обязательной ошибке
- [x] 4.6 Реализовать dashboard со статусами draft/active/applied, PostgreSQL, migrations, TDLib, Telegram, storage, collector и last update; проверить безопасное отображение каждой error category

## 5. Telegram authorization через браузер

- [x] 5.1 Отделить TDLib authorization state machine от terminal input через typed challenge/response interface и сохранить terminal adapter; подтвердить существующий interactive flow unit-тестами
- [x] 5.2 Реализовать одноразовый authorization broker с correlation id, timeout и current-state validation; проверить stale/wrong/duplicate response rejection
- [x] 5.3 Добавить защищённые формы для authentication code, 2FA, email/email code, registration data и other-device confirmation; проверить каждый state с fake TDLib client
- [x] 5.4 Проверить, что codes/passwords не попадают в settings revisions, status, cookies, HTML responses и logs после обработки
- [x] 5.5 Связать Telegram ready result с точным draft hash и persistent TDLib state; проверить invalidation после смены credentials и повторное использование авторизованной session после restart

## 6. Supervisor и явное управление сбором

- [x] 6.1 Рефакторизовать создание collector для готового Settings snapshot и сохранить legacy environment entrypoint; подтвердить существующими unit и PostgreSQL integration tests
- [x] 6.2 Реализовать supervisor с desired states unconfigured/stopped/running и ровно одним ingestion worker; проверить lifecycle test doubles
- [x] 6.3 Реализовать кнопку «Сохранить черновик» без запуска и «Проверить подключения» с привязкой результатов к draft hash; проверить, что непроверенный draft не влияет на running collector
- [x] 6.4 Реализовать «Сохранить и запустить сбор» только после обязательных checks и Telegram ready; проверить atomic active promotion и переход starting → running/error
- [x] 6.5 Реализовать graceful stop/restart с закрытием TDLib и database engine до следующего start; проверить порядок ресурсов и idempotent повтор команды
- [x] 6.6 Реализовать dynamic apply для log/media fields и managed restart для connection/TDLib/retry fields; проверить applied revision и fallback на restart при dynamic failure
- [x] 6.7 Выполнять Alembic upgrade после active database resolution и до ingestion; проверить fresh/current/outdated/error database без потери raw events/messages/versions
- [x] 6.8 Обновлять last successful update timestamp из collector и проверить его отображение и сохранение safe status после container restart

## 7. Docker, документация и итоговая проверка

- [x] 7.1 Обновить Compose стандартным bootstrap/admin/supervisor flow, named secret/settings volumes, non-root user, dropped capabilities, read-only root filesystem и отсутствующим Docker socket; проверить `docker compose config`
- [x] 7.2 Добавить healthchecks и startup ordering так, чтобы clean `docker compose up -d` поднимал панель и БД, но не ingestion; проверить состояние контейнеров без `.env`
- [x] 7.3 Обновить `.env.example` только optional override/recovery placeholders и проверить, что `.env`, bootstrap secrets и settings data игнорируются Git и не входят в image
- [x] 7.4 Обновить README сценарием «одна команда → пароль → настройки → checks → Telegram code/2FA → start», recovery, backup и threat model
- [x] 7.5 Обновить CONCEPT.md описанием setup/control plane, диагностик и явного запуска без привязки к конкретным технологиям
- [x] 7.6 Добавить end-to-end test clean bootstrap → setup → draft → checks → web authorization → start → last update → stop → restart → rollback без реальных credentials
- [x] 7.7 Запустить `openspec validate --all --strict`, format, lint, полный pytest, PostgreSQL integration, Alembic check, Compose build/config и исправить найденные ошибки
- [ ] 7.8 С реальными Telegram credentials проверить code/2FA, ready и получение update; явно отделить эти результаты от автоматических тестов
