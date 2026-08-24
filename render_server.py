import asyncio
import hashlib
import hmac
import json
import os
import subprocess
from contextlib import suppress

from aiohttp import ClientSession, ClientTimeout, web
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

os.environ.setdefault("SEARCH_SERVICE_URL", "http://127.0.0.1:8787")

import bot  # noqa: E402

WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "10000")))
SEARCH_PORT = int(os.getenv("SEARCH_PORT", "8787"))
WEBHOOK_PATH = os.getenv("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
REQUIRE_WEBHOOK_SECRET = os.getenv("REQUIRE_WEBHOOK_SECRET", "1").lower() not in {"0", "false", "no"}
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
EXTERNAL_URL = PUBLIC_URL or (f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else "")
SEARCH_READY_TIMEOUT = max(5, int(os.getenv("SEARCH_READY_TIMEOUT_SECONDS", "20")))
WEBHOOK_MAX_BODY = max(32 * 1024, int(os.getenv("WEBHOOK_MAX_BODY_BYTES", str(1024 * 1024))))

if REQUIRE_WEBHOOK_SECRET and not WEBHOOK_SECRET:
    raise RuntimeError("TELEGRAM_WEBHOOK_SECRET must be configured")

application = Application.builder().token(bot.TOKEN).concurrent_updates(True).build()
application.add_handler(CommandHandler("start", bot.start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text))
application.add_handler(CallbackQueryHandler(bot.handle_callback))
application.add_error_handler(bot.error_handler)

search_process = None
search_watchdog_task = None
telegram_ready = asyncio.Event()
telegram_init_task = None
_recent_updates: dict[int, float] = {}
_RECENT_UPDATES_MAX = 2048
_RECENT_UPDATES_TTL = 3600


def start_search_service():
    global search_process
    if search_process is not None and search_process.poll() is None:
        return
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


async def wait_for_search_service() -> bool:
    deadline = asyncio.get_running_loop().time() + SEARCH_READY_TIMEOUT
    url = f"http://127.0.0.1:{SEARCH_PORT}/health"
    while asyncio.get_running_loop().time() < deadline:
        if search_process is None or search_process.poll() is not None:
            start_search_service()
        try:
            async with ClientSession(timeout=ClientTimeout(total=2)) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json(content_type=None)
                        if data.get("ok"):
                            return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def supervise_search_service():
    while True:
        await asyncio.sleep(10)
        if search_process is None or search_process.poll() is not None:
            print("Search service stopped; restarting", flush=True)
            start_search_service()
            if await wait_for_search_service():
                print("Search service recovered", flush=True)
            else:
                print("Search service restart did not become ready", flush=True)


async def health(_request: web.Request) -> web.Response:
    search_ok = search_process is not None and search_process.poll() is None
    return web.json_response({
        "ok": True,
        "service": "3d-model-finder-bot",
        "telegram_ready": telegram_ready.is_set(),
        "search_service": search_ok,
    })


def _remember_update(update_id: int) -> bool:
    now = asyncio.get_running_loop().time()
    for key, timestamp in list(_recent_updates.items()):
        if now - timestamp >= _RECENT_UPDATES_TTL:
            _recent_updates.pop(key, None)
    if update_id in _recent_updates:
        return False
    _recent_updates[update_id] = now
    while len(_recent_updates) > _RECENT_UPDATES_MAX:
        oldest = min(_recent_updates, key=_recent_updates.get)
        _recent_updates.pop(oldest, None)
    return True


async def webhook(request: web.Request) -> web.Response:
    if REQUIRE_WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            raise web.HTTPUnauthorized(text="invalid webhook secret")

    if not telegram_ready.is_set():
        return web.json_response({"ok": False, "error": "telegram bot is still initializing"}, status=503)

    try:
        payload = await request.json(loads=json.loads)
        update_id = int(payload.get("update_id", -1))
        if update_id >= 0 and not _remember_update(update_id):
            return web.json_response({"ok": True, "duplicate": True})
        update = Update.de_json(payload, application.bot)
        if update is None:
            raise ValueError("invalid Telegram update")
        if update_id >= 0:
            print(f"Telegram update received: {update_id}", flush=True)
        await application.process_update(update)
        return web.json_response({"ok": True})
    except web.HTTPException:
        raise
    except Exception as exc:
        logger = __import__("logging").getLogger(__name__)
        logger.error("Webhook processing error: %s", type(exc).__name__)
        return web.json_response({"ok": False, "error": "webhook processing failed"}, status=500)


async def configure_telegram():
    try:
        await application.initialize()
        await application.start()
        webhook_url = f"{EXTERNAL_URL}{WEBHOOK_PATH}"
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        telegram_ready.set()
        print(f"Telegram webhook configured: {webhook_url}", flush=True)
        print("Telegram bot is ready to receive updates", flush=True)
    except Exception as exc:
        print(f"Telegram initialization error: {type(exc).__name__}", flush=True)
        raise


async def on_startup(_app: web.Application):
    global telegram_init_task, search_watchdog_task
    if not EXTERNAL_URL:
        raise RuntimeError("No public URL configured. Set PUBLIC_URL or assign a Railway public domain.")
    start_search_service()
    if not await wait_for_search_service():
        raise RuntimeError("Search service failed readiness check")
    search_watchdog_task = asyncio.create_task(supervise_search_service())
    telegram_init_task = asyncio.create_task(configure_telegram())
    print(f"Web server listening on port {WEB_PORT}; search service on {SEARCH_PORT}", flush=True)


async def on_cleanup(_app: web.Application):
    if search_watchdog_task is not None:
        search_watchdog_task.cancel()
        with suppress(asyncio.CancelledError):
            await search_watchdog_task
    if telegram_init_task is not None and not telegram_init_task.done():
        telegram_init_task.cancel()
        with suppress(asyncio.CancelledError):
            await telegram_init_task
    with suppress(Exception):
        await application.bot.delete_webhook(drop_pending_updates=False)
    with suppress(Exception):
        await application.stop()
    with suppress(Exception):
        await application.shutdown()
    stop_search_service()


app = web.Application(client_max_size=WEBHOOK_MAX_BODY)
app.router.add_get("/", health)
app.router.add_get("/health", health)
app.router.add_post(WEBHOOK_PATH, webhook)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=WEB_PORT)
