import asyncio
import html
import logging
import os
import re
import time
from collections import OrderedDict

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import AIORateLimiter, Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
SEARCH_SERVICE_URL = os.getenv("SEARCH_SERVICE_URL", "").rstrip("/")

if not TOKEN:
    raise ValueError("BOT_TOKEN должен быть задан")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

for noisy_logger in ("httpx", "httpcore", "telegram", "telegram.ext", "telegram.request", "aiohttp.access"):
    logging.getLogger(noisy_logger).setLevel(WARNING := (logging.WARNING if noisy_logger != "aiohttp.access" else logging.INFO))

PAGE_SIZE = 5
CACHE_TTL = max(60, int(os.getenv("SEARCH_CACHE_TTL_SECONDS", str(30 * 60))))
CACHE_MAX = max(32, int(os.getenv("SEARCH_CACHE_MAX_ENTRIES", "256")))
REQUEST_TIMEOUT = max(5, int(os.getenv("SEARCH_REQUEST_TIMEOUT_SECONDS", "45")))
MAX_QUERY_CHARS = max(40, int(os.getenv("MAX_QUERY_CHARS", "160")))
SEARCH_RATE_LIMIT_SECONDS = max(1, float(os.getenv("SEARCH_RATE_LIMIT_SECONDS", "3")))
SEARCH_RATE_LIMIT_MAX_USERS = max(32, int(os.getenv("SEARCH_RATE_LIMIT_MAX_USERS", "4096")))
MIN_QUALITY_SCORE = float(os.getenv("MIN_QUALITY_SCORE", "35"))
ALLOWED_SOURCES = {"polyhaven", "ambientcg"}

search_cache: OrderedDict[tuple[str, str | None, str | None], dict] = OrderedDict()
search_timestamps: OrderedDict[int, float] = OrderedDict()

FORMAT_ALIASES = {
    "gltf": "gltf", "glb": "glb", "obj": "obj", "fbx": "fbx", "blend": "blend",
    "usd": "usd", "usdz": "usdz", "stl": "stl", "3mf": "3mf", "dae": "dae",
    "ply": "ply", "step": "step", "stp": "step", "iges": "iges", "igs": "iges",
    "off": "off", "max": "max", "c4d": "c4d", "abc": "abc", "3ds": "3ds",
}
FORMAT_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, FORMAT_ALIASES)) + r")(?![a-z0-9])",
    re.IGNORECASE,
)

RUSSIAN_ALIASES = {
    "берёза": "birch", "береза": "birch", "сосна": "pine", "ель": "fir", "ёлка": "christmas tree", "елка": "christmas tree",
    "куст": "shrub", "кустарник": "shrub", "растение": "plant", "цветок": "flower", "трава": "grass",
    "фонарь": "lamp", "светильник": "lamp", "скамейка": "bench", "пергола": "pergola", "беседка": "gazebo",
    "забор": "fence", "камень": "rock", "валун": "boulder", "дорожка": "path", "дом": "house", "здание": "building",
    "стул": "chair", "стулья": "chairs", "кресло": "armchair", "диван": "sofa", "стол": "table", "лампа": "lamp",
    "машина": "car", "автомобиль": "car", "человек": "human", "скульптура": "sculpture", "сад": "garden", "фасад": "facade",
}
STOP_WORDS = {
    "найди", "найти", "поищи", "поиск", "ищи", "ищу", "нужен", "нужна", "нужно", "модель", "модели",
    "3d", "мне", "покажи", "показать", "дай", "для", "сделай", "скачай", "файл", "файлы", "бесплатный", "бесплатная", "бесплатные",
}

FORMAT_BUTTONS = [
    ("STL", "stl"), ("OBJ", "obj"), ("FBX", "fbx"), ("GLB", "glb"),
    ("GLTF", "gltf"), ("BLEND", "blend"), ("3MF", "3mf"), ("STEP", "step"),
    ("IGES", "iges"), ("USD", "usd"), ("USDZ", "usdz"), ("DAE", "dae"),
    ("PLY", "ply"), ("OFF", "off"), ("ABC", "abc"), ("MAX", "max"),
    ("C4D", "c4d"), ("3DS", "3ds"),
]
CATEGORY_BUTTONS = [
    ("🏠 Архитектура", "architecture"), ("🛋 Мебель", "furniture"),
    ("🚗 Транспорт", "vehicle"), ("🧍 Персонажи", "character"),
    ("🌳 Природа", "nature"), ("🐾 Животные", "animal"),
    ("🔧 Промышленность", "industrial"), ("💻 Техника", "electronics"),
    ("🎨 Декор", "decor"), ("📦 Другое", "other"),
]


def extract_and_normalize_query(text: str) -> tuple[str, str | None]:
    raw = re.sub(r"\s+", " ", (text or "").strip())[:MAX_QUERY_CHARS]
    if not raw:
        return "", None
    match = FORMAT_PATTERN.search(raw)
    fmt = FORMAT_ALIASES[match.group(0).lower()] if match else None
    if match:
        raw = (raw[:match.start()] + " " + raw[match.end():]).strip()
    tokens = []
    for token in re.findall(r"[\w-]+", raw.lower(), flags=re.UNICODE):
        if token in STOP_WORDS:
            continue
        tokens.append(RUSSIAN_ALIASES.get(token, token))
    return " ".join(tokens)[:MAX_QUERY_CHARS], fmt


def cache_get(key):
    item = search_cache.get(key)
    if not item:
        return None
    if time.monotonic() - item["ts"] >= CACHE_TTL:
        search_cache.pop(key, None)
        return None
    search_cache.move_to_end(key)
    return item["results"]


def cache_put(key, results):
    now = time.monotonic()
    search_cache[key] = {"results": results, "ts": now}
    search_cache.move_to_end(key)
    for old_key, item in list(search_cache.items()):
        if now - item["ts"] >= CACHE_TTL:
            search_cache.pop(old_key, None)
    while len(search_cache) > CACHE_MAX:
        search_cache.popitem(last=False)


def rate_limit_search(user_id: int | None) -> float:
    if user_id is None:
        return 0.0
    now = time.monotonic()
    last = search_timestamps.get(user_id)
    if last is not None:
        remaining = SEARCH_RATE_LIMIT_SECONDS - (now - last)
        if remaining > 0:
            search_timestamps.move_to_end(user_id)
            return remaining
    search_timestamps[user_id] = now
    search_timestamps.move_to_end(user_id)
    while len(search_timestamps) > SEARCH_RATE_LIMIT_MAX_USERS:
        search_timestamps.popitem(last=False)
    return 0.0


def format_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(FORMAT_BUTTONS), 4):
        rows.append([InlineKeyboardButton(label, callback_data=f"format:{value}") for label, value in FORMAT_BUTTONS[i:i + 4]])
    rows.append([InlineKeyboardButton("🔎 Любой формат", callback_data="format:any")])
    rows.append([InlineKeyboardButton("❌ Отменить выбор", callback_data="format:clear")])
    return InlineKeyboardMarkup(rows)


def category_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(CATEGORY_BUTTONS), 2):
        rows.append([InlineKeyboardButton(label, callback_data=f"category:{value}") for label, value in CATEGORY_BUTTONS[i:i + 2]])
    rows.append([InlineKeyboardButton("❌ Без категории", callback_data="category:clear")])
    return InlineKeyboardMarkup(rows)


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Формат", callback_data="show_formats"), InlineKeyboardButton("🎯 Категория", callback_data="show_categories")],
        [InlineKeyboardButton("🔎 Поиск без фильтров", callback_data="clear_filters")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>3D Model Finder</b>\n\n"
        "Профессиональный поиск 3D-ассетов для архитектуры и визуализации.\n\n"
        "Напишите задачу обычным языком. Например:\n"
        "• берёза для ландшафтной визуализации FBX\n"
        "• фасад современного дома для архитектурной визуализации\n"
        "• садовая скамейка для SketchUp OBJ\n\n"
        "В выдачу попадают только результаты с доступным preview и из разрешённых источников.",
        parse_mode="HTML",
        reply_markup=start_keyboard(),
    )


async def fetch_search_service(query: str, format_name: str | None, category: str | None) -> list:
    if not SEARCH_SERVICE_URL:
        return []
    params = {"q": query}
    if format_name:
        params["format"] = format_name
    if category and category != "other":
        params["category"] = category
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{SEARCH_SERVICE_URL}/search", params=params) as resp:
                if resp.status != 200:
                    logger.warning("Search service HTTP %s", resp.status)
                    return []
                data = await resp.json(content_type=None)
                raw = data.get("results", []) if isinstance(data, dict) else []
                cleaned = []
                for model in raw:
                    source = str(model.get("source", "")).lower()
                    thumbnail = model.get("thumbnailUrl") or model.get("thumbnail")
                    score = float(model.get("qualityScore", 0) or 0)
                    if source not in ALLOWED_SOURCES:
                        logger.warning("Blocked non-GREEN result from bot layer: %s", source)
                        continue
                    if not thumbnail:
                        logger.info("Dropped result without preview: %s", model.get("name", "unknown"))
                        continue
                    if score < MIN_QUALITY_SCORE:
                        logger.info("Dropped low-score result: %s (%.1f)", model.get("name", "unknown"), score)
                        continue
                    cleaned.append(model)
                return cleaned
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("Search service unavailable: %s", type(exc).__name__)
        return []


def normalize_service_model(model: dict, index: int) -> dict:
    return {
        "name": model.get("name", f"Модель {index}"),
        "viewerUrl": model.get("sourceUrl", model.get("viewerUrl", "")),
        "license": {"label": model.get("license") or "Не указана"},
        "thumbnail": model.get("thumbnailUrl") or model.get("thumbnail"),
        "formats": model.get("formats", []),
        "source": model.get("source", "unknown"),
        "qualityScore": model.get("qualityScore"),
        "scoreComponents": model.get("scoreComponents") or {},
        "access": model.get("access"),
    }


def build_caption(model: dict, index: int) -> str:
    name = html.escape(str(model.get("name", f"Модель {index}")))
    license_data = model.get("license")
    license_name = license_data.get("label", "Не указана") if isinstance(license_data, dict) else (license_data or "Не указана")
    license_name = html.escape(str(license_name))
    formats = model.get("formats") or []
    extra = f"\n📦 Форматы: {', '.join(html.escape(str(x).upper()) for x in formats)}" if formats else ""
    provider = f"\n🔎 Источник: {html.escape(str(model.get('source')))}" if model.get("source") else ""
    score = model.get("qualityScore")
    score_line = f"\n⭐ Пригодность: {float(score):.0f}/100" if score is not None else ""
    return f"<b>{index}. {name}</b>{score_line}\n📜 Лицензия: {license_name}{extra}{provider}"


def get_preview_url(model: dict):
    if model.get("thumbnail"):
        return model["thumbnail"]
    thumbnails = model.get("thumbnails", {}).get("images", [])
    for image in thumbnails:
        if image.get("size") == 256 and image.get("url"):
            return image["url"]
    return thumbnails[0].get("url") if thumbnails else None


def build_nav_keyboard(offset: int, total: int) -> InlineKeyboardMarkup:
    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page:{offset - PAGE_SIZE}"))
    if offset + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"page:{offset + PAGE_SIZE}"))
    rows = [nav_row] if nav_row else []
    rows.append([InlineKeyboardButton("📦 Формат", callback_data="show_formats"), InlineKeyboardButton("🎯 Категория", callback_data="show_categories")])
    return InlineKeyboardMarkup(rows)


async def send_page(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    query = context.user_data.get("query")
    offset = context.user_data.get("offset", 0)
    results = context.user_data.get("results", [])
    page_items = results[offset:offset + PAGE_SIZE]
    if not page_items:
        await context.bot.send_message(chat_id, "Подходящих моделей на этой странице нет.")
        return

    for i, model in enumerate(page_items, start=offset + 1):
        caption = build_caption(model, i)
        site_url = model.get("viewerUrl", "")
        preview_url = get_preview_url(model)
        if not preview_url:
            continue
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Открыть источник", url=site_url)]]) if site_url else None
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=preview_url, caption=caption, parse_mode="HTML", reply_markup=keyboard)
            await asyncio.sleep(0.2)
        except Exception as exc:
            logger.warning("Ошибка отправки preview: %s", type(exc).__name__)

    shown_to = min(offset + PAGE_SIZE, len(results))
    await context.bot.send_message(
        chat_id,
        f"Показаны {offset + 1}–{shown_to} из {len(results)} по запросу «{html.escape(str(query))}».",
        reply_markup=build_nav_keyboard(offset, len(results)),
    )


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id if update.effective_user else None
    remaining = rate_limit_search(user_id)
    if remaining > 0:
        await update.message.reply_text(f"⏳ Слишком частые запросы. Повторите через {remaining:.1f} сек.")
        return

    query_text, typed_format = extract_and_normalize_query(text)
    selected_format = context.user_data.get("selected_format")
    category = context.user_data.get("selected_category")
    format_name = typed_format or selected_format
    if not query_text:
        await update.message.reply_text("Напишите, что искать, например: дерево FBX для ландшафтной визуализации")
        return

    context.user_data["selected_format"] = format_name
    format_label = f" [{format_name.upper()}]" if format_name else ""
    category_label = f" + 🎯 {category}" if category else ""
    await update.message.reply_text(f"🔍 Ищу профессиональные ассеты: «{query_text}»{format_label}{category_label}...")

    results = await fetch_search_service(query_text, format_name, category)
    if not results:
        suffix = f" в формате {format_name.upper()}" if format_name else ""
        await update.message.reply_text(
            f"Не нашёл достаточно качественных моделей для «{query_text}»{suffix}.\n"
            "Попробуйте уточнить объект, назначение или формат."
        )
        return

    normalized = [normalize_service_model(model, i) for i, model in enumerate(results, 1)]
    normalized = [model for model in normalized if get_preview_url(model)]
    if not normalized:
        await update.message.reply_text("Найдены результаты, но у них нет пригодного preview. Я не показываю такие модели.")
        return

    context.user_data.update(query=query_text, results=normalized, offset=0, format_name=format_name, category_name=category)
    await send_page(context, update.effective_chat.id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text:
        await run_search(update, context, text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    callback = update.callback_query
    await callback.answer()
    data = callback.data or ""
    chat_id = callback.message.chat_id

    if data == "show_formats":
        await context.bot.send_message(chat_id, "📦 <b>Выберите формат:</b>", parse_mode="HTML", reply_markup=format_keyboard())
        return
    if data == "show_categories":
        await context.bot.send_message(chat_id, "🎯 <b>Выберите категорию:</b>", parse_mode="HTML", reply_markup=category_keyboard())
        return
    if data == "clear_filters":
        context.user_data.pop("selected_format", None)
        context.user_data.pop("selected_category", None)
        await context.bot.send_message(chat_id, "✅ Формат и категория сброшены. Напишите запрос.")
        return
    if data.startswith("format:"):
        value = data.split(":", 1)[1]
        if value in {"clear", "any"}:
            context.user_data.pop("selected_format", None)
            await context.bot.send_message(chat_id, "✅ Ограничение по формату снято.")
        else:
            context.user_data["selected_format"] = value
            await context.bot.send_message(chat_id, f"✅ Формат <b>{value.upper()}</b> выбран.", parse_mode="HTML")
        return
    if data.startswith("category:"):
        value = data.split(":", 1)[1]
        if value == "clear":
            context.user_data.pop("selected_category", None)
            await context.bot.send_message(chat_id, "✅ Категория сброшена.")
        else:
            context.user_data["selected_category"] = value
            await context.bot.send_message(chat_id, f"✅ Категория <b>{value}</b> выбрана.", parse_mode="HTML")
        return
    if data.startswith("page:"):
        try:
            context.user_data["offset"] = max(0, int(data.split(":", 1)[1]))
        except ValueError:
            context.user_data["offset"] = 0
        await send_page(context, chat_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if context.error:
        logger.error("Telegram handler error: %s", type(context.error).__name__)


def build_application() -> Application:
    rate_limiter = AIORateLimiter(overall_max_rate=30, overall_time_period=1)
    app = Application.builder().token(TOKEN).rate_limiter(rate_limiter).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)
    return app
