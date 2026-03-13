
import os
import sqlite3
import logging
from contextlib import closing
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    LabeledPrice,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)

# --- ENV VARIABLES ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://glutenfree-miniapp.vercel.app")

# Premium configuration
PREMIUM_PRICE_STARS = int(os.getenv("PREMIUM_PRICE_STARS", "299"))
PREMIUM_DURATION_DAYS = int(os.getenv("PREMIUM_DURATION_DAYS", "30"))

# Free searches per day
FREE_SEARCHES_PER_DAY = 3

# Database
DB_PATH = "restaurants.db"


# -------------------------
# DATABASE
# -------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # Premium users
        cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            plan TEXT,
            status TEXT,
            starts_at TEXT,
            expires_at TEXT
        )
        """)

        # Daily searches
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_searches (
            user_id INTEGER,
            date TEXT,
            searches INTEGER,
            PRIMARY KEY(user_id, date)
        )
        """)

        conn.commit()


# -------------------------
# PREMIUM SYSTEM
# -------------------------
def is_user_premium(user_id: int) -> bool:

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=?",
            (user_id,),
        )

        row = cur.fetchone()

        if not row:
            return False

        expires = datetime.fromisoformat(row["expires_at"])
        return expires > datetime.utcnow()


def set_user_premium(user_id: int):

    start = datetime.utcnow()
    expires = start + timedelta(days=PREMIUM_DURATION_DAYS)

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("""
        INSERT INTO subscriptions (user_id,plan,status,starts_at,expires_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
        status='active',
        starts_at=?,
        expires_at=?
        """,(
            user_id,
            "premium",
            "active",
            start.isoformat(),
            expires.isoformat(),
            start.isoformat(),
            expires.isoformat(),
        ))

        conn.commit()


# -------------------------
# SEARCH LIMIT SYSTEM
# -------------------------
def can_user_search(user_id: int):

    if is_user_premium(user_id):
        return True

    today = datetime.utcnow().date().isoformat()

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("""
        SELECT searches FROM user_searches
        WHERE user_id=? AND date=?
        """, (user_id, today))

        row = cur.fetchone()

        if not row:
            cur.execute("""
            INSERT INTO user_searches (user_id,date,searches)
            VALUES (?,?,1)
            """, (user_id, today))

            conn.commit()
            return True

        if row["searches"] >= FREE_SEARCHES_PER_DAY:
            return False

        cur.execute("""
        UPDATE user_searches
        SET searches = searches + 1
        WHERE user_id=? AND date=?
        """, (user_id, today))

        conn.commit()

        return True


# -------------------------
# KEYBOARDS
# -------------------------
def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🌍 Apri GlutenFree App",
                web_app=WebAppInfo(url=MINIAPP_URL)
            )
        ],
        [
            InlineKeyboardButton(
                "💎 Abbonati Premium",
                callback_data="premium"
            )
        ]
    ])


# -------------------------
# BOT COMMANDS
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Benvenuto in GlutenFree Italia 🍽\n\n"
        "Usa la nostra Mini App per trovare ristoranti senza glutine.",
        reply_markup=main_keyboard(),
    )


async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="GlutenFree Premium",
        description="Ricerche illimitate per 30 giorni",
        payload="premium_month",
        currency="XTR",
        prices=[LabeledPrice("Premium", PREMIUM_PRICE_STARS)],
        provider_token="",
        start_parameter="premium",
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.data == "premium":
        await premium(update, context)


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    set_user_premium(user.id)

    await update.message.reply_text(
        "🎉 Premium attivato! Hai ricerche illimitate per 30 giorni."
    )


# -------------------------
# APPLICATION BUILDER
# -------------------------
def build_application():

    ensure_schema()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(PreCheckoutQueryHandler(precheckout))
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment)
    )

    return application
