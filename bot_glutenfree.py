import math
import sqlite3
import os
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
    ConversationHandler,
    ContextTypes,
    filters,
)

from import_app_restaurants import import_app_restaurants

# ==========================
# CONFIG
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "restaurants.db"
PAGE_SIZE = 5

ADD_NAME, ADD_CITY, ADD_ADDRESS, ADD_NOTES = range(4)

pending_photo_for_user = {}

# ==========================
# DB UTILS
# ==========================

def get_conn():
    return sqlite3.connect(DB_PATH)

def ensure_schema():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("""
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
            phone TEXT,
            last_update TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            points INTEGER DEFAULT 0,
            title TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER,
            restaurant_id INTEGER,
            created_at TEXT,
            PRIMARY KEY (user_id, restaurant_id)
        )
        """)

        conn.commit()

# ==========================
# UTILS
# ==========================

def add_points(user_id: int, pts: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO user_stats (user_id, points)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET points = points + ?
        """, (user_id, pts, pts))
        conn.commit()

def get_user_stats(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT points, title FROM user_stats WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0, "👤 Utente"
        return row[0], row[1]

def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

# ==========================
# QUERY
# ==========================

def query_by_city(city: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
        SELECT id, name, city, address, notes, rating, lat, lon, phone, last_update
        FROM restaurants
        WHERE LOWER(city) = LOWER(?)
        ORDER BY rating DESC NULLS LAST
        """, (city,))
        return cur.fetchall()

def query_nearby(lat: float, lon: float, limit=15):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("""
        SELECT id, name, city, address, notes, rating, lat, lon, phone, last_update
        FROM restaurants
        WHERE lat IS NOT NULL AND lon IS NOT NULL
        """)
        rows = cur.fetchall()

    enriched = []
    for r in rows:
        dist = haversine_km(lat, lon, r[6], r[7])
        if dist is not None:
            enriched.append((dist, r))

    enriched.sort(key=lambda x: x[0])
    return [e[1] for e in enriched[:limit]]

# ==========================
# FORMAT RISTORANTE (MODIFICA CHIAVE)
# ==========================

def format_restaurant_row(row, user_location=None):
    (
        rid, name, city, address, notes,
        rating, lat, lon, phone, last_update
    ) = row

    rating_str = f"{rating:.1f}⭐" if rating else "n.d."

    distance_str = ""
    if user_location and lat and lon:
        d = haversine_km(user_location[0], user_location[1], lat, lon)
        if d:
            distance_str = f"\n📏 Distanza: {d:.1f} km"

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"

    # 📞 TELEFONO
    if phone:
        phone_clean = phone.replace(" ", "")
        phone_line = f'\n📞 <a href="tel:{phone_clean}">Chiama il ristorante</a>'
    else:
        phone_line = "\n📞 Contatta direttamente il ristorante per conferma"

    disclaimer = (
        "\n\nℹ️ <b>Nota importante</b>\n"
        "Questo ristorante è mostrato in base a recensioni e informazioni pubbliche disponibili online.\n"
        "Le condizioni per il senza glutine possono variare nel tempo "
        "(cambi di gestione, menu o procedure).\n\n"
        "👉 Ti consigliamo sempre di contattare direttamente il ristorante prima di andare."
    )

    text = (
        f"🍽 <b>{name}</b>\n"
        f"📍 <b>{city}</b> – {address or 'Indirizzo non disponibile'}\n"
        f"⭐ Rating medio Google: {rating_str}"
        f"{distance_str}\n\n"
        f"<b>Note:</b> {notes or '—'}"
        f"{disclaimer}"
        f"{phone_line}\n"
        f"\n🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )

    return text, rid

# ==========================
# UI
# ==========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["⭐ Preferiti", "🛒 Shop"],
        ],
        resize_keyboard=True,
    )

# ==========================
# HANDLERS
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Ciao 👋 benvenuto in <b>GlutenFreeBot</b> 🧡\n\n"
        "Qui trovi ristoranti e locali segnalati\n"
        "per chi vive davvero <b>senza glutine</b>.\n\n"
        "🍽 Cerca per città\n"
        "📍 Trova locali vicino a te\n"
        "⭐ Salva i tuoi preferiti\n"
        "🛒 Shop (in arrivo)\n\n"
        "✨ Ma non finisce qui…\n\n"
        "Su Instagram raccontiamo il lato umano del gluten free:\n"
        "consigli veri, esperienze reali, nuove scoperte.\n\n"
        "📸 <a href=\"https://www.instagram.com/glutenfreebot?igsh=bzYxdXd3cDF0MTly&utm_source=qr\">@glutenfreebot</a>\n\n"
        "Seguilo. Qui il bot ti aiuta, lì ti ispiriamo."
    )
    await update.message.reply_text(
        msg,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
        disable_web_page_preview=True
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🔍 Cerca per città":
        context.user_data["await_city"] = True
        await update.message.reply_text("Scrivi il nome della città:")
        return

    if context.user_data.get("await_city"):
        context.user_data["await_city"] = False
        city = text.strip()
        rows = query_by_city(city)
        if not rows:
            await update.message.reply_text(
                "😔 Al momento non ho ristoranti per questa città.\n"
                "Vuoi segnalarla? Faremo il possibile per aggiornare il database.",
                reply_markup=main_keyboard()
            )
            return

        for r in rows:
            msg, rid = format_restaurant_row(r)
            await update.message.reply_text(
                msg,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
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
            "🛒 <b>Shop Gluten Free</b>\n\n"
            "Al momento non ci sono prodotti gluten free segnalati.\n\n"
            "👉 Entra nel gruppo: @GlutenfreeItalia_bot",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
        return

    await update.message.reply_text(
        "Usa i pulsanti qui sotto 👇",
        reply_markup=main_keyboard()
    )

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    rows = query_nearby(loc.latitude, loc.longitude)

    if not rows:
        await update.message.reply_text(
            "😔 Nessun ristorante trovato vicino a te.\n"
            "Faremo il possibile per migliorare la copertura.",
            reply_markup=main_keyboard()
        )
        return

    for r in rows:
        msg, _ = format_restaurant_row(r, (loc.latitude, loc.longitude))
        await update.message.reply_text(
            msg,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    await update.message.reply_text("Puoi continuare dal menu 👇", reply_markup=main_keyboard())

# ==========================
# MAIN
# ==========================

def build_application():
    ensure_schema()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app

if __name__ == "__main__":
    print("🔄 Importo ristoranti da app_restaurants.csv...")
    import_app_restaurants()
    app = build_application()
    print("🤖 Bot avviato")
    app.run_polling()
