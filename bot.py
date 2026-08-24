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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
SKETCHFAB_TOKEN = os.getenv("SKETCHFAB_API_TOKEN")
SEARCH_SERVICE_URL = os.getenv("SEARCH_SERVICE_URL", "").rstrip("/")

if not TOKEN:
    raise ValueError("BOT_TOKEN должен быть задан")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Never let third-party HTTP libraries log Authorization URLs/headers that may contain secrets.
for noisy_logger in ("httpx", "httpcore", "telegram", "telegram.ext", "telegram.request", "aiohttp.access"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING if noisy_logger != "aiohttp.access" else logging.INFO)

PAGE_SIZE = 5
FETCH_LIMIT = 24
CACHE_TTL = max(60, int(os.getenv("SEARCH_CACHE_TTL_SECONDS", str(30 * 60))))
CACHE_MAX = max(32, int(os.getenv("SEARCH_CACHE_MAX_ENTRIES", "256")))
REQUEST_TIMEOUT = max(5, int(os.getenv("SEARCH_REQUEST_TIMEOUT_SECONDS", "45")))
MAX_QUERY_CHARS = max(40, int(os.getenv("MAX_QUERY_CHARS", "160")))

# Bounded LRU cache. Expired entries are removed on access/write; size can never grow unbounded.
search_cache: OrderedDict[tuple[str, bool, str | None, str | None], dict] = OrderedDict()

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

# Small deterministic vocabulary for the common Russian queries a consumer bot receives.
# It avoids sending verbs/grammar words to catalogue providers that use strict keyword matching.
RUSSIAN_ALIASES = {
    "кот": "cat", "кота": "cat", "кошк": "cat", "кошка": "cat", "кошку": "cat", "котенок": "kitten",
    "собака": "dog", "собаку": "dog", "пес": "dog", "пса": "dog", "щенок": "puppy",
    "лошадь": "horse", "лошадью": "horse", "лошади": "horse",
    "корова": "cow", "бык": "bull", "свинья": "pig", "волк": "wolf", "лиса": "fox",
    "медведь": "bear", "олень": "deer", "птица": "bird", "рыба": "fish",
    "дерево": "tree", "елка": "christmas tree", "ёлка": "christmas tree", "цветок": "flower",
    "дом": "house", "здание": "building", "квартира": "apartment",
    "стул": "chair", "стулья": "chairs", "кресло": "armchair", "диван": "sofa", "стол": "table",
    "шкаф": "cabinet", "кровать": "bed", "лампа": "lamp",
    "машина": "car", "автомобиль": "car", "авто": "car", "грузовик": "truck", "мотоцикл": "motorcycle",
    "самолет": "airplane", "самолёт": "airplane", "корабль": "ship", "лодка": "boat",
    "робот": "robot", "человек": "human", "мужчина": "man", "женщина": "woman", "ребенок": "child", "ребёнок": "child",
    "голова": "head", "лицо": "face", "череп": "skull", "рука": "hand", "нога": "leg",
    "дракон": "dragon", "динозавр": "dinosaur", "черепаха": "turtle", "змея": "snake",
}
STOP_WORDS = {
    "найди", "найти", "поищи", "поиск", "ищи", "ищу", "нужен", "нужна", "нужно", "модель", "модели",
    "3d", "мне", "покажи", "показать", "дай", "для", "сделай", "скачай", "файл", "файлы",
}


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
        normalized = RUSSIAN_ALIASES.get(token, token)
        tokens.append(normalized)

    query = " ".join(tokens)
    return query[:MAX_QUERY_CHARS], fmt


def cache_get(key):
    now = time.monotonic()
    item = search_cache.get(key)
    if not item:
        return None
    if now - item["ts"] >= CACHE_TTL:
        search_cache.pop(key, None)
        return None
    search_cache.move_to_end(key)
    return item["results"]


def cache_put(key, results):
    now = time.monotonic()
    search_cache[key] = {"results": results, "ts": now}
    search_cache.move_to_end(key)
    # Opportunistic TTL cleanup, then hard size bound.
    expired = [k for k, v in search_cache.items() if now - v["ts"] >= CACHE_TTL]
    for key_to_remove in expired:
        search_cache.pop(key_to_remove, None)
    while len(search_cache) > CACHE_MAX:
        search_cache.popitem(last=False)


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
        [InlineKeyboardButton("📦 Выбрать формат", callback_data="show_formats"),
         InlineKeyboardButton("🎯 Категория", callback_data="show_categories")],
        [InlineKeyboardButton("🔎 Поиск без фильтров", callback_data="clear_filters")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    selected_format = context.user_data.get("selected_format")
    selected_category = context.user_data.get("selected_category")
    selected = []
    if selected_format:
        selected.append(f"формат {selected_format.upper()}")
    if selected_category:
        selected.append(f"категория «{selected_category}»")
    selected_text = f"\n\n📌 Сейчас: {', '.join(selected)}" if selected else ""
    await update.message.reply_text(
        "🚀 <b>3D Model Finder</b>\n\n"
        "Напишите обычным языком, что нужно найти.\n\n"
        "Примеры:\n"
        "• найти STL кота\n"
        "• стул OBJ\n"
        "• robot GLB\n"
        "• дерево FBX"
        f"{selected_text}",
        parse_mode="HTML",
        reply_markup=start_keyboard(),
    )


async def fetch_search_service(query: str, format_name: str | None, category: str | None) -> list:
    if not SEARCH_SERVICE_URL:
        return []
    params = {"q": query}
    if format_name:
        params["format"] = format_name
    if category:
        params["category"] = category
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{SEARCH_SERVICE_URL}/search", params=params) as resp:
                if resp.status != 200:
                    logger.warning("Search service HTTP %s", resp.status)
                    return []
                data = await resp.json(content_type=None)
                return data.get("results", []) if isinstance(data, dict) else []
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("Search service unavailable: %s", type(exc).__name__)
        return []


async def fetch_sketchfab(query: str, free_only: bool) -> list:
    if not SKETCHFAB_TOKEN:
        return []
    cache_key = (query.lower(), free_only, "sketchfab", None)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    params = {"q": query, "type": "models", "limit": FETCH_LIMIT, "sort_by": "-relevance"}
    if free_only:
        params["downloadable"] = "true"
    headers = {"Authorization": f"Token {SKETCHFAB_TOKEN}"}
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.sketchfab.com/v3/search", params=params, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("Sketchfab HTTP %s", resp.status)
                    return []
                raw = (await resp.json(content_type=None)).get("results", [])
        results = []
        for item in raw:
            haystack = " ".join([
                str(item.get("name", "")),
                str(item.get("description", "")),
                " ".join(str(x) for x in (item.get("tags") or [])),
            ]).lower()
            if query and not all(term in haystack for term in query.split()):
                continue
            results.append(item)
        cache_put(cache_key, results)
        return results
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("Sketchfab unavailable: %s", type(exc).__name__)
        return []


def normalize_service_model(model: dict, index: int) -> dict:
    return {
        "name": model.get("name", f"Модель {index}"),
        "viewerUrl": model.get("sourceUrl", model.get("viewerUrl", "")),
        "license": {"label": model.get("license") or "Не указана"},
        "thumbnail": model.get("thumbnail") or model.get("thumbnailUrl"),
        "formats": model.get("formats", []),
        "source": model.get("source", "unknown"),
        "access": model.get("access"),
    }


def build_caption(model: dict, index: int) -> str:
    name = html.escape(str(model.get("name", f"Модель {index}")))
    license_data = model.get("license")
    if isinstance(license_data, dict):
        license_name = license_data.get("label", "Не указана")
    else:
        license_name = license_data or "Не указана"
    license_name = html.escape(str(license_name))
    formats = model.get("formats") or []
    extra = f"\n📦 Форматы: {', '.join(html.escape(str(x).upper()) for x in formats)}" if formats else ""
    provider = f"\n🔎 Источник: {html.escape(str(model.get('source')))}" if model.get("source") else ""
    access = f"\n🔐 Доступ: {html.escape(str(model['access']))}" if model.get("access") else ""
    return f"<b>{index}. {name}</b>\n📜 Лицензия: {license_name}{extra}{provider}{access}"


def get_preview_url(model: dict):
    if model.get("thumbnail"):
        return model["thumbnail"]
    thumbnails = model.get("thumbnails", {}).get("images", [])
    for img in thumbnails:
        if img.get("size") == 256:
            return img.get("url")
    return thumbnails[0].get("url") if thumbnails else None


def build_nav_keyboard(offset: int, total: int, free_only: bool) -> InlineKeyboardMarkup:
    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page:{offset - PAGE_SIZE}"))
    if offset + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"page:{offset + PAGE_SIZE}"))
    rows = [nav_row] if nav_row else []
    rows.append([InlineKeyboardButton(
        "🆓 Только бесплатные: ВКЛ" if free_only else "🆓 Только бесплатные: ВЫКЛ",
        callback_data="toggle_filter")])
    rows.append([
        InlineKeyboardButton("📦 Формат", callback_data="show_formats"),
        InlineKeyboardButton("🎯 Категория", callback_data="show_categories"),
    ])
    return InlineKeyboardMarkup(rows)


async def send_page(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    query = context.user_data.get("query")
    offset = context.user_data.get("offset", 0)
    free_only = context.user_data.get("free_only", False)
    results = context.user_data.get("results", [])
    page_items = results[offset:offset + PAGE_SIZE]
    if not page_items:
        await context.bot.send_message(chat_id, "Модели закончились 🤷")
        return

    for i, model in enumerate(page_items, start=offset + 1):
        caption = build_caption(model, i)
        site_url = model.get("viewerUrl", "")
        preview_url = get_preview_url(model)
        open_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Открыть", url=site_url)]]) if site_url else None
        try:
            if preview_url:
                await context.bot.send_photo(chat_id=chat_id, photo=preview_url, caption=caption,
                                             parse_mode="HTML", reply_markup=open_keyboard)
            else:
                await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML",
                                               reply_markup=open_keyboard)
            await asyncio.sleep(0.2)
        except Exception as exc:
            logger.warning("Ошибка отправки результата: %s", type(exc).__name__)
            try:
                await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")
            except Exception:
                return

    shown_to = min(offset + PAGE_SIZE, len(results))
    await context.bot.send_message(
        chat_id,
        f"Показаны {offset + 1}–{shown_to} из {len(results)} по запросу «{html.escape(str(query))}».",
        reply_markup=build_nav_keyboard(offset, len(results), free_only),
    )


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    query_text, typed_format = extract_and_normalize_query(text)
    selected_format = context.user_data.get("selected_format")
    category = context.user_data.get("selected_category")
    format_name = typed_format or selected_format
    if not query_text:
        await update.message.reply_text("Напишите, что искать, например: найти STL кота")
        return

    free_only = context.user_data.get("free_only", False)
    context.user_data["selected_format"] = format_name
    format_label = f" [{format_name.upper()}]" if format_name else ""
    category_label = f" + 🎯 {category}" if category else ""
    await update.message.reply_text(f"🔍 Ищу: «{query_text}»{format_label}{category_label}...")

    effective_query = f"{category} {query_text}" if category and category != "other" else query_text

    # Format searches stay on the multi-provider service. Sketchfab is a text-catalog fallback only.
    results = await fetch_search_service(effective_query, format_name, category)
    source_mode = "search-service"
    if not results and not format_name:
        results = await fetch_sketchfab(effective_query, free_only)
        source_mode = "sketchfab"

    if not results:
        suffix = f" в формате {format_name.upper()}" if format_name else ""
        await update.message.reply_text(
            f"Ничего не найдено для «{query_text}»{suffix}.\nПопробуйте другое название или формат."
        )
        return

    if source_mode == "search-service":
        results = [normalize_service_model(x, i) for i, x in enumerate(results, 1)]

    context.user_data.update(
        query=query_text,
        results=results,
        offset=0,
        free_only=free_only,
        format_name=format_name,
        category_name=category,
        source_mode=source_mode,
    )
    await send_page(context, update.effective_chat.id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    await run_search(update, context, text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    callback = update.callback_query
    await callback.answer()
    data = callback.data or ""
    chat_id = callback.message.chat_id

    if data == "show_formats":
        await context.bot.send_message(chat_id, "📦 <b>Выберите формат 3D-модели:</b>",
                                        parse_mode="HTML", reply_markup=format_keyboard())
        return
    if data == "show_categories":
        await context.bot.send_message(chat_id, "🎯 <b>Выберите категорию:</b>",
                                        parse_mode="HTML", reply_markup=category_keyboard())
        return
    if data == "clear_filters":
        context.user_data.pop("selected_format", None)
        context.user_data.pop("selected_category", None)
        await context.bot.send_message(chat_id, "✅ Формат и категория сброшены. Напишите, что искать.")
        return
    if data.startswith("format:"):
        value = data.split(":", 1)[1]
        if value in {"clear", "any"}:
            context.user_data.pop("selected_format", None)
            await context.bot.send_message(chat_id, "✅ Ограничение по формату снято. Напишите, что искать.")
        else:
            context.user_data["selected_format"] = value
            await context.bot.send_message(chat_id,
                                            f"✅ Выбран формат <b>{value.upper()}</b>. Теперь напишите запрос.",
                                            parse_mode="HTML")
        return
    if data.startswith("category:"):
        value = data.split(":", 1)[1]
        if value == "clear":
            context.user_data.pop("selected_category", None)
            await context.bot.send_message(chat_id, "✅ Категория сброшена. Теперь напишите запрос.")
        else:
            context.user_data["selected_category"] = value
            await context.bot.send_message(chat_id,
                                            f"✅ Выбрана категория <b>{value}</b>. Теперь напишите запрос.",
                                            parse_mode="HTML")
        return
    if data.startswith("page:"):
        try:
            context.user_data["offset"] = max(0, int(data.split(":", 1)[1]))
        except ValueError:
            context.user_data["offset"] = 0
        await send_page(context, chat_id)
        return
    if data == "toggle_filter":
        current_query = context.user_data.get("query")
        if not current_query:
            await context.bot.send_message(chat_id, "Сначала отправьте поисковый запрос.")
            return
        free_only = not context.user_data.get("free_only", False)
        context.user_data["free_only"] = free_only
        await context.bot.send_message(chat_id, "🔄 Обновляю поиск...")
        format_name = context.user_data.get("format_name")
        category = context.user_data.get("category_name")
        effective_query = f"{category} {current_query}" if category and category != "other" else current_query
        results = await fetch_search_service(effective_query, format_name, category)
        if not results and not format_name:
            results = await fetch_sketchfab(effective_query, free_only)
        if not results:
            await context.bot.send_message(chat_id, "Ничего не найдено с этим фильтром.")
            return
        context.user_data["results"] = [normalize_service_model(x, i) for i, x in enumerate(results, 1)] if format_name else results
        context.user_data["offset"] = 0
        await send_page(context, chat_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if error:
        logger.error("Telegram handler error: %s", type(error).__name__)
    if update is not None:
        logger.debug("Update processing failed for %s", type(update).__name__)


def build_application() -> Application:
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)
    return app
