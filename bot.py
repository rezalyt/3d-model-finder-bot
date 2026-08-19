import os
import logging
import aiohttp
import asyncio
import re
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
if not TOKEN or not SKETCHFAB_TOKEN:
    raise ValueError("BOT_TOKEN и SKETCHFAB_API_TOKEN должны быть в .env")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

PAGE_SIZE = 5
FETCH_LIMIT = 24
CACHE_TTL = 60 * 30
search_cache: dict[tuple[str, bool, str], dict] = {}

# Форматы, которые понимаем в естественном запросе.
FORMAT_ALIASES = {
    "glb": "glb", "gltf": "gltf", "obj": "obj", "stl": "stl",
    "fbx": "fbx", "blend": "blend", "usd": "usd", "usdz": "usdz",
    "3mf": "3mf", "dae": "dae", "ply": "ply", "step": "step",
    "stp": "step", "iges": "iges", "igs": "iges",
}
FORMAT_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, FORMAT_ALIASES)) + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def extract_format(text: str) -> tuple[str, str]:
    """Возвращает (поисковый текст без формата, формат)."""
    match = FORMAT_PATTERN.search(text)
    if not match:
        return text.strip(), ""
    raw = match.group(0).lower()
    fmt = FORMAT_ALIASES[raw]
    clean = (text[:match.start()] + " " + text[match.end():]).strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean, fmt


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Привет! Я бот для поиска 3D-моделей.\n\n"
        "Можно указать формат прямо в запросе:\n"
        "• chair STL\n"
        "• Toyota Supra GLB\n"
        "• robot FBX\n\n"
        "Если формат указан, я буду возвращать только модели, у которых этот формат заявлен в данных источника."
    )


async def fetch_sketchfab(query: str, free_only: bool) -> list:
    cache_key = (query.lower(), free_only, "sketchfab")
    cached = search_cache.get(cache_key)
    if cached and (asyncio.get_event_loop().time() - cached["ts"]) < CACHE_TTL:
        logger.info(f"Кэш-хит для '{query}' (free_only={free_only})")
        return cached["results"]

    url = "https://api.sketchfab.com/v3/search"
    params = {"q": query, "type": "models", "limit": FETCH_LIMIT, "sort_by": "-relevance"}
    if free_only:
        params["downloadable"] = "true"

    headers = {"Authorization": f"Token {SKETCHFAB_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = data.get("results", [])
                search_cache[cache_key] = {"results": results, "ts": asyncio.get_event_loop().time()}
                logger.info(f"Sketchfab: найдено {len(results)} для '{query}'")
                return results
            logger.error(f"Sketchfab ошибка {resp.status}: {await resp.text()}")
            return []


async def fetch_3dfetch(query: str, fmt: str) -> list:
    if not SEARCH_SERVICE_URL:
        logger.error("SEARCH_SERVICE_URL не задан")
        return []

    cache_key = (query.lower(), False, f"3dfetch:{fmt}")
    cached = search_cache.get(cache_key)
    if cached and (asyncio.get_event_loop().time() - cached["ts"]) < CACHE_TTL:
        return cached["results"]

    params = {"q": query, "format": fmt}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{SEARCH_SERVICE_URL}/search", params=params) as resp:
            if resp.status != 200:
                logger.error(f"3dfetch service ошибка {resp.status}: {await resp.text()}")
                return []
            data = await resp.json()

    results = data.get("results", [])
    search_cache[cache_key] = {"results": results, "ts": asyncio.get_event_loop().time()}
    logger.info(f"3dfetch: найдено {len(results)} моделей для '{query}' в {fmt}")
    return results


def build_caption(model: dict, index: int, source_mode: str = "sketchfab") -> str:
    name = model.get("name", f"Модель {index}")

    if source_mode == "3dfetch":
        license_name = model.get("license") or "Не указана"
        source = model.get("source") or "Источник не указан"
        formats = model.get("formats") or []
        format_text = ", ".join(formats).upper() if formats else "Не указано"
        return (
            f"<b>{index}. {name}</b>\n"
            f"📦 Форматы: {format_text}\n"
            f"🌐 Источник: {source}\n"
            f"📜 Лицензия: {license_name}"
        )

    license_data = model.get("license")
    license_name = license_data.get("label", "Не указана") if isinstance(license_data, dict) else "Не указана"
    return f"<b>{index}. {name}</b>\n📜 Лицензия: {license_name}"


def get_preview_url(model: dict):
    # 3dfetch normalizes thumbnails to thumbnailUrl.
    if model.get("thumbnailUrl"):
        return model["thumbnailUrl"]
    thumbnails = model.get("thumbnails", {}).get("images", [])
    for img in thumbnails:
        if img.get("size") == 256:
            return img.get("url")
    if thumbnails:
        return thumbnails[0].get("url")
    return None


def get_model_url(model: dict, source_mode: str) -> str:
    if source_mode == "3dfetch":
        return model.get("sourceUrl") or model.get("downloadUrl") or ""
    return model.get("viewerUrl", "")


def build_nav_keyboard(offset: int, total: int, free_only: bool) -> InlineKeyboardMarkup:
    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page:{offset - PAGE_SIZE}"))
    if offset + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"page:{offset + PAGE_SIZE}"))

    filter_label = "🆓 Только бесплатные: ВКЛ" if free_only else "🆓 Только бесплатные: ВЫКЛ"
    rows = [nav_row] if nav_row else []
    rows.append([InlineKeyboardButton(filter_label, callback_data="toggle_filter")])
    return InlineKeyboardMarkup(rows)


async def send_page(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    query = context.user_data.get("query")
    offset = context.user_data.get("offset", 0)
    free_only = context.user_data.get("free_only", False)
    results = context.user_data.get("results", [])
    source_mode = context.user_data.get("source_mode", "sketchfab")
    fmt = context.user_data.get("format", "")

    page_items = results[offset:offset + PAGE_SIZE]
    if not page_items:
        await context.bot.send_message(chat_id, "Модели закончились 🤷")
        return

    for i, model in enumerate(page_items, start=offset + 1):
        caption = build_caption(model, i, source_mode)
        site_url = get_model_url(model, source_mode)
        preview_url = get_preview_url(model)
        open_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Открыть", url=site_url)]]) if site_url else None

        try:
            if preview_url:
                await context.bot.send_photo(chat_id=chat_id, photo=preview_url, caption=caption,
                                              parse_mode="HTML", reply_markup=open_keyboard)
            else:
                await context.bot.send_message(chat_id=chat_id, text=caption,
                                               parse_mode="HTML", reply_markup=open_keyboard)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка при отправке {model.get('name')}: {e}")
            link = f"\n🌐 <a href='{site_url}'>Открыть</a>" if site_url else ""
            await context.bot.send_message(chat_id=chat_id, text=f"{caption}{link}", parse_mode="HTML")

    nav_keyboard = build_nav_keyboard(offset, len(results), free_only)
    shown_to = min(offset + PAGE_SIZE, len(results))
    format_suffix = f" • формат: {fmt.upper()}" if fmt else ""
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Показаны {offset + 1}–{shown_to} из {len(results)} по запросу «{query}»{format_suffix}.",
        reply_markup=nav_keyboard,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Введите текст.")
        return

    query_text, fmt = extract_format(text)
    free_only = context.user_data.get("free_only", False)

    if not query_text:
        await update.message.reply_text("Укажите, какую модель искать. Например: chair STL")
        return

    if fmt and not SEARCH_SERVICE_URL:
        await update.message.reply_text(
            "⚠️ Поиск по формату уже включён в боте, но поисковый сервис 3dfetch ещё не подключён к этому окружению."
        )
        return

    label = f" в формате {fmt.upper()}" if fmt else ""
    await update.message.reply_text(f"🔍 Ищу: '{query_text}'{label}...")

    if fmt:
        results = await fetch_3dfetch(query_text, fmt)
        source_mode = "3dfetch"
    else:
        results = await fetch_sketchfab(text, free_only)
        source_mode = "sketchfab"

    if not results:
        message = f"Ничего не найдено по запросу «{query_text}»"
        if fmt:
            message += f" в формате {fmt.upper()}"
        message += ". Попробуйте другой запрос."
        await update.message.reply_text(message)
        return

    context.user_data["query"] = query_text
    context.user_data["results"] = results
    context.user_data["offset"] = 0
    context.user_data["free_only"] = free_only
    context.user_data["source_mode"] = source_mode
    context.user_data["format"] = fmt

    await send_page(context, update.effective_chat.id)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_update = update.callback_query
    await query_update.answer()
    data = query_update.data
    chat_id = query_update.message.chat_id

    if data.startswith("page:"):
        context.user_data["offset"] = int(data.split(":")[1])
        await send_page(context, chat_id)

    elif data == "toggle_filter":
        current_query = context.user_data.get("query")
        fmt = context.user_data.get("format", "")
        if not current_query:
            await context.bot.send_message(chat_id, "Сначала отправьте поисковый запрос.")
            return

        free_only = not context.user_data.get("free_only", False)
        context.user_data["free_only"] = free_only
        await context.bot.send_message(
            chat_id,
            f"🔄 Обновляю поиск ({'только бесплатные' if free_only else 'все лицензии'})..."
        )

        if fmt:
            results = await fetch_3dfetch(current_query, fmt)
        else:
            results = await fetch_sketchfab(current_query, free_only)

        if not results:
            await context.bot.send_message(chat_id, "Ничего не найдено с этим фильтром.")
            return

        context.user_data["results"] = results
        context.user_data["offset"] = 0
        await send_page(context, chat_id)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("✅ Бот запущен. Поиск по формату: 3dfetch; обычный поиск: Sketchfab.")
    app.run_polling()


if __name__ == "__main__":
    main()
