import os
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("technopostbot")

# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # можно "@TechnoNVRSK"
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))         # твой numeric Telegram user id

# ссылки для кнопок
URL_CONDITIONS = os.getenv("URL_CONDITIONS", "").strip()  # ссылка на пост "Условия покупки"
URL_ORDER = os.getenv("URL_ORDER", "").strip()            # ссылка/бот/форма "Оформить заказ"

if not BOT_TOKEN or not CHANNEL_ID or not ADMIN_ID:
    raise RuntimeError("Заполни BOT_TOKEN, CHANNEL_ID, ADMIN_ID в переменных окружения.")
if not URL_CONDITIONS or not URL_ORDER:
    raise RuntimeError("Заполни URL_CONDITIONS и URL_ORDER в переменных окружения.")

# ====== STATES ======
WAIT_PHOTOS, WAIT_TEXT, WAIT_PRICE, WAIT_CONFIRM = range(4)

MAX_PHOTOS = 10
INVISIBLE = "\u2060"  # zero-width space, чтобы сообщение с кнопками выглядело пустым

@dataclass
class Draft:
    photo_file_ids: List[str] = field(default_factory=list)
    text: str = ""
    price: str = ""

drafts: Dict[int, Draft] = {}  # key = admin user id


def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


def build_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🛡 Условия покупки", url=URL_CONDITIONS),
            InlineKeyboardButton("📦 Оформить заказ", url=URL_ORDER),
        ]]
    )


def render_caption(d: Draft) -> str:
    # Можно менять формат как угодно
    # Важно: caption у фото ограничен ~1024 символами
    lines = []
    if d.text.strip():
        lines.append(d.text.strip())
    if d.price.strip():
        lines.append(f"\n💰 {d.price.strip()} ₽")
    return "\n".join(lines).strip()


async def newpost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    drafts[ADMIN_ID] = Draft()
    await update.message.reply_text(
        "Ок, собираем новый пост.\n"
        "1) Отправь фото (до 10).\n"
        "Когда закончишь — напиши /done\n"
        "Отмена в любой момент: /cancel"
    )
    return WAIT_PHOTOS


async def collect_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    d = drafts.get(ADMIN_ID)
    if not d:
        drafts[ADMIN_ID] = Draft()
        d = drafts[ADMIN_ID]

    if not update.message.photo:
        await update.message.reply_text("Пришли фото (как фото), либо /done когда закончил.")
        return WAIT_PHOTOS

    if len(d.photo_file_ids) >= MAX_PHOTOS:
        await update.message.reply_text(f"Уже {MAX_PHOTOS} фото. Больше нельзя. Напиши /done.")
        return WAIT_PHOTOS

    # берём самое большое фото (последний size)
    file_id = update.message.photo[-1].file_id
    d.photo_file_ids.append(file_id)

    await update.message.reply_text(f"Принял фото {len(d.photo_file_ids)}/{MAX_PHOTOS}. Ещё? Или /done")
    return WAIT_PHOTOS


async def done_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    d = drafts.get(ADMIN_ID)
    if not d or not d.photo_file_ids:
        await update.message.reply_text("Нужно хотя бы 1 фото. Пришли фото.")
        return WAIT_PHOTOS

    await update.message.reply_text("2) Теперь пришли текст описания (можно с эмодзи).")
    return WAIT_TEXT


async def collect_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    d = drafts.get(ADMIN_ID)
    if not d:
        await update.message.reply_text("Черновик потерялся. Начни заново: /newpost")
        return ConversationHandler.END

    d.text = (update.message.text or "").strip()
    if not d.text:
        await update.message.reply_text("Текст пустой. Пришли описание ещё раз.")
        return WAIT_TEXT

    await update.message.reply_text("3) Теперь пришли цену (только число), например: 2390")
    return WAIT_PRICE


async def collect_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    d = drafts.get(ADMIN_ID)
    if not d:
        await update.message.reply_text("Черновик потерялся. Начни заново: /newpost")
        return ConversationHandler.END

    raw = (update.message.text or "").strip().replace("₽", "").replace(" ", "")
    if not raw.isdigit():
        await update.message.reply_text("Цена должна быть числом. Пример: 2390")
        return WAIT_PRICE

    d.price = raw

    # Превью администратору: альбом без кнопок + следом кнопки
    caption = render_caption(d)

    media = []
    for i, fid in enumerate(d.photo_file_ids):
        if i == 0:
            media.append(InputMediaPhoto(media=fid, caption=caption))
        else:
            media.append(InputMediaPhoto(media=fid))

    await update.message.reply_text("Показываю превью 👇")
    await context.bot.send_media_group(chat_id=ADMIN_ID, media=media)
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=INVISIBLE,
        reply_markup=build_keyboard()
    )

    await update.message.reply_text("Публикуем в канал? Напиши: /publish или /cancel")
    return WAIT_CONFIRM


async def publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    d = drafts.get(ADMIN_ID)
    if not d or not d.photo_file_ids or not d.text or not d.price:
        await update.message.reply_text("Не вижу полного черновика. Начни заново: /newpost")
        return ConversationHandler.END

    caption = render_caption(d)

    media = []
    for i, fid in enumerate(d.photo_file_ids):
        if i == 0:
            media.append(InputMediaPhoto(media=fid, caption=caption))
        else:
            media.append(InputMediaPhoto(media=fid))

    # 1) Альбом
    await context.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
    # 2) Сообщение “пустышка” с кнопками (без “быстрые действия”)
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=INVISIBLE,
        reply_markup=build_keyboard()
    )

    drafts.pop(ADMIN_ID, None)
    await update.message.reply_text("Готово ✅ Опубликовано в канале.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if is_admin(update):
        drafts.pop(ADMIN_ID, None)
        await update.message.reply_text("Ок, отменил. Черновик очищен.")
    return ConversationHandler.END


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("newpost", newpost)],
        states={
            WAIT_PHOTOS: [
                MessageHandler(filters.PHOTO, collect_photos),
                CommandHandler("done", done_photos),
            ],
            WAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_text),
            ],
            WAIT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_price),
            ],
            WAIT_CONFIRM: [
                CommandHandler("publish", publish),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cancel))

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
