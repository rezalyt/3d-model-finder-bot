# 3D Model Finder Bot

Telegram-бот для поиска 3D-моделей по обычному текстовому запросу. Бот нормализует русские запросы, фильтрует модели по формату и категории, ранжирует результаты по качеству и показывает карточки с превью. При наличии прямой ссылки на файл модель можно скачать прямо в Telegram.

## Архитектура

- `bot.py` — базовый Telegram-бот и общие функции.
- `bot_features.py` — улучшенные карточки результатов, пагинация и безопасное скачивание.
- `search-service/server.js` — HTTP-сервис поиска по белому списку надёжных источников.
- `search-service/search-quality.js` — нормализация, quality scoring и диверсификация результатов.
- `search-service/provider-policy.js` — явный список разрешённых провайдеров.
- `render_server.py` — точка входа, которая запускает оба сервиса внутри Railway-контейнера.
- `Dockerfile` — production-образ.
- `railway.json` — настройки деплоя и healthcheck.

## Поиск

Используются только эти источники через параметр `providers` библиотеки `@pikal6/3dfetch`:

- `polyhaven`
- `printables`
- `thangs`
- `nasa`
- `ambientcg`
- `sketchfab`

Провайдеры `CGTrader`, `Cults3D`, `Free3D`, `Blend Swap` и `GrabCAD` не участвуют в поисковой выдаче. Остальные источники 3dfetch также не включаются по умолчанию, даже если библиотека их поддерживает.

Поиск выполняется с повторными попытками и экспоненциальной задержкой. Ошибки отдельных провайдеров собираются для диагностики и не показываются пользователю.

## Ранжирование качества

Каждая модель получает `qualityScore`. Балл учитывает:

- релевантность названия, тегов и описания;
- надёжность источника;
- наличие требуемого формата;
- современные форматы `GLTF/GLB/FBX`;
- наличие превью и прямой ссылки скачивания;
- лицензию и описание;
- рейтинг, лайки, просмотры и загрузки, когда источник их предоставляет;
- наличие текстур;
- число полигонов: предпочтительная зона — примерно `10k–100k`, а очень маленькие и экстремально тяжёлые модели получают штраф;
- разнообразие источников и удаление дублей.

В production по умолчанию возвращается до 12 лучших результатов.

## Карточки и пагинация

Пользователь получает карточки с:

- превью модели;
- названием;
- доступными форматами;
- источником;
- лицензией;
- quality score;
- размером файла, если источник его сообщает;
- автором, если доступен.

Для каждой модели доступны кнопки `⬇️ Скачать` и `ℹ️ Подробнее`, а результаты листаются по 3–5 моделей на страницу.

## Скачивание

Бот скачивает только прямые HTTP/HTTPS `downloadUrl`. Перед отправкой файл загружается потоково во временный файл и проверяется по размеру.

Текущий лимит Telegram Bot API для `sendDocument` — **50 MB**. Поэтому файлы больше 50 MB не загружаются в Telegram: пользователю показывается прямая ссылка на источник. citeturn742404search2

Для защиты от злоупотреблений действует дневной лимит скачиваний на пользователя, по умолчанию `10`.

Важно: у некоторых площадок прямое скачивание требует отдельной авторизации. Например, Sketchfab Download API требует аутентификацию пользователя и не выдаёт скачиваемый архив без соответствующего доступа. citeturn830069search0turn830069search1

## Локальный запуск

Требуется Python 3.12 и Node.js 22+.

### 1. Установка Python-зависимостей

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux/macOS
# source .venv/bin/activate

pip install -r requirements-dev.txt
```

### 2. Переменные окружения

```env
BOT_TOKEN=ваш_telegram_bot_token
SEARCH_SERVICE_URL=http://127.0.0.1:8787

# Опционально
SKETCHFAB_API_TOKEN=
THINGIVERSE_API_TOKEN=
MYMINIFACTORY_API_KEY=
POLY_PIZZA_KEY=

# Поиск
SEARCH_LIMIT=12
PROVIDER_LIMIT=24
HTTP_TIMEOUT_MS=15000
PROVIDER_RETRIES=3
PROVIDER_RETRY_BASE_MS=300
SEARCH_CACHE_TTL_MS=120000
SEARCH_CACHE_MAX_ENTRIES=256

# Telegram / защита
SEARCH_RATE_LIMIT_SECONDS=3
SEARCH_RATE_LIMIT_MAX_USERS=4096
SEARCH_REQUEST_TIMEOUT_SECONDS=45
DOWNLOADS_PER_DAY=10
DOWNLOAD_MAX_BYTES=52428800
DOWNLOAD_MAX_USERS=4096
DOWNLOAD_TIMEOUT_SECONDS=60
RESULTS_PAGE_SIZE=4

# API rate limiting
RATE_LIMIT_WINDOW_MS=60000
RATE_LIMIT_MAX_REQUESTS=30
RATE_LIMIT_MAX_KEYS=4096
```

Не добавляйте `.env` в Git. В Railway используйте Variables.

### 3. Запуск search-service

```bash
cd search-service
npm install
npm start
```

Проверка: `http://127.0.0.1:8787/health`.

### 4. Запуск бота

```bash
python bot.py
```

В production Railway запускает `render_server.py`, который поднимает Telegram webhook и search-service вместе.

## Тесты

```bash
pytest -q
cd search-service
npm test
npm run check
```

CI запускает Python-тесты, quality-тесты и Node.js syntax check автоматически.

## Безопасность

- bounded LRU/TTL-кэш;
- rate limiting для поиска;
- отдельный дневной лимит скачиваний;
- ограничение размера загружаемых файлов;
- временные файлы удаляются после отправки;
- ошибки провайдеров не показываются пользователю;
- API-ключи не выводятся в логи;
- публичный search-service защищён rate limiting;
- Telegram webhook защищён secret token.

## Лицензии

Наличие модели в каталоге не означает одинаковые права на её использование. Перед использованием модели проверяйте лицензию и требования конкретного источника. Для Sketchfab приложение должно отображать лицензию и атрибуцию автора при использовании Download API. citeturn830069search2turn830069search9
