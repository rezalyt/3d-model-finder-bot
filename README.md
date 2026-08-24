# 3D Model Finder Bot

Telegram-бот для поиска 3D-моделей по обычному текстовому запросу. Бот нормализует русские запросы, поддерживает фильтры по формату и категории и использует отдельный search-service с несколькими источниками моделей.

## Архитектура

- `bot.py` — Telegram-бот и пользовательский интерфейс.
- `search-service/server.js` — HTTP-сервис поиска по провайдерам.
- `render_server.py` — процесс запуска сервисов внутри Railway-контейнера.
- `Dockerfile` — production-образ.
- `railway.json` — настройки деплоя и healthcheck.

## Локальный запуск

Требуется Python 3.12 и Node.js 22+.

### 1. Установить зависимости Python

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux/macOS
# source .venv/bin/activate

pip install -r requirements-dev.txt
```

### 2. Настроить переменные окружения

Создайте `.env` в корне проекта:

```env
BOT_TOKEN=ваш_telegram_bot_token
SEARCH_SERVICE_URL=http://127.0.0.1:8787

# Необязательно
SKETCHFAB_API_TOKEN=
POLY_PIZZA_KEY=
THINGIVERSE_API_TOKEN=
MYMINIFACTORY_API_KEY=
SMITHSONIAN_API_KEY=

# Защита и ограничения
SEARCH_RATE_LIMIT_SECONDS=3
SEARCH_RATE_LIMIT_MAX_USERS=4096
SEARCH_CACHE_TTL_SECONDS=1800
SEARCH_CACHE_MAX_ENTRIES=256
SEARCH_REQUEST_TIMEOUT_SECONDS=45
MAX_QUERY_CHARS=160

# Search-service
SEARCH_LIMIT=12
PROVIDER_LIMIT=30
HTTP_TIMEOUT_MS=15000
SEARCH_CACHE_TTL_MS=60000
SEARCH_CACHE_MAX_ENTRIES=256
PROVIDER_CONCURRENCY=6
RATE_LIMIT_WINDOW_MS=60000
RATE_LIMIT_MAX_REQUESTS=30
RATE_LIMIT_MAX_KEYS=4096
```

Секреты не добавляйте в Git. Используйте `.env` только локально, а в Railway задавайте переменные через Variables.

### 3. Запустить search-service

```bash
cd search-service
npm install
npm start
```

Сервис должен отвечать на `http://127.0.0.1:8787/health`.

### 4. Запустить бота

В отдельном терминале из корня:

```bash
python bot.py
```

## Проверка

Тесты не требуют доступа к Telegram или внешним каталогам:

```bash
pytest -q
```

CI запускает те же тесты автоматически для изменений репозитория.

## Защита от флуда

В боте действует ограничение частоты поисковых запросов на пользователя. Дополнительно Telegram API-вызовы проходят через `AIORateLimiter`.

Search-service ограничивает запросы к публичному `/search` по IP, использует bounded map и возвращает HTTP `429` с `Retry-After` при превышении лимита.

Кэш поиска ограничен по размеру и TTL, поэтому старые записи не могут расти бесконечно.

## Railway

Проект рассчитан на запуск через `Dockerfile`. `railway.json` использует `render_server.py` и healthcheck `/health`.

Основные production-переменные нужно задать в Railway Variables. Особенно важны `BOT_TOKEN`, `SEARCH_SERVICE_URL` и ключи провайдеров, которые реально используются в вашей конфигурации.

## Обновление зависимостей

Перед обновлением major-версий сначала запускайте тесты. Для production не используйте произвольные `latest`: версии фиксируются в `requirements.txt` и `search-service/package.json`.

## Безопасность

Перед публикацией или передачей проекта третьим лицам проверьте:

- что `.env` не попал в Git;
- что токены находятся только в секретах Railway/GitHub;
- что публичный search-service защищён rate limiting;
- что запросы имеют ограничение длины и HTTP timeout;
- что кэши ограничены по TTL и максимальному количеству записей;
- что в логах не выводятся API-ключи и Authorization-заголовки.
