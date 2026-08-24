import asyncio
import html
import logging
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
PAGE_SIZE = max(3, min(5, int(os.getenv('RESULTS_PAGE_SIZE', '4'))))
DOWNLOAD_DAILY_LIMIT = max(1, int(os.getenv('DOWNLOADS_PER_DAY', '10')))
DOWNLOAD_MAX_BYTES = min(50 * 1024 * 1024, max(1 * 1024 * 1024, int(os.getenv('DOWNLOAD_MAX_BYTES', str(50 * 1024 * 1024)))))
DOWNLOAD_MAX_USERS = max(128, int(os.getenv('DOWNLOAD_MAX_USERS', '4096')))
DOWNLOAD_TIMEOUT = max(10, int(os.getenv('DOWNLOAD_TIMEOUT_SECONDS', '60')))
DOWNLOAD_CHUNK_SIZE = 256 * 1024
_download_usage: OrderedDict[int, deque[float]] = OrderedDict()


def _cleanup_usage(now: float) -> None:
    for user_id, timestamps in list(_download_usage.items()):
        while timestamps and now - timestamps[0] >= 86400:
            timestamps.popleft()
        if not timestamps:
            _download_usage.pop(user_id, None)
    while len(_download_usage) > DOWNLOAD_MAX_USERS:
        _download_usage.popitem(last=False)


def consume_download_slot(user_id: int | None) -> tuple[bool, int]:
    if user_id is None:
        return False, 0
    now = time.time()
    _cleanup_usage(now)
    timestamps = _download_usage.setdefault(user_id, deque())
    if len(timestamps) >= DOWNLOAD_DAILY_LIMIT:
        return False, 0
    timestamps.append(now)
    _download_usage.move_to_end(user_id)
    return True, DOWNLOAD_DAILY_LIMIT - len(timestamps)


def safe_filename(model: dict, url: str) -> str:
    name = re.sub(r'[^\w\-. ]+', '_', str(model.get('name') or 'model'), flags=re.UNICODE).strip(' ._') or 'model'
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    allowed = {'.zip', '.glb', '.gltf', '.obj', '.fbx', '.stl', '.3mf', '.blend', '.usd', '.usdz', '.ply', '.dae', '.step', '.iges', '.3ds'}
    return f"{name[:96]}{suffix if suffix in allowed else '.bin'}"


def model_download_url(model: dict) -> str | None:
    url = str(model.get('downloadUrl') or model.get('download_url') or '').strip()
    parsed = urlparse(url)
    return url if parsed.scheme in {'http', 'https'} and parsed.netloc else None


def normalize_card_model(model: dict, index: int) -> dict:
    result = dict(model or {})
    result['name'] = result.get('name') or f'Модель {index}'
    result['source'] = result.get('source') or 'unknown'
    result['viewerUrl'] = result.get('sourceUrl') or result.get('viewerUrl') or ''
    result['thumbnail'] = result.get('thumbnailUrl') or result.get('thumbnail')
    result['formats'] = result.get('formats') or []
    return result


def _size_text(model: dict) -> str:
    metadata = model.get('metadata') if isinstance(model.get('metadata'), dict) else {}
    raw = model.get('size') or model.get('fileSize') or model.get('downloadSize') or metadata.get('size') or metadata.get('fileSize')
    try:
        size = float(raw)
    except (TypeError, ValueError):
        return ''
    return f'{size / (1024 * 1024):.1f} MB' if size >= 1024 * 1024 else f'{size / 1024:.0f} KB'


def build_quality_caption(model: dict, index: int) -> str:
    formats = ', '.join(html.escape(str(x).upper()) for x in (model.get('formats') or [])[:8]) or '—'
    quality = model.get('qualityScore')
    quality_line = f"\n⭐ Качество: {float(quality):.0f}" if isinstance(quality, (int, float)) else ''
    size = _size_text(model)
    size_line = f"\n💾 Размер: {html.escape(size)}" if size else ''
    author = model.get('author')
    author_name = author.get('name') if isinstance(author, dict) else ''
    author_line = f"\n👤 Автор: {html.escape(str(author_name))}" if author_name else ''
    return (
        f"<b>{index}. {html.escape(str(model['name']))}</b>\n"
        f"📦 Форматы: {formats}\n"
        f"🔎 Источник: {html.escape(str(model['source']))}\n"
        f"📜 Лицензия: {html.escape(str(model.get('license') or 'Не указана'))}"
        f"{quality_line}{size_line}{author_line}"
    )


def card_keyboard(model: dict, index: int) -> InlineKeyboardMarkup:
    row = []
    if model_download_url(model):
        row.append(InlineKeyboardButton('⬇️ Скачать', callback_data=f'download:{index}'))
    url = str(model.get('viewerUrl') or '').strip()
    if url.startswith(('http://', 'https://')):
        row.append(InlineKeyboardButton('ℹ️ Подробнее', url=url))
    return InlineKeyboardMarkup([row]) if row else InlineKeyboardMarkup([])


async def _search(query: str, format_name: str | None, category: str | None) -> list:
    effective = f'{category} {query}' if category and category != 'other' else query
    return await base_bot.fetch_search_service(effective, format_name, category)


async def send_page(context, chat_id: int) -> None:
    results = context.user_data.get('results') or []
    offset = max(0, int(context.user_data.get('offset', 0)))
    query = str(context.user_data.get('query') or '')
    page = results[offset:offset + PAGE_SIZE]
    if not page:
        await context.bot.send_message(chat_id, 'Модели закончились 🤷')
        return

    for position, raw in enumerate(page, start=offset + 1):
        model = normalize_card_model(raw, position)
        caption = build_quality_caption(model, position)
        markup = card_keyboard(model, position - 1)
        try:
            if model.get('thumbnail'):
                await context.bot.send_photo(chat_id, model['thumbnail'], caption=caption, parse_mode='HTML', reply_markup=markup)
            else:
                await context.bot.send_message(chat_id, caption, parse_mode='HTML', reply_markup=markup)
        except Exception as exc:
            logger.warning('result card failed: %s', type(exc).__name__)
            try:
                await context.bot.send_message(chat_id, caption, parse_mode='HTML', reply_markup=markup)
            except Exception:
                pass

    last = min(offset + PAGE_SIZE, len(results))
    nav = []
    if offset:
        nav.append(InlineKeyboardButton('◀️ Назад', callback_data=f'page:{offset - PAGE_SIZE}'))
    if last < len(results):
        nav.append(InlineKeyboardButton('Вперёд ▶️', callback_data=f'page:{offset + PAGE_SIZE}'))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton('📦 Формат', callback_data='show_formats'), InlineKeyboardButton('🎯 Категория', callback_data='show_categories')])
    await context.bot.send_message(
        chat_id,
        f"Страница {offset // PAGE_SIZE + 1}/{(len(results) + PAGE_SIZE - 1) // PAGE_SIZE} · {offset + 1}–{last} из {len(results)}\n🔎 «{html.escape(query)}»",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode='HTML',
    )


async def handle_text(update: Update, context) -> None:
    text = (update.message.text or '').strip()
    if not text:
        return
    user_id = update.effective_user.id if update.effective_user else None
    remaining = base_bot.rate_limit_search(user_id)
    if remaining > 0:
        await update.message.reply_text(f'⏳ Слишком частые запросы. Повторите через {remaining:.1f} сек.')
        return
    query, typed_format = base_bot.extract_and_normalize_query(text)
    format_name = typed_format or context.user_data.get('selected_format')
    category = context.user_data.get('selected_category')
    if not query:
        await update.message.reply_text('Напишите, что искать, например: найти STL кота')
        return
    await update.message.reply_text(
        f"🔍 Ищу: «{html.escape(query)}" + (f"» [{format_name.upper()}]" if format_name else '»') + '…',
        parse_mode='HTML',
    )
    results = await _search(query, format_name, category)
    if not results:
        await update.message.reply_text('Ничего качественного не найдено. Попробуйте изменить запрос или формат.')
        return
    context.user_data.update(query=query, results=results, offset=0, format_name=format_name, category_name=category)
    await send_page(context, update.effective_chat.id)


async def _download(url: str, model: dict) -> tuple[str | None, int, str | None]:
    temp_path = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT), headers={'User-Agent': '3DModelFinderBot/1.0'}) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    return None, 0, None
                length = response.headers.get('Content-Length')
                if length and int(length) > DOWNLOAD_MAX_BYTES:
                    return None, int(length), None
                suffix = Path(safe_filename(model, url)).suffix
                with tempfile.NamedTemporaryFile(prefix='3dmodel_', suffix=suffix, delete=False) as temp:
                    temp_path = temp.name
                    total = 0
                    async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                        total += len(chunk)
                        if total > DOWNLOAD_MAX_BYTES:
                            try:
                                os.unlink(temp_path)
                            except OSError:
                                pass
                            return None, total, None
                        temp.write(chunk)
                return temp_path, total, safe_filename(model, url)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
        logger.warning('download failed: %s', type(exc).__name__)
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return None, 0, None


async def download_callback(update: Update, context) -> None:
    callback = update.callback_query
    try:
        index = int((callback.data or '').split(':', 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Некорректная кнопка', show_alert=True)
        return
    results = context.user_data.get('results') or []
    if index < 0 or index >= len(results):
        await callback.answer('Результат устарел. Выполните поиск заново.', show_alert=True)
        return
    model = normalize_card_model(results[index], index + 1)
    url = model_download_url(model)
    if not url:
        await callback.answer('Прямое скачивание недоступно.', show_alert=True)
        return
    allowed, remaining = consume_download_slot(update.effective_user.id if update.effective_user else None)
    if not allowed:
        await callback.answer(f'Достигнут дневной лимит: {DOWNLOAD_DAILY_LIMIT} скачиваний.', show_alert=True)
        return
    await callback.answer('Загружаю файл…')
    status = await callback.message.reply_text('⬇️ Загружаю модель и проверяю размер…')
    temp_path = None
    try:
        temp_path, size, filename = await _download(url, model)
        if temp_path and filename:
            with open(temp_path, 'rb') as handle:
                await context.bot.send_document(
                    callback.message.chat_id,
                    handle,
                    filename=filename,
                    caption=f"{html.escape(str(model['name']))}\nИсточник: {html.escape(str(model['source']))}",
                    parse_mode='HTML',
                )
            if remaining <= 2:
                await callback.message.reply_text(f'✅ Отправлено. Осталось сегодня: {remaining}')
        elif size > DOWNLOAD_MAX_BYTES:
            await callback.message.reply_text(f"Файл больше лимита Telegram (50 MB): {size / (1024 * 1024):.1f} MB.\n🔗 {html.escape(url)}", parse_mode='HTML')
        else:
            await callback.message.reply_text(f"Не удалось скачать автоматически.\n🔗 {html.escape(str(model.get('viewerUrl') or url))}", parse_mode='HTML')
    except Exception as exc:
        logger.warning('send_document failed: %s', type(exc).__name__)
        await callback.message.reply_text('Не удалось отправить файл. Откройте страницу модели.')
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        try:
            await status.delete()
        except Exception:
            pass


async def handle_callback(update: Update, context) -> None:
    callback = update.callback_query
    data = callback.data or ''
    chat_id = callback.message.chat_id
    if data.startswith('download:'):
        await download_callback(update, context)
        return
    await callback.answer()
    if data == 'show_formats':
        await context.bot.send_message(chat_id, '📦 <b>Выберите формат:</b>', parse_mode='HTML', reply_markup=base_bot.format_keyboard())
        return
    if data == 'show_categories':
        await context.bot.send_message(chat_id, '🎯 <b>Выберите категорию:</b>', parse_mode='HTML', reply_markup=base_bot.category_keyboard())
        return
    if data == 'clear_filters':
        context.user_data.pop('selected_format', None)
        context.user_data.pop('selected_category', None)
        await context.bot.send_message(chat_id, '✅ Фильтры сброшены.')
        return
    if data.startswith('format:'):
        value = data.split(':', 1)[1]
        if value in {'clear', 'any'}:
            context.user_data.pop('selected_format', None)
            await context.bot.send_message(chat_id, '✅ Ограничение по формату снято.')
        else:
            context.user_data['selected_format'] = value
            await context.bot.send_message(chat_id, f"✅ Выбран формат <b>{html.escape(value.upper())}</b>.", parse_mode='HTML')
        return
    if data.startswith('category:'):
        value = data.split(':', 1)[1]
        if value == 'clear':
            context.user_data.pop('selected_category', None)
            await context.bot.send_message(chat_id, '✅ Категория сброшена.')
        else:
            context.user_data['selected_category'] = value
            await context.bot.send_message(chat_id, f"✅ Выбрана категория <b>{html.escape(value)}</b>.", parse_mode='HTML')
        return
    if data.startswith('page:'):
        try:
            context.user_data['offset'] = max(0, int(data.split(':', 1)[1]))
        except ValueError:
            context.user_data['offset'] = 0
        await send_page(context, chat_id)
        return
    await context.bot.send_message(chat_id, 'Кнопка устарела. Выполните поиск заново.')
