import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

import asyncpg
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

# ============ LOGGING ============
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("technopostbot")

# ============ ENV ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

URL_CONDITIONS = os.getenv("URL_CONDITIONS", "").strip()
URL_ORDER = os.getenv("URL_ORDER", "").strip()

if not BOT_TOKEN or not CHANNEL_ID or not ADMIN_ID:
    raise RuntimeError("Заполни BOT_TOKEN, CHANNEL_ID, ADMIN_ID в Railway Variables.")
if not DATABASE_URL:
    raise RuntimeError("Нет DATABASE_URL (Railway Postgres должен быть подключён).")
if not URL_CONDITIONS or not URL_ORDER:
    raise RuntimeError("Заполни URL_CONDITIONS и URL_ORDER в Railway Variables.")

# Всегда по Москве
MSK = timezone(timedelta(hours=3))

# Невидимый символ, который Telegram принимает как текст
INVISIBLE = "\u2060"

# ============ STATES ============
WAIT_PHOTOS, WAIT_TEXT, WAIT_PRICE, WAIT_CONFIRM = range(4)
MAX_PHOTOS = 10

@dataclass
class Draft:
    photo_file_ids: List[str] = field(default_factory=list)
    text: str = ""
    price: str = ""

drafts: Dict[int, Draft] = {}  # key = admin user id


# ============ HELP ============
HELP_TEXT = (
    "Команды бота:\n"
    "/newpost — собрать пост (фото → текст → цена)\n"
    "/publish — опубликовать текущий черновик сразу\n"
    "/schedule — поставить текущий черновик в ближайший слот (11:50 / 12:50 МСК)\n"
    "/queue — показать очередь отложенных\n"
    "/unschedule <id> — удалить отложенный пост по ID\n"
    "/cancel — отменить текущую сборку\n"
    "/help — подсказка\n\n"
    "Публикации идут строго по МСК, независимо от твоего региона."
)


# ============ UTILS ============
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
    # caption у фото ограничен ~1024 символами
    parts = []
    if d.text.strip():
        parts.append(d.text.strip())
    if str(d.price).strip():
        parts.append(f"💰 {str(d.price).strip()} ₽")
    return "\n\n".join(parts).strip()


def now_msk() -> datetime:
    return datetime.now(MSK)


def next_slot_msk(dt: datetime) -> datetime:
    """
    Берём ближайший слот 11:50 или 12:50 по МСК.
    Если оба прошли — завтра 11:50.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)

    slots = [(11, 50), (12, 50)]
    today = dt.date()

    for h, m in slots:
        t = datetime(today.year, today.month, today.day, h, m, tzinfo=MSK)
        if t > dt:
            return t

    tomorrow = today + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, 11, 50, tzinfo=MSK)


async def get_pool(app: Application) -> asyncpg.Pool:
    pool = app.bot_data.get("db_pool")
    if not pool:
        raise RuntimeError("DB pool not initialized")
    return pool


async def init_db(app: Application) -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    app.bot_data["db_pool"] = pool

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id SERIAL PRIMARY KEY,
                photo_ids TEXT[] NOT NULL,
                text TEXT NOT NULL,
                price TEXT NOT NULL,
                publish_time TIMESTAMP NOT NULL,
                published BOOLEAN DEFAULT FALSE
            );
        """)

    log.info("DB initialized")


async def close_db(app: Application) -> None:
    pool: Optional[asyncpg.Pool] = app.bot_data.get("db_pool")
    if pool:
        await pool.close()
        log.info("DB closed")


async def publish_to_channel(app: Application, d: Draft) -> None:
    caption = render_caption(d)

    media = []
    for i, fid in enumerate(d.photo_file_ids[:MAX_PHOTOS]):
        if i == 0:
            media.append(InputMediaPhoto(media=fid, caption=caption))
        else:
            media.append(InputMediaPhoto(media=fid))

    # 1) Альбом
    await app.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
    # 2) Сообщение-пустышка с кнопками (без “быстрые действия”)
    await app.bot.send_message(
        chat_id=CHANNEL_ID,
        text=INVISIBLE,
        reply_markup=build_keyboard()
    )


# ============ COMMANDS ============
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text("Я готов ✅\n\n" + HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(HELP_TEXT)


# ============ NEWPOST FLOW ============
async def newpost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    drafts[ADMIN_ID] = Draft()
    await update.message.reply_text(
        "Ок, собираем новый пост.\n"
        "1) Отправь фото (до 10).\n"
        "Когда закончишь — напиши /done\n"
        "Отмена: /cancel"
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

    await update.message.reply_text("2) Теперь пришли текст описания.")
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

    # превью админу
    caption = render_caption(d)
    media = []
    for i, fid in enumerate(d.photo_file_ids):
        if i == 0:
            media.append(InputMediaPhoto(media=fid, caption=caption))
        else:
            media.append(InputMediaPhoto(media=fid))

    await update.message.reply_text("Показываю превью 👇")
    await context.bot.send_media_group(chat_id=ADMIN_ID, media=media)
    await context.bot.send_message(chat_id=ADMIN_ID, text=INVISIBLE, reply_markup=build_keyboard())

    await update.message.reply_text(
        "Готово. Что дальше?\n"
        "/publish — опубликовать сейчас\n"
        "/schedule — в ближайший слот (11:50/12:50 МСК)\n"
        "/cancel — отмена"
    )
    return WAIT_CONFIRM


async def publish_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    d = drafts.get(ADMIN_ID)
    if not d or not d.photo_file_ids or not d.text or not d.price:
        await update.message.reply_text("Не вижу полного черновика. Начни: /newpost")
        return ConversationHandler.END

    app: Application = context.application
    await publish_to_channel(app, d)

    drafts.pop(ADMIN_ID, None)
    await update.message.reply_text("✅ Опубликовано в канале.")
    return ConversationHandler.END


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END

    d = drafts.get(ADMIN_ID)
    if not d or not d.photo_file_ids or not d.text or not d.price:
        await update.message.reply_text("Не вижу полного черновика. Начни: /newpost")
        return ConversationHandler.END

    slot = next_slot_msk(now_msk())

    pool = await get_pool(context.application)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scheduled_posts (photo_ids, text, price, publish_time, published)
            VALUES ($1, $2, $3, $4, FALSE)
            """,
            d.photo_file_ids,
            d.text,
            str(d.price),
            slot.replace(tzinfo=None)  # храним как МСК без tz
        )

    drafts.pop(ADMIN_ID, None)
    await update.message.reply_text(f"✅ Поставил в очередь на {slot.strftime('%d.%m %H:%M')} (МСК).")
    return ConversationHandler.END


async def queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    pool = await get_pool(context.application)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, publish_time, price, left(text, 40) AS t
            FROM scheduled_posts
            WHERE published = FALSE
            ORDER BY publish_time ASC
            LIMIT 20
            """
        )

    if not rows:
        await update.message.reply_text("Очередь пустая ✅")
        return

    lines = ["Очередь (МСК):"]
    for r in rows:
        pt: datetime = r["publish_time"]  # считаем что это МСК
        lines.append(f"#{r['id']} — {pt.strftime('%d.%m %H:%M')} — {r['price']} ₽ — {r['t']}…")
    await update.message.reply_text("\n".join(lines))


async def unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Используй: /unschedule 123")
        return

    post_id = int(context.args[0])

    pool = await get_pool(context.application)
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM scheduled_posts WHERE id=$1 AND published=FALSE",
            post_id
        )

    # res выглядит как "DELETE 1" или "DELETE 0"
    if res.endswith("1"):
        await update.message.reply_text(f"Удалил отложенный пост #{post_id} ✅")
    else:
        await update.message.reply_text(f"Не нашёл пост #{post_id} (возможно уже опубликован/удалён).")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if is_admin(update):
        drafts.pop(ADMIN_ID, None)
        await update.message.reply_text("Ок, отменил. Черновик очищен.")
    return ConversationHandler.END


# ============ AUTOPUBLISH JOB ============
async def autopublish_job(context: ContextTypes.DEFAULT_TYPE):
    app: Application = context.application
    pool = await get_pool(app)

    now_naive = now_msk().replace(tzinfo=None)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, photo_ids, text, price
            FROM scheduled_posts
            WHERE published = FALSE AND publish_time <= $1
            ORDER BY publish_time ASC
            LIMIT 1
            """,
            now_naive
        )

        if not row:
            return

        post_id = row["id"]
        d = Draft(
            photo_file_ids=list(row["photo_ids"]),
            text=row["text"],
            price=row["price"]
        )

        # публикуем
        await publish_to_channel(app, d)

        # помечаем опубликованным
        await conn.execute("UPDATE scheduled_posts SET published=TRUE WHERE id=$1", post_id)
        log.info("Autopublished scheduled post id=%s", post_id)


# ============ APP LIFECYCLE ============
async def post_init(app: Application) -> None:
    await init_db(app)
    # каждые 25 секунд проверяем очередь
    app.job_queue.run_repeating(autopublish_job, interval=25, first=10)
    log.info("Autopublish job scheduled")


async def post_shutdown(app: Application) -> None:
    await close_db(app)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("queue", queue))
    app.add_handler(CommandHandler("unschedule", unschedule))

    # Диалог сборки поста
    conv = ConversationHandler(
        entry_points=[CommandHandler("newpost", newpost)],
        states={
            WAIT_PHOTOS: [
                MessageHandler(filters.PHOTO, collect_photos),
                CommandHandler("done", done_photos),
                CommandHandler("cancel", cancel),
            ],
            WAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_text),
                CommandHandler("cancel", cancel),
            ],
            WAIT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_price),
                CommandHandler("cancel", cancel),
            ],
            WAIT_CONFIRM: [
                CommandHandler("publish", publish_now),
                CommandHandler("schedule", schedule),
                CommandHandler("cancel", cancel),
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
