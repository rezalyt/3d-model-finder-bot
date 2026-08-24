# 3D Model Finder Bot

Telegram-бот профессионального поиска 3D-ассетов для ландшафтной визуализации. Бот работает как **поисковый навигатор**: не скачивает и не хранит модели, а показывает превью, метаданные, лицензию и ссылку на оригинальный источник.

## Продуктовая идея

Пользователь описывает задачу обычным языком, например:

- `берёза для ландшафтного проекта в SketchUp`
- `уличный фонарь для архитектурной визуализации FBX`
- `садовая скамейка GLB`

Search-service определяет профиль задачи (`landscape` по умолчанию), программную среду и категорию Landscape, затем применяет task-aware ranking.

## Архитектура

- `bot.py` — Telegram UI, формат/категория и пагинация результатов.
- `search-service/server.js` — HTTP search API, кэш, retries/rate limit и orchestration провайдеров.
- `search-service/provider-policy.js` — явная provider compliance policy.
- `search-service/task-profiles.js` — профили задач, software aliases и Landscape categories.
- `search-service/search-quality.js` — нормализация и task-aware ranking.
- `render_server.py` — запуск сервисов в Railway.

## Provider policy

MVP использует только GREEN-провайдеров:

- `polyhaven`
- `ambientcg`

NASA, Smithsonian и Sketchfab находятся в YELLOW-категории и не включаются в MVP автоматически: для них требуется отдельная проверка условий/API.

Запрещённые или неподтверждённые источники нельзя подключать через scraping только потому, что технически доступна страница или endpoint.

## Ранжирование

Ranking разделяет несколько сигналов, а не смешивает всё в один «quality»:

- `relevance` — соответствие запросу;
- `taskFit` — пригодность для Landscape и категории;
- `formatFit` — соответствие формату и software workflow;
- `technical` — preview, PBR/материалы, размеры и доступный polygon metadata;
- `popularity` — rating/downloads/views как слабые вспомогательные сигналы;
- `source` — небольшая стабильность источника.

Лицензия и provenance не считаются качеством модели и должны рассматриваться отдельно.

## Ограничения MVP

Бот намеренно НЕ:

- скачивает модели;
- хранит модели;
- проксирует большие файлы;
- принимает платежи;
- использует Mini App;
- подключает непроверенные providers через scraping.

Ссылка `Подробнее` ведёт на оригинальный источник модели.

## Локальный запуск

Требуется Python 3.12 и Node.js 22+.

```bash
python -m venv .venv
# Windows PowerShell
.\\.venv\\Scripts\\Activate.ps1
# Linux/macOS
# source .venv/bin/activate

pip install -r requirements-dev.txt
```

Создайте `.env`:

```env
BOT_TOKEN=ваш_telegram_bot_token
SEARCH_SERVICE_URL=http://127.0.0.1:8787

SEARCH_LIMIT=12
PROVIDER_LIMIT=24
HTTP_TIMEOUT_MS=15000
SEARCH_CACHE_TTL_MS=120000
SEARCH_CACHE_MAX_ENTRIES=256
SEARCH_RATE_LIMIT_SECONDS=3
SEARCH_RATE_LIMIT_MAX_USERS=4096
SEARCH_REQUEST_TIMEOUT_SECONDS=45
MAX_QUERY_CHARS=160
RATE_LIMIT_WINDOW_MS=60000
RATE_LIMIT_MAX_REQUESTS=30
RATE_LIMIT_MAX_KEYS=4096
```

Запуск search-service:

```bash
cd search-service
npm install
npm start
```

Проверка:

```text
http://127.0.0.1:8787/health
```

Запуск бота из корня проекта:

```bash
python bot.py
```

## Тесты

```bash
pytest -q
cd search-service
npm test
npm run check
```

CI выполняет Python tests, Node tests и syntax check для search-service.

## Безопасность

- bounded LRU/TTL cache;
- rate limiting для пользователя и search-service;
- ограничение длины запроса;
- таймауты внешних запросов;
- ошибки provider не показываются пользователю;
- секреты не выводятся в логи;
- Telegram webhook защищён secret token;
- provider policy блокирует неподтверждённые операции.

## Следующий этап

После MVP нужно измерить:

- repeat search rate;
- successful search rate;
- result click/source-open rate;
- feedback;
- востребованные категории и форматы.

Только после подтверждения ценности имеет смысл добавлять persistent user history, saved searches, partner APIs, Professional Search/Pro или Mini App.
