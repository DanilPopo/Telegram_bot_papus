#!/usr/bin/env python3
# bot.py — GameDeals multi-store Telegram bot
# Требования: python >=3.9, python-telegram-bot[job-queue], aiohttp, aiosqlite

import os
import logging
import asyncio
import aiohttp
import aiosqlite
import uuid
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    InlineQueryHandler
)

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не задан. Установи переменную окружения BOT_TOKEN и перезапусти.")
    raise SystemExit("Не найден BOT_TOKEN")

DB_PATH = os.environ.get("BOT_DB", "bot.db")
CACHE_TTL = 60 * 5  # 5 минут кэш для запросов
FREE_CHECK_INTERVAL = 60 * 60 * 6  # каждые 6 часов проверять бесплатные игры

# глобальный aiohttp session (создаётся в main)
SESSION = None

# простой in-memory кэш { key: (expires_datetime, data) }
CACHE = {}

# --- Вспомогательные функции ---
def cache_get(key):
    v = CACHE.get(key)
    if not v:
        return None
    expires, data = v
    if datetime.utcnow() > expires:
        del CACHE[key]
        return None
    return data

def cache_set(key, data, ttl=CACHE_TTL):
    CACHE[key] = (datetime.utcnow() + timedelta(seconds=ttl), data)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                added_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS last_offers (
                store TEXT,
                offer_id TEXT,
                title TEXT,
                PRIMARY KEY(store, offer_id)
            )
        """)
        await db.commit()

async def add_subscriber(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO subscribers(chat_id, added_at) VALUES (?, ?)",
                         (chat_id, datetime.utcnow().isoformat()))
        await db.commit()

async def remove_subscriber(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        await db.commit()

async def list_subscribers():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id FROM subscribers")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def offer_exists(store, offer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM last_offers WHERE store=? AND offer_id=?", (store, offer_id))
        row = await cur.fetchone()
        return bool(row)

async def save_offer(store, offer_id, title):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO last_offers(store, offer_id, title) VALUES (?, ?, ?)",
                         (store, offer_id, title))
        await db.commit()

# --- Парсеры магазинов (async) ---
async def fetch_json(url, params=None, headers=None, timeout=15):
    global SESSION
    if SESSION is None:
        SESSION = aiohttp.ClientSession()
    try:
        async with SESSION.get(url, params=params, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as e:
        logger.warning("fetch_json error %s %s", url, e)
        return None

async def get_epic_games():
    """Возвращает список словарей с игровыми офферами (Epic)"""
    cached = cache_get("epic_top")
    if cached:
        return cached

    url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json'
    }
    data = await fetch_json(url, headers=headers)
    if not data:
        return []

    out = []
    try:
        elems = data['data']['Catalog']['searchStore']['elements']
        for g in elems:
            promos = g.get('promotions') or {}
            if (promos.get('promotionalOffers') and any(promos.get('promotionalOffers')) ) or \
               (promos.get('upcomingPromotionalOffers') and any(promos.get('upcomingPromotionalOffers'))):
                title = g.get('title') or g.get('productSlug') or "Unknown"
                gid = str(g.get('id') or title)
                price_info = g.get('price', {}).get('totalPrice', {})
                original = price_info.get('fmtPrice', {}).get('originalPrice', '—')
                discount = price_info.get('fmtPrice', {}).get('discountPrice', '—')
                images = g.get('keyImages', [])
                thumbnail = next((i['url'] for i in images if i.get('type')=='Thumbnail'), None)
                out.append({
                    'title': title,
                    'id': gid,
                    'original_price': original,
                    'discount_price': discount,
                    'url': f"https://store.epicgames.com/p/{g.get('productSlug','')}",
                    'image': thumbnail,
                    'store': 'epic'
                })
                if len(out) >= 10:
                    break
    except Exception as e:
        logger.exception("epic parse error: %s", e)

    cache_set("epic_top", out, ttl=60*10)
    return out

async def get_gog_games():
    """Возвращает список офферов GOG (парсинг ajax)"""
    cached = cache_get("gog_top")
    if cached:
        return cached

    url = "https://www.gog.com/games/ajax/filtered?mediaType=game&page=1&sort=popularity"
    headers = {'User-Agent': 'Mozilla/5.0'}
    data = await fetch_json(url, headers=headers)
    if not data:
        return []

    out = []
    try:
        products = data.get('products', [])[:15]
        for p in products:
            price = p.get('price')
            if price is None:
                # возможно промо/бесплатно
                original = "—"
                discount = "0"
            else:
                original = f"{price.get('amount')}{price.get('currency', '')}"
                discount = original
            out.append({
                'title': p.get('title'),
                'id': p.get('id'),
                'original_price': original,
                'discount_price': discount,
                'url': f"https://www.gog.com{p.get('url')}",
                'image': (p.get('image') + '.jpg') if p.get('image') else None,
                'store': 'gog'
            })
    except Exception as e:
        logger.exception("gog parse error: %s", e)

    cache_set("gog_top", out, ttl=60*10)
    return out

async def get_steam_games(query, limit=5):
    """
    Поиск в Steam.
    Используем неофициальный endpoint storesearch (структурированный JSON).
    Источники: люди используют /api/storesearch или /search/results?format=json .
    """
    key = f"steam:{query}:{limit}"
    cached = cache_get(key)
    if cached:
        return cached

    # API: https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=US
    url = "https://store.steampowered.com/api/storesearch/"
    params = {"term": query, "l": "english", "cc": "US", "count": limit}
    data = await fetch_json(url, params=params, headers={'User-Agent':'Mozilla/5.0'})
    out = []
    if not data:
        return []

    try:
        items = data.get('items') or data.get('results') or []
        # items: list of dicts with fields 'id','name','price'...
        for it in items[:limit]:
            name = it.get('name') or it.get('title')
            appid = it.get('id') or it.get('appid') or it.get('id')
            price_info = it.get('price') or {}
            if price_info:
                final = price_info.get('final')  # in cents
                initial = price_info.get('initial')
                if final is None:
                    price_text = "Free" if it.get('is_free') else "—"
                else:
                    price_text = f"${final/100:.2f}"
            else:
                price_text = "Free" if it.get('is_free') else "—"
            url_game = f"https://store.steampowered.com/app/{appid}/" if appid else it.get('url')
            out.append({
                'title': name,
                'id': appid,
                'original_price': price_info.get('initial') if price_info else '—',
                'discount_price': price_text,
                'url': url_game,
                'image': it.get('tiny_image') or None,
                'store': 'steam'
            })
    except Exception as e:
        logger.exception("steam parse error: %s", e)

    cache_set(key, out, ttl=60*5)
    return out

# --- Telegram handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎯 <b>Добро пожаловать в GameDeals Bot!</b>\n\n"
        "Я отслеживаю выгодные предложения в игровых магазинах: Epic, GOG, Steam.\n"
        "• Используй меню или команды:\n"
        "  /compare <название> — сравнить цены по магазинам\n"
        "  /subscribe — подписаться на уведомления о новых бесплатных играх\n"
        "  /unsubscribe — отписаться\n\n"
        "Или вызови меня в любом чате через @<b>имя_бота</b> и введи название игры (inline режим)."
    )
    keyboard = [
        [InlineKeyboardButton("🎮 Epic", callback_data="store_epic")],
        [InlineKeyboardButton("🕹️ GOG", callback_data="store_gog")],
        [InlineKeyboardButton("🔎 Compare", callback_data="compare_prompt")],
    ]
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def compare_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /compare <query>
    if not context.args:
        await update.message.reply_text("Использование: /compare <название игры>")
        return
    query = " ".join(context.args).strip()
    msg = await update.message.reply_text(f"🔎 Ищу «{query}» по магазинам...")

    steam = await get_steam_games(query, limit=3)
    epic = await get_epic_games()
    gog = await get_gog_games()

    # простая логика: попробуем найти совпадение по названию в epic/gog (по вхождению)
    def find_match(list_, q):
        ql = q.lower()
        for it in list_:
            if it['title'] and ql in it['title'].lower():
                return it
        return None

    s0 = steam[0] if steam else None
    e0 = find_match(epic, query)
    g0 = find_match(gog, query)

    text = f"🔎 <b>Сравнение цен — {query}</b>\n\n"
    if s0:
        text += f"🟦 <b>Steam:</b> {s0['title']} — {s0['discount_price']}\n   🔗 {s0['url']}\n\n"
    else:
        text += "🟦 <b>Steam:</b> — результатов не найдено\n\n"
    if e0:
        text += f"🟣 <b>Epic:</b> {e0['title']} — {e0['discount_price']}\n   🔗 {e0['url']}\n\n"
    else:
        text += "🟣 <b>Epic:</b> — результатов не найдено\n\n"
    if g0:
        text += f"🟠 <b>GOG:</b> {g0['title']} — {g0['discount_price']}\n   🔗 {g0['url']}\n\n"
    else:
        text += "🟠 <b>GOG:</b> — результатов не найдено\n\n"

    await msg.edit_text(text, parse_mode='HTML', disable_web_page_preview=False)

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    # Подписка работает если бот может отправлять сообщения в этот чат.
    await add_subscriber(chat.id)
    await update.message.reply_text("✅ Вы подписаны на оповещения о новых бесплатных играх.")

async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await remove_subscriber(chat.id)
    await update.message.reply_text("🗑️ Вы отписаны от оповещений.")

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        # можно предложить подсказку
        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="Напиши название игры",
                input_message_content=InputTextMessageContent("Напиши название игры после @бота")
            )
        ]
        await update.inline_query.answer(results, cache_time=60)
        return

    steam = await get_steam_games(query, limit=5)
    results = []
    for i, g in enumerate(steam):
        msg_text = f"🎯 <b>{g['title']}</b>\n\n{g['discount_price']}\n🔗 {g['url']}"
        results.append(
            InlineQueryResultArticle(
                id=f"steam_{g.get('id')}_{i}_{uuid.uuid4().hex[:6]}",
                title=f"{g['title']} — {g['discount_price']}",
                input_message_content=InputTextMessageContent(msg_text, parse_mode='HTML')
            )
        )
    if not results:
        results.append(
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="Ничего не найдено",
                input_message_content=InputTextMessageContent(f"По запросу «{query}» ничего не найдено.")
            )
        )
    await update.inline_query.answer(results, cache_time=30)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "store_epic":
        epic = await get_epic_games()
        text = "🎮 <b>Epic — топ офферов</b>\n\n"
        for i, g in enumerate(epic[:5], 1):
            text += f"{i}. <b>{g['title']}</b>\n   {g['discount_price']}\n   🔗 {g['url']}\n\n"
        await q.edit_message_text(text, parse_mode='HTML', disable_web_page_preview=True)
    elif data == "store_gog":
        gog = await get_gog_games()
        text = "🕹️ <b>GOG — топ офферов</b>\n\n"
        for i, g in enumerate(gog[:5], 1):
            text += f"{i}. <b>{g['title']}</b>\n   {g['discount_price']}\n   🔗 {g['url']}\n\n"
        await q.edit_message_text(text, parse_mode='HTML', disable_web_page_preview=True)
    elif data == "compare_prompt":
        await q.edit_message_text("Используй команду /compare <название игры>")

# --- Job: проверка бесплатных игр и рассылка подписчикам ---
async def check_free_games_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info("Job: check free games start")
        epic = await get_epic_games()
        gog = await get_gog_games()
        new_messages = []

        # Epic: считаем уникальным offer id = id
        for g in epic:
            # простая проверка на "бесплатно" — если discount_price содержит 'FREE' или '0' или 'БЕСПЛАТНО'
            if g['discount_price'] in ('0', 'Free', '0.00', 'FREE') or 'FREE' in str(g['discount_price']).upper() or 'БЕСПЛАТ' in str(g['discount_price']).upper():
                already = await offer_exists('epic', g['id'])
                if not already:
                    await save_offer('epic', g['id'], g['title'])
                    new_messages.append(f"🎁 <b>Epic — бесплатно:</b> {g['title']}\n🔗 {g['url']}")

        # GOG: у GOG нет явного 'free' в цене — но если price пустой/0
        for g in gog:
            dp = g.get('discount_price') or ""
            if dp == '$0' or dp == 'Free' or 'FREE' in str(dp).upper() or '0' in str(dp):
                already = await offer_exists('gog', g['id'])
                if not already:
                    await save_offer('gog', str(g['id']), g['title'])
                    new_messages.append(f"🎁 <b>GOG — бесплатно:</b> {g['title']}\n🔗 {g['url']}")

        if not new_messages:
            logger.info("Job: нет новых бесплатных офферов")
            return

        subs = await list_subscribers()
        if not subs:
            logger.info("Job: подписчиков нет, прерываем рассылку")
            return

        text = "\n\n".join(new_messages)
        for chat_id in subs:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', disable_web_page_preview=False)
            except Exception as e:
                logger.warning("Не удалось отправить подписчику %s: %s", chat_id, e)
    except Exception as e:
        logger.exception("Ошибка в job check_free_games: %s", e)

# --- Основная функция ---
def main():
    global SESSION
    application = Application.builder().token(BOT_TOKEN).build()

    # handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("compare", compare_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(CallbackQueryHandler(button_handler))

    # job: проверяем бесплатные офферы каждые FREE_CHECK_INTERVAL
    application.job_queue.run_repeating(check_free_games_job, interval=FREE_CHECK_INTERVAL, first=10)

    # подготовка (инициализация БД и сессии)
    async def _run():
        global SESSION
        SESSION = aiohttp.ClientSession()
        await init_db()
        logger.info("✅ DB initialized, starting polling")
        # старт polling (удобно для начала; позже можно перейти на webhook)
        await application.run_polling()

        # при остановке закрываем session
        await SESSION.close()

    # запускаем
    asyncio.run(_run())

if __name__ == "__main__":
    main()
