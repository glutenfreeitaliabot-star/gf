import math
import sqlite3
import os
from contextlib import closing
from datetime import datetime
from typing import Optional

from import_app_restaurants import import_app_restaurants

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

# ==========================
# CONFIG
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # per /stats e notifiche suggerimenti
DB_PATH = "restaurants.db"

PAGE_SIZE = 5

# pending foto per ristorante
pending_photo_for_user: dict[int, int] = {}


# ==========================
# DB UTILS
# ==========================

def get_conn():
    return sqlite3.connect(DB_PATH)


def ensure_schema():
    """
    Crea / aggiorna le tabelle di supporto.
    La tabella 'restaurants' è gestita da import_app_restaurants.py
    e NON viene toccata qui.
    """
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # Preferiti
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

        # Foto
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

        # Impostazioni utente
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                min_rating REAL
            )
            """
        )

        # Eventi di utilizzo (/stats)
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

        # Suggerimenti città
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS suggested_cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                city TEXT,
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
# GEO / RISCHIO / FORMAT
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


def format_restaurant_row(row, user_location=None):
    """
    row atteso:
    (id, name, city, address, notes, rating, lat, lon, last_update, [types opzionale])
    """
    rid, name, city, address, notes, rating, lat, lon, last_update = row[:9]
    types = row[9] if len(row) > 9 else ""

    distance_str = ""
    if user_location and lat is not None and lon is not None:
        d = haversine_km(user_location[0], user_location[1], lat, lon)
        if d is not None:
            if d < 1:
                distance_str = f"\n📏 Distanza: {d*1000:.0f} m"
            else:
                distance_str = f"\n📏 Distanza: {d:.1f} km"

    rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."
    risk = eval_risk(notes or "")

    maps = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ','+')}+{city.replace(' ','+')}"

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


# ==========================
# QUERY RISTORANTI
# ==========================

def _restaurants_has_types(cur) -> bool:
    cur.execute("PRAGMA table_info(restaurants)")
    cols = [c[1].lower() for c in cur.fetchall()]
    return "types" in cols


def query_by_city(city: str, user_id: int, category: Optional[str] = None):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        has_types = _restaurants_has_types(cur)

        select_sql = "SELECT id, name, city, address, notes, rating, lat, lon, last_update"
        if has_types:
            select_sql += ", types"
        select_sql += " FROM restaurants WHERE LOWER(city) = LOWER(?)"
        params = [city]

        if category and has_types:
            select_sql += " AND LOWER(types) LIKE ?"
            params.append(f"%{category.lower()}%")

        select_sql += " ORDER BY rating DESC, name ASC"

        cur.execute(select_sql, params)
        rows = cur.fetchall()

    if min_rating is not None:
        rows = [r for r in rows if (r[5] is None or r[5] >= min_rating)]

    return rows


def query_nearby(lat: float, lon: float, user_id: int, max_distance_km: Optional[float] = None):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        has_types = _restaurants_has_types(cur)

        select_sql = "SELECT id, name, city, address, notes, rating, lat, lon, last_update"
        if has_types:
            select_sql += ", types"
        select_sql += " FROM restaurants WHERE lat IS NOT NULL AND lon IS NOT NULL"

        cur.execute(select_sql)
        rows = cur.fetchall()

    enriched = []
    for r in rows:
        rid, name, city, address, notes, rating, rlat, rlon, last_update = r[:9]
        d = haversine_km(lat, lon, rlat, rlon)
        if d is None:
            continue
        if max_distance_km is not None and d > max_distance_km:
            continue
        if min_rating is not None and rating is not None and rating < min_rating:
            continue
        enriched.append((d, r))

    enriched.sort(key=lambda x: x[0])
    return [e[1] for e in enriched]


def extract_categories(rows):
    """
    Estrae categorie dai 'types' se presenti.
    rows può avere 9 o 10 colonne; se 10, la 10a è types.
    """
    categories = set()
    for r in rows:
        types = r[9] if len(r) > 9 else ""
        if not types:
            continue
        parts = [p.strip().lower() for p in types.split("|")]
        for p in parts:
            if p in ["restaurant", "bar", "cafe", "bakery", "meal_takeaway", "meal_delivery", "store"]:
                categories.add(p)

    order = ["restaurant", "bar", "cafe", "bakery", "meal_takeaway", "meal_delivery", "store"]
    return [c for c in order if c in categories]


def build_city_page(user_id: int, city: str, page: int, category: Optional[str] = None):
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
    msg_lines = [
        f"🔎 Ho trovato <b>{total}</b> locali a <b>{city}</b>{cat_txt} — Pagina {page+1}/{total_pages}:\n"
    ]

    kb_rows = []

    for i, r in enumerate(subset, start=start + 1):
        rid = r[0]
        rating = r[5]
        rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."
        msg_lines.append(f"{i}. {r[1]} – {rating_str}")
        kb_rows.append([InlineKeyboardButton(f"Dettagli {i}", callback_data=f"details:{rid}")])

    nav = []
    category_token = category if category else "ALL"
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"page:{city}:{page-1}:{category_token}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"page:{city}:{page+1}:{category_token}"))
    if nav:
        kb_rows.append(nav)

    kb = InlineKeyboardMarkup(kb_rows)
    return "\n".join(msg_lines), kb


def build_nearby_page(user_id: int, lat: float, lon: float, radius_km: float, page: int):
    """
    Lista paginata dei locali vicino alla posizione.
    """
    rows = query_nearby(lat, lon, user_id, max_distance_km=radius_km)
    if not rows:
        return None, None

    total = len(rows)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    subset = rows[start:end]

    msg_lines = [
        f"📍 Locali entro <b>{radius_km} km</b> — trovati <b>{total}</b> (pagina {page+1}/{total_pages}):\n"
    ]

    kb_rows = []

    for i, r in enumerate(subset, start=start + 1):
        rid, name, city, address, notes, rating, rlat, rlon, last_update = r[:9]
        rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."
        d = haversine_km(lat, lon, rlat, rlon)
        if d is None:
            dist_str = "n.d."
        elif d < 1:
            dist_str = f"{d*1000:.0f} m"
        else:
            dist_str = f"{d:.1f} km"

        msg_lines.append(f"{i}. {name} – {city} – {rating_str} – {dist_str}")
        kb_rows.append([InlineKeyboardButton(f"Dettagli {i}", callback_data=f"details:{rid}")])

    # navigazione
    nav = []
    lat_str = f"{lat:.5f}"
    lon_str = f"{lon:.5f}"
    radius_str = f"{radius_km:.2f}"
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"nearpage:{lat_str}:{lon_str}:{radius_str}:{page-1}",
            )
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"nearpage:{lat_str}:{lon_str}:{radius_str}:{page+1}",
            )
        )
    if nav:
        kb_rows.append(nav)

    kb = InlineKeyboardMarkup(kb_rows)
    return "\n".join(msg_lines), kb


# ==========================
# FAVORITI & FOTO
# ==========================

def get_favorites(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        has_types = _restaurants_has_types(cur)

        select_sql = "SELECT r.id, r.name, r.city, r.address, r.notes, r.rating, r.lat, r.lon, r.last_update"
        if has_types:
            select_sql += ", r.types"
        select_sql += """
            FROM favorites f
            JOIN restaurants r ON r.id = f.restaurant_id
            WHERE f.user_id = ?
            ORDER BY f.created_at DESC
        """

        cur.execute(select_sql, (user_id,))
        rows = cur.fetchall()
    return rows


def add_favorite(user_id: int, restaurant_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO favorites (user_id, restaurant_id, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id, restaurant_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


def add_photo_record(user_id: int, restaurant_id: int, file_id: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO photos (restaurant_id, file_id, user_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (restaurant_id, file_id, user_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


# ==========================
# KEYBOARD & START/HELP
# ==========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["⭐ I miei preferiti", "🛒 Shop"],
            ["💡 Suggerisci città", "⚙️ Filtri"],
        ],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "start")

    await update.message.reply_text(
        f"Ciao {user.first_name}!\n\n"
        "Benvenuto in <b>GlutenFreeBot</b> 🧡\n"
        "Ti aiuto a trovare ristoranti e prodotti senza glutine.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Puoi usare il menu qui sotto per:\n"
        "• Cercare per città\n"
        "• Cercare vicino a te\n"
        "• Gestire i preferiti\n"
        "• Aprire lo Shop Amazon 🛒\n"
        "• Suggerire nuove città",
        reply_markup=main_keyboard(),
    )


# ==========================
# RICERCA PER CITTÀ
# ==========================

async def search_city(update: Update, context: ContextTypes.DEFAULT_TYPE, city_text: str):
    user = update.effective_user
    city = city_text.strip()
    if not city:
        await update.message.reply_text("Scrivi una città valida.")
        return

    log_usage(user.id, f"search_city:{city}")

    rows = query_by_city(city, user.id, category=None)
    if not rows:
        await update.message.reply_text(
            f"Non ho ancora locali per <b>{city}</b>.",
            parse_mode="HTML",
        )
        return

    categories = extract_categories(rows)
    if categories:
        buttons = [
            [InlineKeyboardButton(c.title(), callback_data=f"cat:{city}:{c}")]
            for c in categories
        ]
        buttons.append(
            [InlineKeyboardButton("Mostra tutti", callback_data=f"cat:{city}:ALL")]
        )
        await update.message.reply_text(
            f"Trovati <b>{len(rows)}</b> locali a <b>{city}</b>.\n"
            "Vuoi filtrare per tipo di locale?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        text, kb = build_city_page(user.id, city, page=0, category=None)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ==========================
# HANDLE TESTO / MENU
# ==========================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Fase: attendo città
    if context.user_data.get("awaiting_city_search"):
        context.user_data["awaiting_city_search"] = False
        await search_city(update, context, text)
        return

    # Fase: attendo raggio
    if context.user_data.get("awaiting_radius") and text.endswith("km"):
        try:
            radius = int(text.split()[0])
        except ValueError:
            radius = 5
        context.user_data["nearby_radius_km"] = radius
        context.user_data["awaiting_radius"] = False
        await update.message.reply_text(
            f"Raggio impostato a {radius} km.\nOra inviami la posizione 📍",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Invia posizione 📍", request_location=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return

    # Menu principale
    if text == "🔍 Cerca per città":
        log_usage(user.id, "menu_search_city")
        context.user_data["awaiting_city_search"] = True
        await update.message.reply_text("Scrivi il nome della città:")
        return

    if text == "📍 Vicino a me":
        log_usage(user.id, "menu_nearby")
        context.user_data["awaiting_radius"] = True
        await update.message.reply_text(
            "Scegli il raggio di ricerca:",
            reply_markup=ReplyKeyboardMarkup(
                [["1 km", "3 km"], ["5 km", "10 km"]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return

    if text == "⭐ I miei preferiti":
        await my_favorites(update, context)
        return

    if text == "⚙️ Filtri":
        await show_filters(update, context)
        return

    if text == "🛒 Shop":
        await show_shop(update, context)
        return

    if text == "💡 Suggerisci città":
        context.user_data["awaiting_suggest_city"] = True
        await update.message.reply_text("Scrivimi la città che vorresti vedere analizzata:")
        return

    if context.user_data.get("awaiting_suggest_city"):
        context.user_data["awaiting_suggest_city"] = False
        await handle_suggest_city(update, context, text)
        return

    # fallback
    await update.message.reply_text(
        "Non ho capito. Usa il menu qui sotto 👇",
        reply_markup=main_keyboard(),
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

    text, kb = build_nearby_page(user.id, lat, lon, radius_km=radius, page=0)
    if text is None:
        await update.message.reply_text(
            f"Nessun locale trovato entro {radius} km.",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=kb,
    )


# ==========================
# PREFERITI
# ==========================

async def my_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = get_favorites(user.id)

    if not rows:
        await update.message.reply_text(
            "Non hai ancora preferiti ⭐\nQuando trovi un locale interessante usa il bottone '⭐ Preferito'.",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text(
        f"Hai <b>{len(rows)}</b> locali nei preferiti:",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    for r in rows[:15]:
        text, rid = format_restaurant_row(r)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")]]
        )
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ==========================
# FILTRI
# ==========================

async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    settings = get_user_settings(user.id)
    min_rating = settings.get("min_rating")
    current = f"{min_rating:.1f}⭐" if min_rating is not None else "nessuno"

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("≥ 4.0⭐", callback_data="filt:4.0"),
                InlineKeyboardButton("≥ 4.5⭐", callback_data="filt:4.5"),
            ],
            [InlineKeyboardButton("❌ Nessun filtro", callback_data="filt:none")],
        ]
    )

    await update.message.reply_text(
        f"Rating minimo attuale: <b>{current}</b>\nScegli un'impostazione:",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ==========================
# SUGGERISCI CITTÀ
# ==========================

async def handle_suggest_city(update: Update, context: ContextTypes.DEFAULT_TYPE, city_text: str):
    user = update.effective_user
    city = city_text.strip()
    if not city:
        await update.message.reply_text("Città non valida.")
        return

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO suggested_cities (user_id, city, created_at)
            VALUES (?, ?, ?)
            """,
            (user.id, city, datetime.utcnow().isoformat()),
        )
        conn.commit()

    log_usage(user.id, f"suggest_city:{city}")

    await update.message.reply_text(
        f"Grazie! Ho registrato il suggerimento per <b>{city}</b>.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    if ADMIN_CHAT_ID:
        try:
            app = context.application
            await app.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=f"💡 Nuovo suggerimento città da {user.id} ({user.first_name}): {city}",
            )
        except Exception:
            pass


# ==========================
# SHOP AMAZON
# ==========================

SHOP_PRODUCTS = [
    {
        "name": "Mulino Bianco Senza Glutine — Biscotti",
        "url": "https://amzn.to/4iuLj4T",
    },
    {
        "name": "Biscotti senza glutine – prodotto 2",
        "url": "https://www.amazon.it/Mulino-Bianco-Biscotti-Frollini-Cioccolato/dp/B07FKN8YRS?tag=glutenfreeita-21",
    },
]


async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_shop_page(update, page=0)


async def send_shop_page(update_or_query, page: int):
    per_page = 5
    total = len(SHOP_PRODUCTS)
    if total == 0:
        msg = "Al momento non ho ancora prodotti da mostrarti."
        if isinstance(update_or_query, Update):
            await update_or_query.message.reply_text(msg, reply_markup=main_keyboard())
        else:
            await update_or_query.edit_message_text(msg)
        return

    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))

    start = page * per_page
    end = start + per_page
    sub = SHOP_PRODUCTS[start:end]

    msg = "<b>🛒 Prodotti consigliati senza glutine</b>\n\n"
    for i, p in enumerate(sub, start=1):
        msg += f"{i}. <a href=\"{p['url']}\">{p['name']}</a>\n"

    kb_rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"shop:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"shop:{page+1}"))
    if nav:
        kb_rows.append(nav)
    kb = InlineKeyboardMarkup(kb_rows) if kb_rows else None

    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(
            msg, parse_mode="HTML", reply_markup=kb
        )
    else:
        await update_or_query.edit_message_text(
            msg, parse_mode="HTML", reply_markup=kb
        )


# ==========================
# FOTO
# ==========================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_photo_for_user:
        await update.message.reply_text(
            "Per collegare una foto, prima apri i dettagli di un locale e tocca '📷 Aggiungi foto'."
        )
        return

    rid = pending_photo_for_user.pop(user.id)
    file_id = update.message.photo[-1].file_id

    add_photo_record(user.id, rid, file_id)
    log_usage(user.id, f"add_photo:{rid}")

    await update.message.reply_text("📷 Foto salvata, grazie!", reply_markup=main_keyboard())


# ==========================
# /STATS — STATISTICHE USO BOT
# ==========================

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Se è configurato ADMIN_CHAT_ID, limito l'accesso
    if ADMIN_CHAT_ID and str(user.id) != str(ADMIN_CHAT_ID):
        await update.message.reply_text("Questo comando è riservato all'amministratore del bot.")
        return

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # Totale eventi e utenti unici
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT user_id) FROM usage_events")
        row = cur.fetchone() or (0, 0)
        total_events = row[0] or 0
        total_users = row[1] or 0

        # Eventi più usati
        cur.execute(
            """
            SELECT event, COUNT(*) AS c
            FROM usage_events
            GROUP BY event
            ORDER BY c DESC
            LIMIT 10
            """
        )
        events_rows = cur.fetchall()

        # Città più cercate
        cur.execute(
            """
            SELECT event, COUNT(*) AS c
            FROM usage_events
            WHERE event LIKE 'search_city:%'
            GROUP BY event
            ORDER BY c DESC
            LIMIT 20
            """
        )
        city_rows = cur.fetchall()

    # Eventi
    if events_rows:
        events_lines = [f"• {ev}: {c}" for ev, c in events_rows]
        events_block = "\n".join(events_lines)
    else:
        events_block = "Nessun evento registrato."

    # Città
    if city_rows:
        city_counts = {}
        for ev, c in city_rows:
            parts = ev.split(":", 1)
            city = parts[1] if len(parts) == 2 else ev
            city_counts[city] = city_counts.get(city, 0) + c

        top_cities = sorted(city_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        city_lines = [f"• {city}: {cnt}" for city, cnt in top_cities]
        city_block = "\n".join(city_lines)
    else:
        city_block = "Nessuna ricerca città registrata."

    msg = (
        "<b>📊 Stats GlutenFreeBot</b>\n\n"
        f"• Eventi totali: <b>{total_events}</b>\n"
        f"• Utenti unici: <b>{total_users}</b>\n\n"
        "<b>🔝 Eventi più usati</b>\n"
        f"{events_block}\n\n"
        "<b>🏙 Città più cercate</b>\n"
        f"{city_block}"
    )

    await update.message.reply_text(msg, parse_mode="HTML")


# ==========================
# CALLBACK HANDLER
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
        text, kb = build_city_page(user.id, city, page=0, category=category)
        if text is None:
            await query.message.reply_text(
                f"Nessun locale trovato per questa categoria a <b>{city}</b>.",
                parse_mode="HTML",
            )
        else:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        await query.answer()
        return

    # --- PAGINE CITTÀ ---
    if data.startswith("page:"):
        _, city, page_str, category_token = data.split(":", 3)
        category = None if category_token == "ALL" else category_token
        page = int(page_str)
        text, kb = build_city_page(user.id, city, page=page, category=category)
        if text:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await query.message.reply_text("Nessun risultato disponibile.")
        await query.answer()
        return

    # --- PAGINE VICINO A ME ---
    if data.startswith("nearpage:"):
        # nearpage:<lat>:<lon>:<radius_km>:<page>
        _, lat_s, lon_s, rad_s, page_s = data.split(":", 4)
        lat = float(lat_s)
        lon = float(lon_s)
        radius_km = float(rad_s)
        page = int(page_s)

        text, kb = build_nearby_page(user.id, lat, lon, radius_km=radius_km, page=page)
        if text:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await query.message.reply_text("Nessun risultato disponibile per questo raggio.")
        await query.answer()
        return

    # --- DETTAGLI ---
    if data.startswith("details:"):
        rid = int(data.split(":")[1])
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            has_types = _restaurants_has_types(cur)

            select_sql = "SELECT id, name, city, address, notes, rating, lat, lon, last_update"
            if has_types:
                select_sql += ", types"
            select_sql += " FROM restaurants WHERE id = ?"

            cur.execute(select_sql, (rid,))
            row = cur.fetchone()

        if not row:
            await query.message.reply_text("Ristorante non trovato.")
            await query.answer()
            return

        text, rid = format_restaurant_row(row)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}")],
                [InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")],
            ]
        )
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        await query.answer()
        return

    # --- PREFERITO ---
    if data.startswith("fav:"):
        rid = int(data.split(":")[1])
        add_favorite(user.id, rid)
        log_usage(user.id, f"fav:{rid}")
        await query.answer("Aggiunto ai preferiti ⭐")
        return

    # --- FOTO ---
    if data.startswith("photo:"):
        rid = int(data.split(":")[1])
        pending_photo_for_user[user.id] = rid
        await query.message.reply_text("Inviami ora una foto del locale/piatto 📷")
        await query.answer()
        return

    # --- FILTRO RATING ---
    if data.startswith("filt:"):
        _, val = data.split(":", 1)
        if val == "none":
            set_user_min_rating(user.id, None)
            await query.message.reply_text("Filtro rating rimosso.")
        else:
            try:
                mr = float(val)
            except ValueError:
                mr = None
            set_user_min_rating(user.id, mr)
            await query.message.reply_text(f"Rating minimo impostato a {val}⭐.")
        await query.answer()
        return

    # --- SHOP PAGINE ---
    if data.startswith("shop:"):
        _, page_str = data.split(":", 1)
        page = int(page_str)
        await send_shop_page(query, page=page)
        await query.answer()
        return


# ==========================
# BUILD APPLICATION
# ==========================

def build_application():
    ensure_schema()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))

    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


# ==========================
# MAIN
# ==========================

if __name__ == "__main__":
    print("🔄 Importo ristoranti da app_restaurants.csv...")
    try:
        import_app_restaurants()
        print("✅ Import completato.")
    except Exception as e:
        print("⚠️ Errore durante l'import dei ristoranti:", e)

    application = build_application()
    print("🤖 GlutenFreeBot avviato...")
    application.run_polling()
