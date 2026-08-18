import os
import logging
import aiohttp
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 Привет! Я бот для поиска 3D-моделей.\n"
        "Я ищу на Sketchfab. Отправь запрос, например: 'chair', 'tree', 'robot'."
    )

async def search_sketchfab(query: str):
    # Используем эндпоинт поиска вместо /models
    url = "https://api.sketchfab.com/v3/search"
    params = {
        "q": query,
        "type": "models",
        "limit": 5,
        "sort_by": "-relevance"
    }
    headers = {"Authorization": f"Token {SKETCHFAB_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                # В ответе поиска результаты могут быть в поле "results"
                results = data.get("results", [])
                logger.info(f"Найдено {len(results)} моделей для '{query}'")
                if results:
                    names = [r.get("name", "без имени") for r in results[:5]]
                    logger.info(f"Названия: {names}")
                return results
            else:
                logger.error(f"Ошибка: {resp.status} - {await resp.text()}")
                return []

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if not query:
        await update.message.reply_text("Введите текст.")
        return

    await update.message.reply_text(f"🔍 Ищу по запросу: '{query}'...")

    results = await search_sketchfab(query)
    if not results:
        await update.message.reply_text("Ничего не найдено.")
        return

    for i, model in enumerate(results[:5], start=1):
        name = model.get("name", f"Модель {i}")
        site_url = model.get("viewerUrl", "")
        license_data = model.get("license")
        license_name = license_data.get("label", "Не указана") if isinstance(license_data, dict) else "Не указана"
        thumbnails = model.get("thumbnails", {}).get("images", [])
        preview_url = None
        for img in thumbnails:
            if img.get("size") == 256:
                preview_url = img.get("url")
                break
        if not preview_url and thumbnails:
            preview_url = thumbnails[0].get("url")

        caption = f"<b>{i}. {name}</b>\n📜 Лицензия: {license_name}"
        keyboard = [[InlineKeyboardButton("🌐 Открыть", url=site_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            if preview_url:
                await update.message.reply_photo(
                    photo=preview_url,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка при отправке {name}: {e}")
            await update.message.reply_text(
                f"<b>{i}. {name}</b>\n🌐 <a href='{site_url}'>Открыть</a>",
                parse_mode="HTML"
            )

    await update.message.reply_text(f"✅ Показано {min(5, len(results))} моделей по запросу '{query}'.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("✅ Бот запущен с поисковым эндпоинтом Sketchfab.")
    app.run_polling()

if __name__ == "__main__":
    main()