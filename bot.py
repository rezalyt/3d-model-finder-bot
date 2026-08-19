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
if not TOKEN or not SKETCHFAB_TOKEN:
    raise ValueError("BOT_TOKEN и SKETCHFAB_API_TOKEN должны быть в .env")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Настройки пагинации и кэша
# ---------------------------------------------------------------------------
PAGE_SIZE = 5           # сколько моделей показываем за раз
FETCH_LIMIT = 24        # сколько моделей запрашиваем у Sketchfab за один поиск
CACHE_TTL = 60 * 30     # 30 минут — сколько храним результаты в памяти

# Простой in-memory кэш: {(query, free_only): {"results": [...], "ts": время}}
# Важно: этот кэш живёт только пока бот запущен. Если бот перезапустится
# (например, при новом деплое на Railway), кэш очистится — это нормально
# для старта. Позже можно заменить на Redis, если понадобится кэш между
# перезапусками.
search_cache: dict[tuple[str, bool], dict] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Привет! Я бот для поиска 3D-моделей.\n"
        "Я ищу на Sketchfab. Отправь запрос, например: 'chair', 'tree', 'robot'.\n\n"
        "После поиска можно листать результаты кнопками и включать фильтр "
        "«только бесплатные»."
    )


async def fetch_sketchfab(query: str, free_only: bool) -> list:
    """Запрашивает модели у Sketchfab API. Использует кэш, если он свежий."""
    cache_key = (query.lower(), free_only)
    cached = search_cache.get(cache_key)
    if cached and (asyncio.get_event_loop().time() - cached["ts"]) < CACHE_TTL:
        logger.info(f"Кэш-хит для '{query}' (free_only={free_only})")
        return cached["results"]

    url = "https://api.sketchfab.com/v3/search"
    params = {
        "q": query,
        "type": "models",
        "limit": FETCH_LIMIT,
        "sort_by": "-relevance",
    }
    if free_only:
        params["downloadable"] = "true"

    headers = {"Authorization": f"Token {SKETCHFAB_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = data.get("results", [])
                logger.info(f"Найдено {len(results)} моделей для '{query}' (free_only={free_only})")
                search_cache[cache_key] = {
                    "results": results,
                    "ts": asyncio.get_event_loop().time(),
                }
                return results
            else:
                logger.error(f"Ошибка: {resp.status} - {await resp.text()}")
                return []


def build_caption(model: dict, index: int) -> str:
    name = model.get("name", f"Модель {index}")
    license_data = model.get("license")
    license_name = license_data.get("label", "Не указана") if isinstance(license_data, dict) else "Не указана"
    return f"<b>{index}. {name}</b>\n📜 Лицензия: {license_name}"


def get_preview_url(model: dict):
    thumbnails = model.get("thumbnails", {}).get("images", [])
    for img in thumbnails:
        if img.get("size") == 256:
            return img.get("url")
    if thumbnails:
        return thumbnails[0].get("url")
    return None


def build_nav_keyboard(offset: int, total: int, free_only: bool) -> InlineKeyboardMarkup:
    """Строит клавиатуру: Назад / Вперёд / Фильтр."""
    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page:{offset - PAGE_SIZE}"))
    if offset + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"page:{offset + PAGE_SIZE}"))

    filter_label = "🆓 Только бесплатные: ВКЛ" if free_only else "🆓 Только бесплатные: ВЫКЛ"
    filter_row = [InlineKeyboardButton(filter_label, callback_data="toggle_filter")]

    rows = []
    if nav_row:
        rows.append(nav_row)
    rows.append(filter_row)
    return InlineKeyboardMarkup(rows)


async def send_page(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Отправляет текущую страницу результатов (5 моделей + клавиатура навигации)."""
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

        # Кнопка "Открыть" — отдельно от навигации, под каждой моделью
        open_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Открыть", url=site_url)]])

        try:
            if preview_url:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=preview_url,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=open_keyboard,
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=open_keyboard,
                )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка при отправке {model.get('name')}: {e}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{caption}\n🌐 <a href='{site_url}'>Открыть</a>",
                parse_mode="HTML",
            )

    # Одно сообщение-навигатор внизу страницы
    nav_keyboard = build_nav_keyboard(offset, len(results), free_only)
    shown_to = min(offset + PAGE_SIZE, len(results))
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Показаны {offset + 1}–{shown_to} из {len(results)} по запросу «{query}».",
        reply_markup=nav_keyboard,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Введите текст.")
        return

    free_only = context.user_data.get("free_only", False)

    await update.message.reply_text(f"🔍 Ищу по запросу: '{text}'...")

    results = await fetch_sketchfab(text, free_only)
    if not results:
        await update.message.reply_text("Ничего не найдено. Попробуйте другой запрос или отключите фильтр.")
        return

    # Сохраняем состояние поиска для этого пользователя
    context.user_data["query"] = text
    context.user_data["results"] = results
    context.user_data["offset"] = 0
    context.user_data["free_only"] = free_only

    await send_page(context, update.effective_chat.id)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_update = update.callback_query
    await query_update.answer()  # обязательно, иначе кнопка будет "крутиться"

    data = query_update.data
    chat_id = query_update.message.chat_id

    if data.startswith("page:"):
        new_offset = int(data.split(":")[1])
        context.user_data["offset"] = new_offset
        await send_page(context, chat_id)

    elif data == "toggle_filter":
        current_query = context.user_data.get("query")
        if not current_query:
            await context.bot.send_message(chat_id, "Сначала отправьте поисковый запрос.")
            return

        # Переключаем фильтр и делаем новый поиск с текущим запросом
        free_only = not context.user_data.get("free_only", False)
        context.user_data["free_only"] = free_only

        await context.bot.send_message(
            chat_id,
            f"🔄 Обновляю поиск ({'только бесплатные' if free_only else 'все лицензии'})..."
        )

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
    print("✅ Бот запущен с пагинацией, фильтром и кэшем.")
    app.run_polling()


if __name__ == "__main__":
    main()
