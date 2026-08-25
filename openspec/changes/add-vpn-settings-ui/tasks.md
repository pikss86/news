## 1. Модель и безопасное хранилище настроек

- [ ] 1.1 Расширить зависимости минимальным ASGI/template/crypto/password-hashing набором и подтвердить установку через сборку Docker image
- [ ] 1.2 Выделить общую модель всех collector-настроек с metadata раздела, secret-флага и класса применения; проверить defaults, диапазоны и полную Pydantic-валидацию unit-тестами
- [ ] 1.3 Добавить отдельную bootstrap-модель для settings volume, master-key file, Amnezia bind address/CIDR, session timeout и TLS-cookie flag; проверить отклонение пустых, wildcard, loopback и публичных сетей
- [ ] 1.4 Реализовать authenticated encryption и атомарную запись файлов с правами владельца; проверить round-trip, неверный ключ, повреждённый ciphertext и отсутствие plaintext secrets на диске
- [ ] 1.5 Реализовать append-only ревизии, manifest, changed-field metadata, согласованный snapshot и rollback-as-new-revision; проверить нумерацию, параллельное сохранение и сохранность прошлых ревизий
- [ ] 1.6 Реализовать безопасный приоритет persistent revision → environment fallback → optional defaults и проверить, что источники не смешиваются внутри одного snapshot

## 2. Безопасность административного доступа

- [ ] 2.1 Реализовать проверку Amnezia peer address по нескольким private CIDR до маршрутизации; проверить allow/deny и игнорирование X-Forwarded-For тестами middleware
- [ ] 2.2 Реализовать первичное создание и замену административного пароля со стойким хешированием; проверить отсутствие пароля в файлах, ответах и logs
- [ ] 2.3 Реализовать server-side sessions с absolute/idle expiry и HttpOnly SameSite=Strict cookie; проверить вход, выход, expiration и завершение сессий после рестарта
- [ ] 2.4 Добавить synchronizer CSRF tokens на все изменяющие маршруты и проверить, что отсутствующий, чужой и повторно использованный token не изменяют состояние
- [ ] 2.5 Добавить ограничение попыток входа по непосредственному адресу клиента и проверить временную блокировку без записи введённого пароля

## 3. Административная web-страница

- [ ] 3.1 Создать отдельный admin entrypoint и server-rendered маршруты setup/login/logout/settings/status/revisions/rollback; проверить доступность setup до конфигурации PostgreSQL и Telegram
- [ ] 3.2 Создать адаптивные HTML templates и локальный CSS без CDN, отобразив все разделы модели, defaults, descriptions и классы применения; проверить рендеринг на mobile viewport
- [ ] 3.3 Реализовать masked secret inputs с семантикой «пусто — оставить прежнее» и отдельным подтверждённым удалением optional secret; проверить отсутствие сохранённых секретов в HTML и response bodies
- [ ] 3.4 Реализовать атомарную обработку формы и field-level ошибки без частичного сохранения; проверить invalid ranges, blank required values и противоречивые database-поля
- [ ] 3.5 Реализовать историю безопасных metadata и подтверждённый rollback, проверив, что rollback создаёт новую ревизию и не показывает значения secrets
- [ ] 3.6 Реализовать status page с saved/applied revision, состоянием collector и безопасной категорией последней ошибки; проверить маскирование URL, Telegram и административных secrets

## 4. Supervisor и применение конфигурации

- [ ] 4.1 Рефакторизовать создание collector так, чтобы он принимал готовый immutable Settings snapshot и сохранял прежний environment-only entrypoint; подтвердить существующими unit/integration-тестами
- [ ] 4.2 Реализовать supervisor, который ждёт полную конфигурацию, запускает ровно один collector и атомарно публикует status; проверить состояния unconfigured/starting/running/error/stopped
- [ ] 4.3 Реализовать обнаружение новой ревизии и dynamic-применение log level, media flag и download priority; проверить номер фактически применённой ревизии
- [ ] 4.4 Реализовать graceful collector restart для Telegram/database/TDLib/retry изменений с закрытием TDLib и database engine до нового запуска; проверить порядок lifecycle с test doubles
- [ ] 4.5 Перенести применение Alembic migrations после разрешения database URL и до старта ingestion; проверить новую БД, актуальную БД и безопасный status при ошибке миграции
- [ ] 4.6 Проверить, что неуспешное применение новой ревизии не меняет raw events/messages/versions и не объявляется успешным

## 5. Docker и Amnezia deployment

- [ ] 5.1 Добавить непривилегированный admin service в opt-in Compose profile с host networking, settings_data volume и без Docker socket/capabilities; проверить итоговый `docker compose config`
- [ ] 5.2 Настроить admin listener на точный `AMNEZIA_ADMIN_BIND_ADDRESS` с fail-closed запуском и отключёнными proxy headers; проверить отказ при отсутствующем интерфейсе без wildcard fallback
- [ ] 5.3 Подключить settings_data к collector supervisor и сохранить совместимый запуск без admin profile; проверить restart/recreate обоих контейнеров без потери ревизий
- [ ] 5.4 Обновить `.env.example` только bootstrap/fallback placeholders и убедиться через `git check-ignore`/secret scan, что реальные `.env`, master key и settings volume не отслеживаются Git

## 6. Документация и итоговая проверка

- [ ] 6.1 Обновить README: threat model, получение Amnezia interface IP/CIDR, создание master-key file, запуск admin profile, первый пароль, настройка с телефона, применение/rollback и emergency environment-only recovery
- [ ] 6.2 Обновить CONCEPT.md описанием административного control plane без привязки к конкретным технологиям и без смешивания с ingestion/analysis
- [ ] 6.3 Добавить тесты security headers, secret redaction и полного first-run → save → supervisor apply → rollback потока без реальных credentials
- [ ] 6.4 Запустить `openspec validate --all --strict`, formatting, lint, полный pytest, PostgreSQL integration test, Alembic check и Docker Compose build/config; исправить все найденные ошибки
- [ ] 6.5 На Linux-host с известными параметрами Amnezia проверить bind только на VPN-IP и deny клиента вне CIDR; если целевая сеть недоступна, явно оставить эту проверку как требующую реального deployment
