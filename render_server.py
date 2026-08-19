import asyncio
import os
import signal
import subprocess
from contextlib import suppress

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

# The search service stays private inside the same Render container.
os.environ.setdefault("SEARCH_SERVICE_URL", "http://127.0.0.1:8787")

import bot  # noqa: E402

WEB_PORT = int(os.getenv("PORT", "10000"))
SEARCH_PORT = int(os.getenv("SEARCH_PORT", "8787"))
WEBHOOK_PATH = os.getenv("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

application = Application.builder().token(bot.TOKEN).build()
application.add_handler(CommandHandler("start", bot.start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text))
application.add_handler(CallbackQueryHandler(bot.handle_callback))

search_process = None


def start_search_service():
    global search_process
    env = os.environ.copy()
    env["PORT"] = str(SEARCH_PORT)
    search_process = subprocess.Popen(
        ["node", "search-service/server.js"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        stdout=None,
        stderr=None,
    )


def stop_search_service():
    global search_process
    if search_process is None or search_process.poll() is not None:
        return
    with suppress(Exception):
        search_process.terminate()
    try:
        search_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with suppress(Exception):
            search_process.kill()


async def health(_request: web.Request) -> web.Response:
    search_ok = search_process is not None and search_process.poll() is None
    return web.json_response({
        "ok": search_ok,
        "service": "3d-model-finder-bot",
        "telegram": True,
        "search_service": search_ok,
        "search_service_url": f"http://127.0.0.1:{SEARCH_PORT}",
    })


async def webhook(request: web.Request) -> web.Response:
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            raise web.HTTPUnauthorized(text="invalid webhook secret")

    try:
        payload = await request.json()
        update = Update.de_json(payload, application.bot)
        if update is None:
            raise ValueError("invalid Telegram update")
        await application.process_update(update)
        return web.json_response({"ok": True})
    except Exception as exc:
        print(f"Webhook error: {exc}", flush=True)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def on_startup(_app: web.Application):
    start_search_service()
    await asyncio.sleep(1)

    if not EXTERNAL_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL is not available; webhook cannot be configured")

    await application.initialize()
    await application.start()

    webhook_url = f"{EXTERNAL_URL}{WEBHOOK_PATH}"
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    print(f"Telegram webhook configured: {webhook_url}", flush=True)


async def on_cleanup(_app: web.Application):
    with suppress(Exception):
        await application.bot.delete_webhook(drop_pending_updates=False)
    with suppress(Exception):
        await application.stop()
    with suppress(Exception):
        await application.shutdown()
    stop_search_service()


app = web.Application()
app.router.add_get("/", health)
app.router.add_get("/health", health)
app.router.add_post(WEBHOOK_PATH, webhook)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=WEB_PORT)
