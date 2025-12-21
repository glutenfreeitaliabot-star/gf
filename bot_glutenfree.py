import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Optional, List

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from import_app_restaurants import import_app_restaurants

# ==========================
# CONFIG
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DB_PATH = "restaurants.db"

PAGE_SIZE = 5

pending_photo_for_user = {}

# ==========================
# DB
# ==========================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS restaurants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                city TEXT NOT NULL,
                address TEXT,
                notes TEXT,
                source TEXT NOT NULL,
                lat REAL,
                lon REAL,
                rating REAL,
                last_update TEXT,
                types TEXT,
                phone TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                restaurant_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, restaurant_id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                min_rating REAL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                restaurant_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new'
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                restaurant_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event TEXT NOT NULL,
                city TEXT,
                restaurant_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.commit()


def log_usage(user_id, event, city=None, restaurant_id=None):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO usage_events (user_id, event, city, restaurant_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, event, city, restaurant_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


# ==========================
# UTILS
# ==========================

def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def format_restaurant_detail(r, user_location=None):
    rating = f"{r['rating']:.1f}⭐" if r["rating"] is not None else "n.d."
    distance = ""
    if user_location and r["lat"] and r["lon"]:
        d = haversine_km(user_location[0], user_location[1], r["lat"], r["lon"])
        if d is not None:
            distance = f"\n📏 Distanza: {d:.1f} km"

    phone_line = (
        f"\n📞 Telefono: <b>{r['phone']}</b>\n👉 Tocca il numero per chiamare"
        if r["phone"]
        else "\n📞 Telefono: <b>non disponibile</b>"
    )

    maps_url = f"https://www.google.com/maps/search/?api=1&query={r['name'].replace(' ', '+')}+{r['city'].replace(' ', '+')}"

    text = (
        f"🍽 <b>{r['name']}</b>\n"
        f"📍 {r['city']} – {r['address'] or 'Indirizzo non disponibile'}\n"
        f"⭐ Rating Google: {rating}"
        f"{distance}"
        f"{phone_line}\n\n"
        f"<b>Note:</b> {r['notes'] or '—'}\n\n"
        "ℹ️ <b>Nota importante</b>\n"
        "Il locale è mostrato in base a recensioni pubbliche online.\n"
        "Le informazioni possono cambiare nel tempo.\n"
        "👉 Contatta sempre il ristorante prima di recarti sul posto.\n\n"
        f"🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )
    return text


# ==========================
# QUERY
# ==========================

def query_by_city(city, user_id):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM restaurants
            WHERE LOWER(city) = LOWER(?)
            ORDER BY rating DESC NULLS LAST, name
            """,
            (city,),
        )
        return cur.fetchall()


def query_nearby(lat, lon, radius_km):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM restaurants WHERE lat IS NOT NULL AND lon IS NOT NULL"
        )
        rows = cur.fetchall()

    results = []
    for r in rows:
        d = haversine_km(lat, lon, r["lat"], r["lon"])
        if d is not None and d <= radius_km:
            results.append((d, r))

    results.sort(key=lambda x: x[0])
    return [r[1] for r in results]


# ==========================
# UI
# ==========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["⭐ I miei preferiti", "⚙️ Filtri"],
            ["🛒 Shop"],
        ],
        resize_keyboard=True,
    )


# ==========================
# HANDLERS
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_usage(update.effective_user.id, "start")
    msg = (
        "Ciao 👋 benvenuto in <b>GlutenFreeBot</b> 🧡\n\n"
        "Trova ristoranti con recensioni che parlano di gluten free.\n\n"
        "📸 Seguici su Instagram:\n"
        "<a href=\"https://www.instagram.com/glutenfreebot\">@glutenfreebot</a>"
    )
    await update.message.reply_text(
        msg, parse_mode="HTML", reply_markup=main_keyboard()
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_CHAT_ID or str(update.effective_user.id) != str(ADMIN_CHAT_ID):
        return

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM usage_events")
        users = cur.fetchone()[0]
        cur.execute("SELECT event, COUNT(*) FROM usage_events GROUP BY event")
        events = cur.fetchall()

    text = f"👥 Utenti: {users}\n\n"
    for e in events:
        text += f"{e[0]}: {e[1]}\n"

    await update.message.reply_text(text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user

    if text == "🔍 Cerca per città":
        context.user_data["awaiting_city"] = True
        await update.message.reply_text("Scrivi il nome della città:")
        return

    if context.user_data.get("awaiting_city"):
        context.user_data["awaiting_city"] = False
        city = text.strip()
        log_usage(user.id, "search_city", city=city)
        rows = query_by_city(city, user.id)

        if not rows:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📩 Suggerisci città", callback_data=f"suggest:{city}")]]
            )
            await update.message.reply_text(
                f"Nessun risultato per <b>{city}</b>.\nVuoi segnalarla?",
                parse_mode="HTML",
                reply_markup=kb,
            )
            await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
            return

        msg = f"Ho trovato {len(rows)} ristoranti a {city}:\n\n"
        kb_rows = []
        for i, r in enumerate(rows[:PAGE_SIZE], 1):
            msg += f"{i}. {r['name']}\n"
            kb_rows.append(
                [InlineKeyboardButton(f"Dettagli {i}", callback_data=f"details:{r['id']}")]
            )

        await update.message.reply_text(
            msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb_rows)
        )
        await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
        return

    if text == "📍 Vicino a me":
        await update.message.reply_text(
            "Invia la tua posizione 📍",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Invia posizione 📍", request_location=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return

    if text == "🛒 Shop":
        await update.message.reply_text(
            "🛒 <b>Shop</b>\n\n"
            "Al momento non ci sono prodotti gluten free segnalati.\n\n"
            "👉 @GlutenfreeItalia_bot",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text("Usa il menu 👇", reply_markup=main_keyboard())


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    radius = 5.0
    rows = query_nearby(loc.latitude, loc.longitude, radius)

    if not rows:
        await update.message.reply_text(
            "Nessun locale trovato vicino a te.", reply_markup=main_keyboard()
        )
        return

    msg = f"Ho trovato {len(rows)} locali vicino a te:\n\n"
    kb_rows = []
    for i, r in enumerate(rows[:PAGE_SIZE], 1):
        msg += f"{i}. {r['name']} – {r['city']}\n"
        kb_rows.append(
            [InlineKeyboardButton(f"Dettagli {i}", callback_data=f"details:{r['id']}")]
        )

    await update.message.reply_text(
        msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb_rows)
    )
    await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data.startswith("details:"):
        rid = int(data.split(":")[1])
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM restaurants WHERE id = ?", (rid,))
            r = cur.fetchone()

        if not r:
            await query.message.reply_text("Locale non trovato.")
            return

        log_usage(query.from_user.id, "details_click", restaurant_id=rid)
        text = format_restaurant_detail(r)
        await query.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("suggest:"):
        city = data.split(":")[1]
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=f"📩 Città suggerita: {city}",
            )
        await query.message.reply_text("Grazie per la segnalazione 🙏")
        return


# ==========================
# MAIN
# ==========================

def build_application():
    ensure_schema()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(callback_handler))

    return app


if __name__ == "__main__":
    print("🔄 Import ristoranti...")
    import_app_restaurants()
    application = build_application()
    application.run_polling()
