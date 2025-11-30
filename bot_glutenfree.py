import math
import sqlite3
import os
from contextlib import closing
from datetime import datetime
from import_app_restaurants import import_app_restaurants
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

# ==========================
# CONFIG
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DB_PATH = "restaurants.db"

PAGE_SIZE = 5  # Numero ristoranti per pagina

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

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS restaurants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                city TEXT NOT NULL,
                address TEXT,
                notes TEXT,
                source TEXT NOT NULL CHECK(source IN ('app', 'user')),
                lat REAL,
                lon REAL,
                rating REAL,
                last_update TEXT,
                types TEXT
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
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                min_rating REAL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event TEXT,
                created_at TEXT
            )
            """
        )

        conn.commit()


def log_usage(user_id: int, event: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usage_events (user_id, event, created_at) VALUES (?, ?, ?)",
            (user_id, event, datetime.utcnow().isoformat()),
        )
        conn.commit()


def extract_categories(rows):
    categories = set()
    for r in rows:
        types_raw = r[9] if len(r) > 9 else ""
        for p in types_raw.split("|"):
            p = p.strip().lower()
            if p in ["restaurant", "bar", "cafe", "bakery", "meal_takeaway", "meal_delivery", "store"]:
                categories.add(p)

    order = ["restaurant", "bar", "cafe", "bakery", "meal_takeaway", "meal_delivery", "store"]
    return [c for c in order if c in categories]


def get_user_settings(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT min_rating FROM user_settings WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return {"min_rating": row[0] if row else None}


def set_user_min_rating(user_id: int, value: Optional[float]):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        if value is None:
            cur.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        else:
            cur.execute(
                """
                INSERT INTO user_settings (user_id, min_rating)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET min_rating = ?
                """,
                (user_id, value, value),
            )
        conn.commit()
# ==========================
# LOGICA RISTORANTI
# ==========================

def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def eval_risk(notes: str) -> str:
    if not notes:
        return "⚪️ Info non sufficiente"

    t = notes.lower()

    safe = ["no contaminazione", "senza contaminazione", "cucina separata", "forno dedicato", "aic"]
    danger = ["contaminazione", "tracce di glutine", "stesso forno", "stessa friggitrice"]

    if any(k in t for k in safe):
        return "🟢 Attenzione alta al senza glutine"
    if any(k in t for k in danger):
        return "🟠 Possibile contaminazione, chiedi info al locale"
    return "🟡 Verifica sul posto"


# --------------------------
# QUERY RISTORANTI
# --------------------------

def query_by_city(city: str, user_id: int, category: str = None):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        if category:
            sql = """
            SELECT id, name, city, address, notes, rating, lat, lon, last_update, types
            FROM restaurants
            WHERE LOWER(city) = LOWER(?)
              AND LOWER(types) LIKE ?
            ORDER BY rating DESC, name ASC
            """
            cur.execute(sql, (city, f"%{category}%"))
        else:
            sql = """
            SELECT id, name, city, address, notes, rating, lat, lon, last_update, types
            FROM restaurants
            WHERE LOWER(city) = LOWER(?)
            ORDER BY rating DESC, name ASC
            """
            cur.execute(sql, (city,))

        rows = cur.fetchall()

    if min_rating is not None:
        rows = [r for r in rows if (r[5] is None or r[5] >= min_rating)]

    return rows


def query_nearby(lat, lon, user_id, max_distance_km=None):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, city, address, notes, rating, lat, lon, last_update, types
            FROM restaurants
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            """
        )
        rows = cur.fetchall()

    results = []
    for r in rows:
        dist = haversine_km(lat, lon, r[6], r[7])
        if dist is None:
            continue
        if max_distance_km and dist > max_distance_km:
            continue
        if min_rating is not None and r[5] is not None and r[5] < min_rating:
            continue

        results.append((dist, r))

    results.sort(key=lambda x: x[0])
    return [r[1] for r in results]


# --------------------------
# FORMATTING
# --------------------------

def format_restaurant_row(row, user_location=None):
    rid, name, city, address, notes, rating, lat, lon, last_update, types = row

    distance_str = ""
    if user_location and lat and lon:
        d = haversine_km(user_location[0], user_location[1], lat, lon)
        if d:
            distance_str = f"\n📏 Distanza: {d:.1f} km"

    rating_str = f"{rating:.1f}⭐" if rating else "n.d."
    risk = eval_risk(notes or "")

    maps = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ','+')}+{city}"

    text = (
        f"🍽 <b>{name}</b>\n"
        f"📍 {city} – {address or '-'}\n"
        f"⭐ Rating: {rating_str}\n"
        f"{distance_str}\n\n"
        f"<b>Note:</b> {notes or '—'}\n"
        f"<b>Rischio:</b> {risk}\n"
        f"\n🌍 <a href=\"{maps}\">Apri Google Maps</a>"
    )

    return text, rid


# --------------------------
# PAGINE RISULTATI (con categoria)
# --------------------------

def build_city_page(user_id: int, city: str, page: int, category: str = None):
    rows = query_by_city(city, user_id, category)
    if not rows:
        return None, None

    total = len(rows)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    subset = rows[start:end]

    cat_txt = f" (categoria <b>{category}</b>)" if category else ""
    msg = [f"🔎 Ho trovato <b>{total}</b> locali a <b>{city}</b>{cat_txt} — Pagina {page+1}/{total_pages}:\n"]

    kb_rows = []

    for i, r in enumerate(subset, start=start + 1):
        rid = r[0]
        rating_str = f"{r[5]:.1f}⭐" if r[5] else "n.d."
        msg.append(f"{i}. {r[1]} – {rating_str}")
        kb_rows.append([InlineKeyboardButton(f"Dettagli {i}", callback_data=f"details:{rid}")])

    nav = []
    cat_encoded = category if category else "ALL"
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"page:{city}:{page-1}:{cat_encoded}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"page:{city}:{page+1}:{cat_encoded}"))

    if nav:
        kb_rows.append(nav)

    kb = InlineKeyboardMarkup(kb_rows)
    return "\n".join(msg), kb

# ==========================
# KEYBOARD PRINCIPALE
# ==========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["⭐ I miei preferiti", "🛒 Shop"],
            ["⚙️ Filtri"]
        ],
        resize_keyboard=True,
    )


# ==========================
# START & HELP
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "start")

    await update.message.reply_text(
        f"Ciao {user.first_name}!\n\n"
        "Benvenuto in <b>GlutenFreeBot</b> 🧡\n"
        "Trova ristoranti affidabili e prodotti senza glutine.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandi disponibili:\n"
        "• Cerca per città\n"
        "• Cerca vicino a te\n"
        "• Preferiti ⭐\n"
        "• Shop Amazon 🛒",
        reply_markup=main_keyboard()
    )


# ==========================
# RICERCA PER CITTÀ
# ==========================

async def search_city(update: Update, context: ContextTypes.DEFAULT_TYPE, city_text: str):
    user = update.effective_user
    city = city_text.strip()

    log_usage(user.id, f"search_city:{city}")

    rows = query_by_city(city, user.id, category=None)
    if not rows:
        await update.message.reply_text(
            f"Nessun ristorante trovato per <b>{city}</b>.",
            parse_mode="HTML"
        )
        return

    # Estrai categorie dinamiche
    cats = extract_categories(rows)

    if cats:
        buttons = [[InlineKeyboardButton(c.title(), callback_data=f"cat:{city}:{c}")]
                   for c in cats]
        buttons.append([InlineKeyboardButton("Mostra tutti", callback_data=f"cat:{city}:ALL")])

        await update.message.reply_text(
            f"Trovati <b>{len(rows)}</b> locali a <b>{city}</b>.\n"
            "Seleziona una categoria:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # Se nessuna categoria → mostra tutto
    text, kb = build_city_page(user.id, city, page=0, category=None)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ==========================
# HANDLE TEXT (menu)
# ==========================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user = update.effective_user

    if text == "🔍 Cerca per città":
        log_usage(user.id, "menu_search_city")
        context.user_data["awaiting_city_search"] = True
        await update.message.reply_text("Scrivi il nome della città:")
        return

    if context.user_data.get("awaiting_city_search"):
        context.user_data["awaiting_city_search"] = False
        return await search_city(update, context, text)

    # ---- VICINO A ME ----
    if text == "📍 Vicino a me":
        log_usage(user.id, "menu_nearby")
        await update.message.reply_text(
            "Scegli il raggio di ricerca:",
            reply_markup=ReplyKeyboardMarkup(
                [["1 km", "3 km"], ["5 km", "10 km"]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        context.user_data["awaiting_radius"] = True
        return

    # scelta raggio
    if context.user_data.get("awaiting_radius") and text.endswith("km"):
        r = int(text.split()[0])
        context.user_data["nearby_radius_km"] = r
        context.user_data["awaiting_radius"] = False

        await update.message.reply_text(
            f"Raggio impostato a {r} km.\nOra inviami la posizione 📍",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Invia posizione 📍", request_location=True)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return

    if text == "⭐ I miei preferiti":
        return await my_favorites(update, context)

    if text == "⚙️ Filtri":
        return await show_filters(update, context)

    if text == "🛒 Shop":
        return await show_shop(update, context)

    # fallback
    await update.message.reply_text(
        "Non ho capito. Usa il menu:",
        reply_markup=main_keyboard()
    )


# ==========================
# VICINO A ME
# ==========================

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "location_sent")

    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude

    radius = context.user_data.get("nearby_radius_km", 5)
    rows = query_nearby(lat, lon, user.id, max_distance_km=radius)

    if not rows:
        await update.message.reply_text("Nessun locale trovato entro il raggio scelto.")
        return

    await update.message.reply_text(
        f"Trovati <b>{len(rows)}</b> locali entro {radius} km:",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

    for r in rows[:15]:
        text, rid = format_restaurant_row(r, user_location=(lat, lon))
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}")],
                [InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")]
            ]
        )
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ==========================
# PREFERITI
# ==========================

def get_favorites(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT r.id, r.name, r.city, r.address, r.notes, r.rating, r.lat, r.lon, r.last_update, r.types
            FROM favorites f
            JOIN restaurants r ON r.id = f.restaurant_id
            WHERE f.user_id = ?
            ORDER BY f.created_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


async def my_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    favs = get_favorites(user.id)

    if not favs:
        await update.message.reply_text("Nessun preferito ancora ⭐")
        return

    await update.message.reply_text(
        f"Hai <b>{len(favs)}</b> locali nei preferiti:",
        parse_mode="HTML"
    )

    for r in favs[:15]:
        text, rid = format_restaurant_row(r)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")]]
        )
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ==========================
# FILTRI (rating minimo)
# ==========================

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    s = get_user_settings(user.id)["min_rating"]
    txt = f"⭐ Rating minimo attuale: <b>{s if s else 'Nessuno'}</b>"

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("≥ 4.0", callback_data="filt:4.0"),
                InlineKeyboardButton("≥ 4.5", callback_data="filt:4.5"),
            ],
            [InlineKeyboardButton("❌ Nessun filtro", callback_data="filt:none")]
        ]
    )

    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=kb)


# ==========================
# CALLBACK HANDLER (part 1) — categorie & pagine
# ==========================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    # --- FILTRO CATEGORIA ---
    if data.startswith("cat:"):
        _, city, category = data.split(":", 2)
        if category == "ALL":
            category = None

        text, kb = build_city_page(user.id, city, 0, category)
        if text is None:
            await query.message.reply_text(
                f"Nessun locale per categoria <b>{category}</b> a <b>{city}</b>.",
                parse_mode="HTML"
            )
        else:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        await query.answer()
        return

    # --- PAGINAZIONE ---
    if data.startswith("page:"):
        _, city, page, category = data.split(":", 3)
        if category == "ALL":
            category = None

        page = int(page)
        text, kb = build_city_page(user.id, city, page, category)

        if text:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await query.message.reply_text("Nessun risultato disponibile.")
        await query.answer()
        return

# ==========================
# CALLBACK HANDLER (part 2)
# ==========================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    # ----------------------------
    # (LE PRIME DUE SEZIONI SONO IN BLOCCO 3)
    # cat:...
    # page:...
    # ----------------------------

    # --- DETTAGLI ---
    if data.startswith("details:"):
        rid = int(data.split(":")[1])

        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, name, city, address, notes, rating, lat, lon, last_update, types
                FROM restaurants
                WHERE id = ?
                """,
                (rid,),
            )
            r = cur.fetchone()

        if not r:
            await query.message.reply_text("Ristorante non trovato.")
            return

        text, rid = format_restaurant_row(r)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}")],
                [InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")]
            ]
        )

        await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        await query.answer()
        return

    # --- FAVORITO ---
    if data.startswith("fav:"):
        rid = int(data.split(":")[1])
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO favorites (user_id, restaurant_id, created_at) VALUES (?, ?, ?)",
                (user.id, rid, datetime.utcnow().isoformat()),
            )
            conn.commit()

        await query.answer("Aggiunto ai preferiti ⭐")
        return

    # --- FOTO ---
    if data.startswith("photo:"):
        rid = int(data.split(":")[1])
        pending_photo_for_user[user.id] = rid

        await query.message.reply_text("Inviami una foto del locale/piatto 📷")
        await query.answer()
        return

    # --- FILTRO RATING ---
    if data.startswith("filt:"):
        _, val = data.split(":", 1)
        if val == "none":
            set_user_min_rating(user.id, None)
            await query.message.reply_text("Filtro eliminato.")
        else:
            set_user_min_rating(user.id, float(val))
            await query.message.reply_text(f"Rating minimo impostato a {val}⭐")
        await query.answer()
        return

    # --- SHOP PAGINE ---
    if data.startswith("shop:"):
        page = int(data.split(":")[1])
        await send_shop_page(update, page)
        await query.answer()
        return


# ==========================
# FOTO UPLOAD
# ==========================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_photo_for_user:
        await update.message.reply_text(
            "Prima seleziona un ristorante → poi premi '📷 Aggiungi foto'."
        )
        return

    rid = pending_photo_for_user.pop(user.id)
    file_id = update.message.photo[-1].file_id

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO photos (restaurant_id, file_id, user_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (rid, file_id, user.id, datetime.utcnow().isoformat()),
        )
        conn.commit()

    await update.message.reply_text("Foto aggiunta 📷 Grazie!")
    log_usage(user.id, f"add_photo:{rid}")


# ==========================
# SHOP AMAZON
# ==========================

SHOP_PRODUCTS = [
    {
        "name": "Mulino Bianco Senza Glutine — Biscotti",
        "url": "https://amzn.to/4iuLj4T"
    },
    {
        "name": "Biscotti senza glutine – prodotto 2",
        "url": "https://www.amazon.it/Mulino-Bianco-Biscotti-Frollini-Cioccolato/dp/B07FKN8YRS?tag=glutenfreeita-21"
    },
]


async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_shop_page(update, page=0)


async def send_shop_page(update_or_query, page: int):
    per_page = 5
    total = len(SHOP_PRODUCTS)
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))

    start = page * per_page
    end = start + per_page
    sub = SHOP_PRODUCTS[start:end]

    msg = "<b>🛒 Prodotti consigliati senza glutine</b>\n\n"
    for i, p in enumerate(sub, start=1):
        msg += f"{i}. <a href=\"{p['url']}\">{p['name']}</a>\n"

    kb = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"shop:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"shop:{page+1}"))
    if nav:
        kb.append(nav)

    reply_markup = InlineKeyboardMarkup(kb) if kb else None

    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(msg, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await update_or_query.edit_message_text(msg, parse_mode="HTML", reply_markup=reply_markup)


# ==========================
# MAIN
# ==========================

def build_application():
    ensure_schema()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


# ==========================
# RUN
# ==========================

if __name__ == "__main__":
    print("🔄 Importo ristoranti da app_restaurants.csv...")
    try:
        import_app_restaurants()
        print("✅ Import completato.")
    except Exception as e:
        print("⚠️ Errore import ristoranti:", e)

    application = build_application()
    print("🤖 GlutenFreeBot avviato...")
    application.run_polling()
