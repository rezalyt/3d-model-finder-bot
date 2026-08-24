import asyncio
import html
import logging
import mimetypes
import os
import re
import tempfile
import time
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

import bot as base_bot

logger = logging.getLogger(__name__)

PAGE_SIZE = max(3, min(5, int(os.getenv("RESULTS_PAGE_SIZE", "4"))))
DOWNLOAD_DAILY_LIMIT = max(1, int(os.getenv("DOWNLOADS_PER_DAY", "10")))
DOWNLOAD_MAX_BYTES = min(50 * 1024 * 1024, max(1 * 1024 * 1024, int(os.getenv("DOWNLOAD_MAX_BYTES", str(50 * 1024 * 1024)))))
DOWNLOAD_MAX_USERS = max(128, int(os.getenv("DOWNLOAD_MAX_USERS", "4096")))
DOWNLOAD_TIMEOUT = max(10, int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "60")))
DOWNLOAD_CHUNK_SIZE = 256 * 1024

_download_usage: OrderedDict[int, deque[float]] = OrderedDict()


def _cleanup_download_usage(now: float) -> None:
    day = 24 * 60 * 60
    for user_id, timestamps in list(_download_usage.items()):
        while timestamps and now - timestamps[0] >= day:
            timestamps.popleft()
        if not timestamps:
            _download_usage.pop(user_id, None)
    while len(_download_usage) > DOWNLOAD_MAX_USERS:
        _download_usage.popitem(last=False)


def consume_download_slot(user_id: int | None) -> tuple[bool, int]:
    if user_id is None:
        return False, 0
    now = time.time()
    _cleanup_download_usage(now)
    timestamps = _download_usage.setdefault(user_id, deque())
    timestamps.append(now)
    _download_usage.move_to_end(user_id)
    if len(timestamps) > DOWNLOAD_DAILY_LIMIT:
        timestamps.pop()
        remaining = DOWNLOAD_DAILY_LIMIT - len(timestamps)
        return False, remaining
    return True, DOWNLOAD_DAILY_LIMIT - len(timestamps)


def safe_filename(model: dict, url: str) -> str:
    raw_name = str(model.get("name") or "model").strip()
    raw_name = re.sub(r"[^\w\-. ]+", "_", raw_name, flags=re.UNICODE).strip(" ._") or "model"
    parsed = urlparse(url)
    suffix = Path(unquote(parsed.path)).suffix.lower()
    if suffix not in {".zip", ".glb", ".gltf", ".obj", ".fbx", ".stl", ".3mf", ".blend", ".usd", ".usdz", ".ply", ".dae", ".step", ".iges", ".3ds"}:
        suffix = ".bin"
    return f"{raw_name[:96]}{suffix}"


def model_download_url(model: dict) -> str | None:
    value = model.get("downloadUrl") or model.get("download_url")
    if not value:
        return None
    url = str(value).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return None
    return url


def _card_keyboard(model: dict, index: int) -> InlineKeyboardMarkup:
    buttons = []
    download_url = model_download_url(model)
    if download_url:
        buttons.append(InlineKeyboardButton("⬇️ Скачать", callback_data=f"download:{index}"))
    source_url = str(model.get("viewerUrl") or model.get("sourceUrl") or "").strip()
    if source_url.startswith("http://") or source_url.startswith("https://"):
        buttons.append(InlineKeyboardButton("ℹ️ Подробнее", url=source_url))
    return InlineKeyboardMarkup([buttons]) if buttons else InlineKeyboardMarkup([])


def _size_label(model: dict) -> str:
    metadata = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
    raw = model.get("size") or model.get("fileSize") or model.get("downloadSize") or metadata.get("size") or metadata.get("fileSize")
    try:
        size = float(raw)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def build_quality_caption(model: dict, index: int) -> str:
    name = html.escape(str(model.get("name") or f"Модель {index}"))
    source = html.escape(str(model.get("source") or "unknown"))
    license_name = html.escape(str(model.get("license") or "Не указана"))
    formats = model.get("formats") or []
    format_text = ", ".join(html.escape(str(value).upper()) for value in formats[:8]) or "—"
    quality = model.get("qualityScore")
    quality_text = f"\n⭐ Качество: {float(quality):.0f}/100" if isinstance(quality, (int, float)) else ""
    size = _size_label(model)
    size_text = f"\n💾 Размер: {html.escape(size)}" if size else ""
    author = model.get("author")
    author_name = author.get("name") if isinstance(author, dict) else None
    author_text = f"\n👤 Автор: {html.escape(str(author_name))}" if author_name else ""
    return (
        f"<b>{index}. {name}</b>\n"
        f"📦 Форматы: {format_text}\n"
        f"🔎 Источник: {source}\n"
        f"📜 Лицензия: {license_name}"
        f"{quality_text}{size_text}{author_text}"
    )


def preview_url(model: dict) -> str | None:
    return model.get("thumbnailUrl") or model.get("thumbnail")


def normalize_card_model(model: dict, index: int) -> dict:
    normalized = dict(model or {})
    normalized["name"] = normalized.get("name") or f"Модель {index}"
    normalized["source"] = normalized.get("source") or "unknown"
    normalized["viewerUrl"] = normalized.get("sourceUrl") or normalized.get("viewerUrl") or ""
    normalized["thumbnail"] = normalized.get("thumbnailUrl") or normalized.get("thumbnail")
    normalized["formats"] = normalized.get("formats") or []
    return normalized


async def _fetch_results(query: str, format_name: str | None, category: str | None) -> list:
    effective_query = f"{category} {query}" if category and category != "other" else query
    return await base_bot.fetch_search_service(effective_query, format_name, category)


async def send_page(context, chat_id: int) -> None:
    results = context.user_data.get("results") or []
    offset = max(0, int(context.user_data.get("offset", 0)))
    query = str(context.user_data.get("query") or "")
    if not results:
        await context.bot.send_message(chat_id, "Модели закончились 🤷")
        return

    page_items = results[offset:offset + PAGE_SIZE]
    for position, raw_model in enumerate(page_items, start=offset + 1):
        model = normalize_card_model(raw_model, position)
        caption = build_quality_caption(model, position)
        photo = preview_url(model)
        markup = _card_keyboard(model, position - 1)
        try:
            if photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
        except Exception as exc:
            logger.warning("Result card send failed: %s", type(exc).__name__)
            try:
                await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass

    last = min(offset + PAGE_SIZE, len(results))
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page:{max(0, offset - PAGE_SIZE)}"))
    if last < len(results):
        nav.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"page:{offset + PAGE_SIZE}"))
    rows = [nav] if nav else []
    rows.append([
        InlineKeyboardButton("📦 Формат", callback_data="show_formats"),
        InlineKeyboardButton("🎯 Категория", callback_data="show_categories"),
    ])
    await context.bot.send_message(
        chat_id,
        f"Страница {offset // PAGE_SIZE + 1}/{(len(results) + PAGE_SIZE - 1) // PAGE_SIZE} · показаны {offset + 1}–{last} из {len(results)}\n🔎 «{html.escape(query)}»",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


async def handle_text(update: Update, context) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    user_id = update.effective_user.id if update.effective_user else None
    remaining = base_bot.rate_limit_search(user_id)
    if remaining > 0:
        await update.message.reply_text(f"⏳ Слишком частые запросы. Повторите через {remaining:.1f} сек.")
        return

    query, typed_format = base_bot.extract_and_normalize_query(text)
    format_name = typed_format or context.user_data.get("selected_format")
    category = context.user_data.get("selected_category")
    if not query:
        await update.message.reply_text("Напишите, что искать, например: найти STL кота")
        return

    await update.message.reply_text(
        f"🔍 Ищу: «{html.escape(query)}" + (f"» [{format_name.upper()}]" if format_name else "»") + "…",
        parse_mode="HTML",
    )
    results = await _fetch_results(query, format_name, category)
    if not results:
        await update.message.reply_text("Ничего качественного не найдено. Попробуйте изменить запрос или формат.")
        return

    context.user_data.update(
        query=query,
        results=results,
        offset=0,
        format_name=format_name,
        category_name=category,
    )
    await send_page(context, update.effective_chat.id)


async def _download_to_temp(url: str, model: dict) -> tuple[str | None, int, str | None]:
    path = None
    try:
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
        headers = {"User-Agent": "3DModelFinderBot/1.0"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    logger.warning("Download source HTTP %s", response.status)
                    return None, 0, None
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > DOWNLOAD_MAX_BYTES:
                    return None, int(content_length), None
                suffix = Path(safe_filename(model, url)).suffix
                with tempfile.NamedTemporaryFile(prefix="3dmodel_", suffix=suffix, delete=False) as temp:
                    path = temp.name
                    total = 0
                    while True:
                        chunk = await response.content.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > DOWNLOAD_MAX_BYTES:
                            return None, total, None
                        temp.write(chunk)
                    return path, total, safe_filename(model, url)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as exc:
        logger.warning("Model download failed: %s", type(exc).__name__)
        return None, 0, None
    finally:
        # Caller owns a successful file; failed paths are removed here.
        if path and not Path(path).exists():
            return


async def download_callback(update: Update, context) -> None:
    callback = update.callback_query
    data = callback.data or ""
    try:
        index = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный запрос", show_alert=True)
        return

    results = context.user_data.get("results") or []
    if index < 0 or index >= len(results):
        await callback.answer("Эта модель больше недоступна. Выполните поиск заново.", show_alert=True)
        return
    model = normalize_card_model(results[index], index + 1)
    url = model_download_url(model)
    if not url:
        await callback.answer("Прямое скачивание для этого источника недоступно.", show_alert=True)
        return

    allowed, remaining = consume_download_slot(update.effective_user.id if update.effective_user else None)
    if not allowed:
        await callback.answer("Достигнут дневной лимит скачиваний (10).", show_alert=True)
        return

    await callback.answer(f"Скачивание… Осталось сегодня: {remaining}")
    status = await callback.message.reply_text("⬇️ Загружаю модель, проверяю размер файла…")
    temp_path = None
    try:
        temp_path, size, filename = await _download_to_temp(url, model)
        if temp_path and filename:
            with open(temp_path, "rb") as handle:
                await context.bot.send_document(
                    chat_id=callback.message.chat_id,
                    document=handle,
                    filename=filename,
                    caption=f"{html.escape(str(model.get('name')))}\nИсточник: {html.escape(str(model.get('source')))}",
                    parse_mode="HTML",
                )
            if remaining <= 2:
                await callback.message.reply_text(f"✅ Файл отправлен. Осталось скачиваний сегодня: {remaining}")
        elif size > DOWNLOAD_MAX_BYTES:
            await callback.message.reply_text(
                f"Файл слишком большой для отправки ботом: {size / (1024 * 1024):.1f} MB.\n🔗 {html.escape(url)}",
                parse_mode="HTML",
            )
        else:
            await callback.message.reply_text(
                f"Не удалось скачать файл автоматически. Откройте источник:\n{html.escape(str(model.get('viewerUrl') or url))}",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning("Telegram document send failed: %s", type(exc).__name__)
        await callback.message.reply_text("Не удалось отправить модель в Telegram. Откройте страницу источника.")
    finally:
        with suppress_file(temp_path):
            pass
        try:
            await status.delete()
        except Exception:
            pass


def suppress_file(path: str | None):
    class _Cleanup:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return False

    return _Cleanup()


async def handle_callback(update: Update, context) -> None:
    callback = update.callback_query
    data = callback.data or ""
    chat_id = callback.message.chat_id
    if data.startswith("download:"):
        await download_callback(update, context)
        return
    await callback.answer()

    if data == "show_formats":
        await context.bot.send_message(chat_id, "📦 <b>Выберите формат:</b>", parse_mode="HTML", reply_markup=base_bot.format_keyboard())
        return
    if data == "show_categories":
        await context.bot.send_message(chat_id, "🎯 <b>Выберите категорию:</b>", parse_mode="HTML", reply_markup=base_bot.category_keyboard())
        return
    if data == "clear_filters":
        context.user_data.pop("selected_format", None)
        context.user_data.pop("selected_category", None)
        await context.bot.send_message(chat_id, "✅ Фильтры сброшены.")
        return
    if data.startswith("format:"):
        value = data.split(":", 1)[1]
        if value in {"clear", "any"}:
            context.user_data.pop("selected_format", None)
            await context.bot.send_message(chat_id, "✅ Ограничение по формату снято.")
        else:
            context.user_data["selected_format"] = value
            await context.bot.send_message(chat_id, f"✅ Выбран формат <b>{html.escape(value.upper())}</b>.", parse_mode="HTML")
        return
    if data.startswith("category:"):
        value = data.split(":", 1)[1]
        if value == "clear":
            context.user_data.pop("selected_category", None)
            await context.bot.send_message(chat_id, "✅ Категория сброшена.")
        else:
            context.user_data["selected_category"] = value
            await context.bot.send_message(chat_id, f"✅ Выбрана категория <b>{html.escape(value)}</b>.", parse_mode="HTML")
        return
    if data.startswith("page:"):
        try:
            context.user_data["offset"] = max(0, int(data.split(":", 1)[1]))
        except ValueError:
            context.user_data["offset"] = 0
        await send_page(context, chat_id)
        return
    await context.bot.send_message(chat_id, "Кнопка устарела. Выполните поиск заново.")


def install_handlers(application) -> None:
    # render_server registers these replacements instead of the old text/callback handlers.
    application.add_handler(base_bot.CommandHandler("start", base_bot.start))
    application.add_handler(base_bot.MessageHandler(base_bot.filters.TEXT & ~base_bot.filters.COMMAND, handle_text))
    application.add_handler(base_bot.CallbackQueryHandler(handle_callback))
