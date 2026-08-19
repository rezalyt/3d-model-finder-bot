import os
import logging
import aiohttp
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
SKETCHFAB_TOKEN = os.getenv("SKETCHFAB_API_TOKEN")
SEARCH_SERVICE_URL = os.getenv("SEARCH_SERVICE_URL", "").rstrip("/")
if not TOKEN:
    raise ValueError("BOT_TOKEN должен быть в .env")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

PAGE_SIZE = 5
FETCH_LIMIT = 24
CACHE_TTL = 60 * 30
search_cache: dict[tuple[str, bool, str | None, str | None], dict] = {}

FORMAT_ALIASES = {
    "gltf": "gltf", "glb": "glb", "obj": "obj", "fbx": "fbx", "blend": "blend",
    "usd": "usd", "usdz": "usdz", "stl": "stl", "3mf": "3mf", "dae": "dae",
    "ply": "ply", "step": "step", "stp": "step", "iges": "iges", "igs": "iges",
    "off": "off", "max": "max", "c4d": "c4d", "abc": "abc", "3ds": "3ds",
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


def format_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(FORMAT_BUTTONS), 4):
        rows.append([
            InlineKeyboardButton(label, callback_data=f"format:{value}")
            for label, value in FORMAT_BUTTONS[i:i + 4]
        ])
    rows.append([InlineKeyboardButton("🔎 Любой формат", callback_data="format:any")])
    rows.append([InlineKeyboardButton("❌ Отменить выбор", callback_data="format:clear")])
    return InlineKeyboardMarkup(rows)


def category_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(CATEGORY_BUTTONS), 2):
        rows.append([
            InlineKeyboardButton(label, callback_data=f"category:{value}")
            for label, value in CATEGORY_BUTTONS[i:i + 2]
        ])
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
        "Выберите формат и категорию или просто напишите запрос.\n\n"
        "Примеры:\n"
        "• tree\n"
        "• tree FBX\n"
        "• chair OBJ\n"
        "• robot GLB"
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
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(f"{SEARCH_SERVICE_URL}/search", params=params) as resp:
                if resp.status != 200:
                    logger.error("Search service HTTP %s: %s", resp.status, await resp.text())
                    return []
                data = await resp.json()
                return data.get("results", [])
    except Exception as exc:
        logger.error("Search service error: %s", exc)
        return []


async def fetch_sketchfab(query: str, free_only: bool, format_name: str | None = None) -> list:
    if not SKETCHFAB_TOKEN:
        return []
    cache_key = (query.lower(), free_only, format_name, None)
    cached = search_cache.get(cache_key)
    if cached and (asyncio.get_event_loop().time() - cached["ts"]) < CACHE_TTL:
        return cached["results"]

    params = {"q": query, "type": "models", "limit": FETCH_LIMIT, "sort_by": "-relevance"}
    if free_only:
        params["downloadable"] = "true"
    headers = {"Authorization": f"Token {SKETCHFAB_TOKEN}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.get("https://api.sketchfab.com/v3/search", params=params, headers=headers) as resp:
                if resp.status != 200:
                    logger.error("Sketchfab HTTP %s: %s", resp.status, await resp.text())
                    return []
                raw = (await resp.json()).get("results", [])
        results = []
        terms = [x.lower() for x in query.split() if x.strip()]
        for item in raw:
            formats = {str(x).lower() for x in (item.get("formats") or [])}
            haystack = " ".join([
                str(item.get("name", "")), str(item.get("description", "")),
                " ".join(str(x) for x in (item.get("tags") or [])),
            ]).lower()
            if format_name and format_name.lower() not in formats:
                continue
            if terms and not any(term in haystack for term in terms):
                continue
            results.append(item)
        search_cache[cache_key] = {"results": results, "ts": asyncio.get_event_loop().time()}
        return results
    except Exception as exc:
        logger.error("Sketchfab error: %s", exc)
        return []


def extract_format(text: str) -> tuple[str, str | None]:
    parts = text.strip().split()
    if parts:
        key = parts[-1].lower().lstrip(".")
        if key in FORMAT_ALIASES:
            return " ".join(parts[:-1]).strip(), FORMAT_ALIASES[key]
    return text.strip(), None


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
    name = model.get("name", f"Модель {index}")
    license_data = model.get("license")
    if isinstance(license_data, dict):
        license_name = license_data.get("label", "Не указана")
    else:
        license_name = license_data or "Не указана"
    formats = model.get("formats") or []
    extra = f"\n📦 Форматы: {', '.join(str(x).upper() for x in formats)}" if formats else ""
    provider = f"\n🔎 Источник: {model.get('source')}" if model.get("source") else ""
    access = f"\n🔐 Доступ: {model['access']}" if model.get("access") else ""
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
            await asyncio.sleep(0.35)
        except Exception as exc:
            logger.error("Ошибка отправки %s: %s", model.get("name"), exc)
            await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")

    shown_to = min(offset + PAGE_SIZE, len(results))
    await context.bot.send_message(
        chat_id,
        f"Показаны {offset + 1}–{shown_to} из {len(results)} по запросу «{query}».",
        reply_markup=build_nav_keyboard(offset, len(results), free_only),
    )


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    typed_query, typed_format = extract_format(text)
    selected_format = context.user_data.get("selected_format")
    category = context.user_data.get("selected_category")
    format_name = typed_format or selected_format
    query_text = typed_query if typed_format else text.strip()
    if not query_text:
        await update.message.reply_text("Напишите, что искать, например: tree")
        return

    free_only = context.user_data.get("free_only", False)
    context.user_data["selected_format"] = format_name
    format_label = f" [{format_name.upper()}]" if format_name else ""
    category_label = f" + 🎯 {category}" if category else ""
    await update.message.reply_text(f"🔍 Ищу: «{query_text}»{format_label}{category_label}...")

    # Category is currently a semantic hint; provider-native category filters can be added later.
    effective_query = f"{category} {query_text}" if category and category != "other" else query_text
    results = await fetch_search_service(effective_query, format_name, category)

    if not results:
        results = await fetch_sketchfab(effective_query, free_only, format_name)
        if results:
            results = [normalize_service_model(x, i) for i, x in enumerate(results, 1)]

    if not results:
        await update.message.reply_text(
            f"Ничего не найдено для «{query_text}»{format_label}.\n"
            "Попробуйте другое название или формат."
        )
        return

    if not all("viewerUrl" in x for x in results):
        results = [normalize_service_model(x, i) for i, x in enumerate(results, 1)]

    context.user_data.update(
        query=query_text,
        results=results,
        offset=0,
        free_only=free_only,
        format_name=format_name,
        category_name=category,
    )
    await send_page(context, update.effective_chat.id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text:
        await run_search(update, context, text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    callback = update.callback_query
    await callback.answer()
    data = callback.data
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
        if value == "clear" or value == "any":
            context.user_data.pop("selected_format", None)
            await context.bot.send_message(chat_id, "✅ Ограничение по формату снято. Напишите, что искать.")
            return
        context.user_data["selected_format"] = value
        await context.bot.send_message(
            chat_id,
            f"✅ Выбран формат <b>{value.upper()}</b>. Теперь напишите, что искать, например: <b>tree</b>",
            parse_mode="HTML",
        )
        return

    if data.startswith("category:"):
        value = data.split(":", 1)[1]
        if value == "clear":
            context.user_data.pop("selected_category", None)
            await context.bot.send_message(chat_id, "✅ Категория сброшена. Теперь напишите запрос.")
            return
        context.user_data["selected_category"] = value
        await context.bot.send_message(
            chat_id,
            f"✅ Выбрана категория <b>{value}</b>. Теперь напишите, что искать, например: <b>chair</b>",
            parse_mode="HTML",
        )
        return

    if data.startswith("page:"):
        context.user_data["offset"] = int(data.split(":")[1])
        await send_page(context, chat_id)
        return

    if data == "toggle_filter":
        current_query = context.user_data.get("query")
        if not current_query:
            await context.bot.send_message(chat_id, "Сначала отправьте поисковый запрос.")
            return
        free_only = not context.user_data.get("free_only", False)
        context.user_data["free_only"] = free_only
        format_name = context.user_data.get("format_name")
        category = context.user_data.get("category_name")
        await context.bot.send_message(chat_id, "🔄 Обновляю поиск...")
        effective_query = f"{category} {current_query}" if category and category != "other" else current_query
        results = await fetch_search_service(effective_query, format_name, category)
        if not results:
            results = await fetch_sketchfab(effective_query, free_only, format_name)
            results = [normalize_service_model(x, i) for i, x in enumerate(results, 1)]
        context.user_data["results"] = results
        context.user_data["offset"] = 0
        if results:
            await send_page(context, chat_id)
        else:
            await context.bot.send_message(chat_id, "Ничего не найдено с этим фильтром.")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("✅ 3D Model Finder: multi-provider format search + category filter.")
    app.run_polling()


if __name__ == "__main__":
    main()
