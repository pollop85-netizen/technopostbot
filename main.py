import os
import logging
import asyncpg
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime, timedelta, timezone, time as dtime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))        # numeric Telegram user id
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

URL_CONDITIONS = os.getenv("URL_CONDITIONS", "").strip()  # ссылка на пост "Условия покупки"
URL_ORDER = os.getenv("URL_ORDER", "").strip()            # ссылка/бот/форма "Оформить заказ"

# МСК
MSK = timezone(timedelta(hours=3))

# 2 поста ежедневно по МСК
SLOT_1 = dtime(hour=11, minute=50)
SLOT_2 = dtime(hour=12, minute=50)

if not BOT_TOKEN or not CHANNEL_ID or not ADMIN_ID or not DATABASE_URL:
    raise RuntimeError("Заполни BOT_TOKEN, CHANNEL_ID, ADMIN_ID, DATABASE_URL в переменных окружения.")
if not URL_CONDITIONS or not URL_ORDER:
    raise RuntimeError("Заполни URL_CONDITIONS и URL_ORDER в переменных окружения.")

# ====== STATES ======
WAIT_PHOTOS, WAIT_TEXT, WAIT_PRICE, WAIT_CONFIRM = range(4)
MAX_PHOTOS = 10

# “Пустое” сообщение под кнопки (чтобы НЕ было текста “быстрые действия”)
INVISIBLE = "\u2060"  # word-joiner (у тебя уже рабочий вариант)

@dataclass
class Draft:
    photo_file_ids: List[str] = field(default_factory=list)
    text: str = ""
    price: str = ""

drafts: Dict[int, Draft] = {}  # key = admin user id


def now_msk() -> datetime:
    return datetime.now(MSK)


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def build_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🛡 Условия покупки", url=URL_CONDITIONS),
            InlineKeyboardButton("📦 Оформить заказ", url=URL_ORDER),
        ]]
    )


def render_caption(d: Draft) -> str:
    # caption у первого фото ограничен ~1024 символами
    parts = []
    if d.text.strip():
        parts.append(d.text.strip())
    if d.price.strip():
        parts.append(f"\n💰 {d.price.strip()} ₽")
    return "\n".join(parts).strip()


async def init_db() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL,
                photo_ids TEXT[] NOT NULL,
                text TEXT NOT NULL,
                price TEXT NOT NULL,
                publish_time TIMESTAMPTZ NOT NULL,
                published BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    finally:
        await conn.close()


def parse_hhmm(s: str) -> Optional[dtime]:
    s = s.strip()
    if len(s) != 5 or s[2] != ":":
        return None
    hh, mm = s.split(":")
    if not (hh.isdigit() and mm.isdigit()):
        return None
    h = int(hh); m = int(mm)
    if 0 <= h <= 23 and 0 <= m <= 59:
        return dtime(hour=h, minute=m)
    return None


def next_slot_time(prefer: Optional[dtime] = None) -> datetime:
    """
    Возвращает ближайшее подходящее время публикации по МСК:
    - по умолчанию: ближайшее из 11:50/12:50
    - если prefer задан (например 11:50) — ближайший день, когда это время ещё впереди
    """
    n = now_msk()
    today = n.date()

    slots = [SLOT_1, SLOT_2]
    if prefer is not None:
        slots = [prefer]

    for sl in slots:
        candidate = datetime.combine(today, sl, tzinfo=MSK)
        if candidate > n:
            return candidate

    # если сегодня уже поздно — завтра
    tomorrow = today + timedelta(days=1)
    return datetime.combine(tomorrow, slots[0], tzinfo=MSK)


async def publish_to_channel(app: Application, d: Draft) -> None:
    caption = render_caption(d)

    media = []
    for i, fid in enumerate(d.photo_file_ids):
        if i == 0:
            media.append(InputMediaPhoto(media=fid, caption=caption))
        else:
            media.append(InputMediaPhoto(media=fid))

    # 1) Альбом
    await app.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
    # 2) Пустышка с кнопками
    await app.bot.send_message(
        chat_id=CHANNEL_ID,
        text=INVISIBLE,
        reply_markup=build_keyboard()
    )


async def worker_check_scheduled(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Фоновая проверка БД и публикация постов по расписанию.
    """
    app = context.application
    n = now_msk()

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("""
            SELECT id, photo_ids, text, price
            FROM scheduled_posts
            WHERE published = FALSE AND publish_time <= $1
            ORDER BY publish_time ASC
            LIMIT 5;
        """, n)

        for r in rows:
            post_id = r["id"]
            d = Draft(photo_file_ids=list(r["photo_ids"]), text=r["text"], price=r["price"])

            try:
                await publish_to_channel(app, d)
                await conn.execute("UPDATE scheduled_posts SET published=TRUE WHERE id=$1;", post_id)
                log.info("Scheduled post %s published", post_id)
            except Exception:
                log.exception("Failed to publish scheduled post id=%s", post_id)

    finally:
        await conn.close()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(
        "Я готов ✅\n\n"
        "Команды:\n"
        "/newpost — собрать новый пост\n"
        "/help — подсказка\n"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(
        "Подсказка команд 👇\n\n"
        "🧩 Создать пост:\n"
        "• /newpost\n"
        "• отправь фото (до 10)\n"
        "• /done\n"
        "• отправь текст\n"
        "• отправь цену числом\n\n"
        "📌 Дальше:\n"
        "• /publish — опубликовать сейчас\n"
        "• /schedule — поставить в ближайшее окно (11:50 или 12:50 МСК)\n"
        "• /schedule 11:50 — выбрать время явно\n"
        "• /cancel — отменить и очистить черновик"
    )


# ====== Conversation flow ======

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

    d = drafts.get(ADMIN_ID) or Draft()
    drafts[ADMIN_ID] = d

    if not update.message.photo:
        await update.message.reply_text("Пришли фото (как фото), либо /done когда закончил.")
        return WAIT_PHOTOS

    if len(d.photo_file_ids) >= MAX_PHOTOS:
        await update.message.reply_text(f"Уже {MAX_PHOTOS} фото. Больше нельзя. Напиши /done.")
        return WAIT_PHOTOS

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

    # Превью администратору
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

    await update.message.reply_text(
        "Что делаем дальше?\n"
        "• /publish — опубликовать сейчас\n"
        "• /schedule — поставить на ближайшее время (11:50/12:50 МСК)\n"
        "• /schedule 11:50 — выбрать время явно\n"
        "• /cancel — отменить"
    )
    return WAIT_CONFIRM


async def publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    d = drafts.get(ADMIN_ID)
    if not d or not d.photo_file_ids or not d.text or not d.price:
        await update.message.reply_text("Не вижу полного черновика. Начни заново: /newpost")
        return ConversationHandler.END

    await publish_to_channel(context.application, d)

    drafts.pop(ADMIN_ID, None)
    await update.message.reply_text("Готово ✅ Опубликовано в канале.")
    return ConversationHandler.END


async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /schedule            -> ближайшее окно 11:50/12:50 МСК
    /schedule 11:50      -> ближайший день/время
    """
    if not is_admin(update):
        return ConversationHandler.END

    d = drafts.get(ADMIN_ID)
    if not d or not d.photo_file_ids or not d.text or not d.price:
        await update.message.reply_text("Сначала собери пост: /newpost")
        return ConversationHandler.END

    prefer = None
    if context.args:
        t = parse_hhmm(" ".join(context.args))
        if not t:
            await update.message.reply_text("Формат времени: /schedule 11:50 (только HH:MM)")
            return WAIT_CONFIRM
        prefer = t

    publish_time = next_slot_time(prefer)

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            INSERT INTO scheduled_posts (admin_id, photo_ids, text, price, publish_time, published)
            VALUES ($1, $2, $3, $4, $5, FALSE);
        """, ADMIN_ID, d.photo_file_ids, d.text, d.price, publish_time)
    finally:
        await conn.close()

    drafts.pop(ADMIN_ID, None)

    await update.message.reply_text(
        f"Ок ✅ Поставил в очередь на {publish_time.strftime('%d.%m %H:%M')} (МСК).\n"
        "Если нужно собрать ещё — /newpost"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if is_admin(update):
        drafts.pop(ADMIN_ID, None)
        await update.message.reply_text("Ок, отменил. Черновик очищен.")
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", context.error)
    # админский мягкий фидбек (чтобы не молчало)
    try:
        if update and isinstance(update, Update) and update.effective_user and update.effective_user.id == ADMIN_ID:
            if update.message:
                await update.message.reply_text("⚠️ Ошибка на стороне бота. Я записал лог. Попробуй ещё раз.")
    except Exception:
        pass


async def post_init(app: Application) -> None:
    # создаём таблицу
    await init_db()
    # запускаем фоновую проверку каждые 20 секунд
    app.job_queue.run_repeating(worker_check_scheduled, interval=20, first=5)
    log.info("DB ready, scheduler started")


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

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
                CommandHandler("schedule", schedule_cmd),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("commands", cmd_help))

    app.add_handler(conv)
    app.add_handler(CommandHandler("publish", publish))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_error_handler(on_error)

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
