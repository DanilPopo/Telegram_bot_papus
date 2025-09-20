import requests
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
import logging
import time

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN = "8489310512:AAE40jUiqHaWj2rvRfkc0-3wYrztA_2cI2k"

# Кэш для хранения данных игр
games_cache = {}

# ========== EПIC GAMES API ==========
def get_epic_games():
    try:
        url = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        games = []
        for game in data['data']['Catalog']['searchStore']['elements']:
            if game.get('promotions') and game['promotions']['promotionalOffers']:
                title = game['title']
                game_id = game['id']
                price_info = game['price']['totalPrice']
                
                # Форматируем цену
                original_price = price_info['fmtPrice']['originalPrice']
                discount_price = price_info['fmtPrice']['discountPrice']
                
                # Получаем изображение
                images = game.get('keyImages', [])
                image_url = next((img['url'] for img in images if img['type'] == 'Thumbnail'), None)
                
                game_data = {
                    'title': title,
                    'original_price': original_price,
                    'discount_price': discount_price,
                    'url': f'https://store.epicgames.com/p/{game_id.lower()}',
                    'image': image_url,
                    'store': 'epic'
                }
                
                games.append(game_data)
                if len(games) >= 10:  # Берем больше для кэша
                    break
        
        return games[:5]  # Возвращаем 5 лучших
        
    except Exception as e:
        print(f"Epic Games error: {e}")
        return []

# ========== GOG API ==========
def get_gog_games():
    try:
        url = "https://www.gog.com/games/ajax/filtered?mediaType=game&page=1&sort=popularity"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        games = []
        for product in data['products'][:10]:  # Берем больше для кэша
            if product.get('price'):
                game_data = {
                    'title': product['title'],
                    'original_price': f"${product['price']['amount']}",
                    'discount_price': f"${product['price']['amount']}",
                    'url': f"https://www.gog.com{product['url']}",
                    'image': product['image'] + '.jpg',
                    'store': 'gog'
                }
                games.append(game_data)
        
        return games[:5]  # Возвращаем 5 лучших
        
    except Exception as e:
        print(f"GOG error: {e}")
        return []

# ========== КОМАНДА /START ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎮 Epic Games", callback_data="store_epic")],
        [InlineKeyboardButton("🕹️ GOG.com", callback_data="store_gog")],
        [InlineKeyboardButton("📊 О боте", callback_data="about")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎯 <b>Добро пожаловать в GameDeals Bot!</b>\n\n"
        "Я помогу найти лучшие предложения в игровых магазинах:\n"
        "• 🔥 Горячие скидки\n"
        "• 🎁 Бесплатные игры\n"
        "• 💰 Лучшие цены\n\n"
        "<b>Выберите магазин:</b>",
        parse_mode='HTML',
        reply_markup=reply_markup
    )

# ========== ПОКАЗАТЬ ИГРЫ ==========
async def show_store_games(update: Update, context: ContextTypes.DEFAULT_TYPE, store_type: str):
    query = update.callback_query
    await query.answer()
    
    # Получаем игры
    if store_type == 'epic':
        games = get_epic_games()
        store_name = "Epic Games"
    else:
        games = get_gog_games()
        store_name = "GOG.com"
    
    if not games:
        await query.edit_message_text(
            f"❌ Не удалось загрузить игры из {store_name}\nПопробуйте позже.",
            parse_mode='HTML'
        )
        return
    
    # Сохраняем в кэш
    games_cache[query.message.chat_id] = games
    
    # Создаем сообщение с играми
    message = f"🎮 <b>{store_name} - Топ предложений</b>\n\n"
    for i, game in enumerate(games[:5], 1):
        message += f"{i}. <b>{game['title']}</b>\n"
        if game['discount_price'] != '0':
            message += f"   💰 <s>{game['original_price']}</s> → {game['discount_price']}\n"
        else:
            message += f"   🎁 <b>БЕСПЛАТНО!</b>\n"
        message += f"   🔗 <a href='{game['url']}'>Купить</a>\n\n"
    
    # Создаем кнопки
    keyboard = []
    for i in range(min(5, len(games))):
        keyboard.append([InlineKeyboardButton(f"📖 {games[i]['title'][:20]}...", callback_data=f"detail_{i}")])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад к магазинам", callback_data="back_to_stores")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        message,
        parse_mode='HTML',
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

# ========== ДЕТАЛЬНЫЙ ПРОСМОТР ==========
async def show_game_details(update: Update, context: ContextTypes.DEFAULT_TYPE, game_index: int):
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    if chat_id not in games_cache:
        await query.edit_message_text("❌ Данные устарели. Выберите магазин снова.")
        return
    
    games = games_cache[chat_id]
    if game_index >= len(games):
        await query.edit_message_text("❌ Игра не найдена.")
        return
    
    game = games[game_index]
    
    # Формируем детальное сообщение
    message = f"🎯 <b>{game['title']}</b>\n\n"
    
    if game['store'] == 'epic':
        message += f"🏪 <b>Магазин:</b> Epic Games\n"
    else:
        message += f"🏪 <b>Магазин:</b> GOG.com\n"
    
    if game['discount_price'] != '0' and game['discount_price'] != game['original_price']:
        message += f"💰 <b>Цена:</b> <s>{game['original_price']}</s> → {game['discount_price']}\n"
        message += f"🔥 <b>Скидка:</b> Отличное предложение!\n"
    elif game['discount_price'] == '0':
        message += f"🎁 <b>Статус:</b> БЕСПЛАТНО!\n"
    else:
        message += f"💰 <b>Цена:</b> {game['original_price']}\n"
    
    message += f"\n📦 <b>Ссылка:</b> <a href='{game['url']}'>Перейти к покупке</a>\n\n"
    message += "⭐ <i>Не упустите выгодное предложение!</i>"
    
    # Кнопки
    keyboard = [
        [InlineKeyboardButton("🛒 Купить сейчас", url=game['url'])],
        [InlineKeyboardButton("🔙 К списку игр", callback_data=f"back_to_{game['store']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        message,
        parse_mode='HTML',
        reply_markup=reply_markup,
        disable_web_page_preview=False  # Разрешаем превью для картинок
    )

# ========== ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == 'store_epic':
        await show_store_games(update, context, 'epic')
    elif data == 'store_gog':
        await show_store_games(update, context, 'gog')
    elif data == 'back_to_stores':
        await start(update, context)
    elif data.startswith('detail_'):
        game_index = int(data.split('_')[1])
        await show_game_details(update, context, game_index)
    elif data.startswith('back_to_'):
        store_type = data.split('_')[2]
        await show_store_games(update, context, store_type)
    elif data == 'about':
        await query.answer("GameDeals Bot v1.0 • Отслеживайте лучшие игровые сделки!", show_alert=True)

# ========== ОСНОВНАЯ ФУНКЦИЯ ==========
def main():
    # Создаем приложение
    application = Application.builder().token(TOKEN).build()
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Запускаем бота
    print("✅ GameDeals Bot запущен!")
    
    while True:
        try:
            application.run_polling(
                poll_interval=3.0,
                timeout=10.0,
                drop_pending_updates=True
            )
        except Exception as e:
            print(f"💥 Ошибка: {e}. Перезапуск через 10 секунд...")
            time.sleep(10)

if __name__ == "__main__":
    main()
