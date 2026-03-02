import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

if not BOT_TOKEN or not CHANNEL_ID:
    raise RuntimeError("Нужно задать переменные BOT_TOKEN и CHANNEL_ID в Railway")

async def post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = [
        "https://picsum.photos/seed/1/1200/1200",
        "https://picsum.photos/seed/2/1200/1200",
        "https://picsum.photos/seed/3/1200/1200",
    ]

    caption = (
        "🔥 Тестовый товар\n\n"
        "Описание товара здесь.\n\n"
        "💰 2390 ₽"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛡 Условия покупки", url="https://t.me/TechnoNVRSK/1"),
            InlineKeyboardButton("📦 Оформить заказ", url="https://t.me/technopolza_catalog_bot")
        ]
    ])

    media = [InputMediaPhoto(photos[0], caption=caption)]
    media += [InputMediaPhoto(p) for p in photos[1:]]

    await context.bot.send_media_group(chat_id=CHANNEL_ID, media=media)

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text="⬇️ Быстрые действия:",
        reply_markup=keyboard
    )

    await update.message.reply_text("Пост опубликован.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши /post — отправлю тест в канал.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("post", post))
    app.run_polling()

if __name__ == "__main__":
    main()
