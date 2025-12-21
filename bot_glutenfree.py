import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import httpx

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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # obbligatoria per /stats e notifiche
DB_PATH = "restaurants.db"

PAGE_SIZE = 5

pending_photo_for_user: Dict[int, int] = {}

# ==========================
# DB
# ==========================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_cols(cur, table: str) -> set:
    try:
        cur.execute(f"PRAGMA table_info({table})")
        return {r[1].lower() for r in cur.fetchall()}
    except Exception:
        return set()


def ensure_schema():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # restaurants (base)
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
                last_update TEXT
            )
            """
        )

        # aggiunta colonne se mancanti
        cols = _table_cols(cur, "restaurants")
        def add_col_if_missing(col: str, ddl: str):
            nonlocal cols
            if col.lower() not in cols:
                try:
                    cur.execute(f"ALTER TABLE restaurants ADD COLUMN {ddl}")
                    cols = _table_cols(cur, "restaurants")
                except Exception:
                    pass

        add_col_if_missing("types", "types TEXT")
        add_col_if_missing("phone", "phone TEXT")

        # favorites
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

        # user_settings
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                min_rating REAL
            )
            """
        )

        # reports
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

        # photos
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

        # usage_events (log per /stats + click details)
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

        # cache reverse geocode (per ridurre chiamate esterne)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS geo_cache (
                key TEXT PRIMARY KEY,
                city TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.commit()


def log_usage(user_id: int, event: str, city: Optional[str] = None, restaurant_id: Optional[int] = None):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usage_events (user_id, event, city, restaurant_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, event, city, restaurant_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_user_settings(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT min_rating FROM user_settings WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return {"min_rating": row["min_rating"] if row else None}


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


def get_favorites(user_id: int) -> List[sqlite3.Row]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT r.*
            FROM favorites f
            JOIN restaurants r ON r.id = f.restaurant_id
            WHERE f.user_id = ?
            ORDER BY f.created_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


def add_report(user_id: int, restaurant_id: int, reason: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO reports (user_id, restaurant_id, reason, created_at, status)
            VALUES (?, ?, ?, ?, 'new')
            """,
            (user_id, restaurant_id, reason, datetime.utcnow().isoformat()),
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


def get_photos_for_restaurant(restaurant_id: int) -> List[str]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT file_id FROM photos
            WHERE restaurant_id = ?
            ORDER BY created_at DESC
            LIMIT 3
            """,
            (restaurant_id,),
        )
        return [r["file_id"] for r in cur.fetchall()]


# ==========================
# GEO + FORMATTING
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


def normalize_phone_for_tel(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    p = str(phone).strip()
    if not p:
        return None

    cleaned = []
    for ch in p:
        if ch.isdigit() or ch == "+":
            cleaned.append(ch)
    p2 = "".join(cleaned)

    if p2 in ("", "+"):
        return None

    if p2.startswith("00"):
        p2 = "+" + p2[2:]

    if not p2.startswith("+"):
        p2 = "+39" + p2

    return p2


def disclaimer_text() -> str:
    return (
        "\n\nℹ️ <b>Nota importante</b>\n"
        "Mostriamo questo locale in base a recensioni e informazioni pubbliche online.\n"
        "Le condizioni per il senza glutine possono cambiare (menu, gestione, procedure).\n\n"
        "👉 Contatta sempre il ristorante prima di andare."
    )


def format_restaurant_detail(r: sqlite3.Row, user_location=None) -> Tuple[str, Optional[str]]:
    name = r["name"]
    city = r["city"]
    address = r["address"] or "Indirizzo non disponibile"
    notes = r["notes"] or "—"
    rating = r["rating"]
    last_update = r["last_update"]
    lat = r["lat"]
    lon = r["lon"]
    phone = r["phone"] if "phone" in r.keys() else None

    rating_str = f"{float(rating):.1f}⭐" if rating is not None else "n.d."
    update_str = f" (aggiornato: {last_update})" if last_update else ""

    distance_str = ""
    if user_location and lat is not None and lon is not None:
        d = haversine_km(user_location[0], user_location[1], lat, lon)
        if d is not None:
            distance_str = f"\n📏 Distanza: {d*1000:.0f} m" if d < 1 else f"\n📏 Distanza: {d:.1f} km"

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"

    if phone and str(phone).strip():
        phone_line = f"\n📞 Telefono: <b>{phone}</b>"
    else:
        phone_line = "\n📞 Telefono: <b>non disponibile</b>"

    text = (
        f"🍽 <b>{name}</b>\n"
        f"📍 <b>{city}</b> – {address}\n"
        f"⭐ Rating medio Google: {rating_str}{update_str}"
        f"{distance_str}"
        f"{phone_line}\n\n"
        f"<b>Note:</b> {notes}"
        f"{disclaimer_text()}\n"
        f"\n🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )

    return text, phone


# ==========================
# REVERSE GEOCODING (per city stats da posizione)
# ==========================

def _geo_cache_key(lat: float, lon: float) -> str:
    # arrotondo a ~1km per cache (0.01 gradi)
    return f"{round(lat, 2)}|{round(lon, 2)}"


async def reverse_geocode_city(lat: float, lon: float) -> Optional[str]:
    key = _geo_cache_key(lat, lon)

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT city FROM geo_cache WHERE key = ?", (key,))
        row = cur.fetchone()
        if row and row["city"]:
            return row["city"]

    # Nominatim (OSM) - best effort
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1},
                headers={"User-Agent": "GlutenFreeBot/1.0 (contact: admin)"},
            )
            data = resp.json()
            addr = data.get("address", {})
            city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
            if city:
                with closing(get_conn()) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO geo_cache (key, city, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET city = excluded.city, updated_at = excluded.updated_at",
                        (key, city, datetime.utcnow().isoformat()),
                    )
                    conn.commit()
                return city
    except Exception:
        return None

    return None


# ==========================
# QUERY
# ==========================

def query_by_city(city: str, user_id: int) -> List[sqlite3.Row]:
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT *
            FROM restaurants
            WHERE LOWER(city) = LOWER(?)
            ORDER BY (rating IS NULL) ASC, rating DESC, name ASC
            """,
            (city,),
        )
        rows = cur.fetchall()

    if min_rating is not None:
        rows = [r for r in rows if (r["rating"] is None or float(r["rating"]) >= float(min_rating))]

    return rows


def query_nearby(lat: float, lon: float, user_id: int, radius_km: float, max_results: int = 200) -> List[sqlite3.Row]:
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT *
            FROM restaurants
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            """
        )
        rows = cur.fetchall()

    enriched: List[Tuple[float, sqlite3.Row]] = []
    for r in rows:
        d = haversine_km(lat, lon, r["lat"], r["lon"])
        if d is None or d > radius_km:
            continue
        if min_rating is not None and r["rating"] is not None and float(r["rating"]) < float(min_rating):
            continue
        enriched.append((d, r))

    enriched.sort(key=lambda x: x[0])
    return [e[1] for e in enriched[:max_results]]


# ==========================
# PAGINATION BUILDERS
# ==========================

def build_city_page(user_id: int, city: str, page: int):
    rows = query_by_city(city, user_id)
    if not rows:
        return None, None

    total = len(rows)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    subset = rows[start:end]

    lines = [f"🔎 Ho trovato <b>{total}</b> locali a <b>{city}</b> (pagina {page+1}/{total_pages}):", ""]
    kb_rows = []

    for idx, r in enumerate(subset, start=1):
        rid = int(r["id"])
        name = r["name"]
        rating = r["rating"]
        rating_str = f"{float(rating):.1f}⭐" if rating is not None else "n.d."
        display_n = start + idx  # 1..N globale
        lines.append(f"{display_n}. {name} – {rating_str}")
        kb_rows.append([InlineKeyboardButton(f"Dettagli {display_n}", callback_data=f"details:{rid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"page:{city}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"page:{city}:{page+1}"))
    if nav:
        kb_rows.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


def build_nearby_page(user_id: int, lat: float, lon: float, radius_km: float, page: int):
    rows = query_nearby(lat, lon, user_id, radius_km)
    if not rows:
        return None, None

    total = len(rows)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    subset = rows[start:end]

    lines = [f"📍 Locali entro <b>{radius_km:g} km</b> — trovati <b>{total}</b> (pagina {page+1}/{total_pages}):", ""]
    kb_rows = []

    for idx, r in enumerate(subset, start=1):
        rid = int(r["id"])
        name = r["name"]
        city = r["city"]
        rating = r["rating"]
        rating_str = f"{float(rating):.1f}⭐" if rating is not None else "n.d."
        d = haversine_km(lat, lon, r["lat"], r["lon"])
        dist_str = "n.d." if d is None else (f"{d*1000:.0f} m" if d < 1 else f"{d:.1f} km")
        display_n = start + idx
        lines.append(f"{display_n}. {name} – {city} – {rating_str} – {dist_str}")
        kb_rows.append([InlineKeyboardButton(f"Dettagli {display_n}", callback_data=f"details:{rid}")])

    nav = []
    lat_s = f"{lat:.5f}"
    lon_s = f"{lon:.5f}"
    rad_s = f"{radius_km:.2f}"
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"nearpage:{lat_s}:{lon_s}:{rad_s}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"nearpage:{lat_s}:{lon_s}:{rad_s}:{page+1}"))
    if nav:
        kb_rows.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


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
# COMMANDS
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "start")

    msg = (
        "Ciao 👋 benvenuto in <b>GlutenFreeBot</b> 🧡\n\n"
        "Qui trovi locali individuati da recensioni pubbliche che citano esperienze gluten free.\n\n"
        "✨ Effetto wow: seguici anche su Instagram\n"
        "📸 <a href=\"https://www.instagram.com/glutenfreebot?igsh=bzYxdXd3cDF0MTly&utm_source=qr\">@glutenfreebot</a>\n\n"
        "Usa i pulsanti qui sotto 👇"
    )
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=main_keyboard(), disable_web_page_preview=True)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # solo admin
    if not ADMIN_CHAT_ID or str(update.effective_user.id) != str(ADMIN_CHAT_ID):
        return

    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(DISTINCT user_id) AS n FROM usage_events")
        users = cur.fetchone()["n"] or 0

        cur.execute("SELECT COUNT(*) AS n FROM usage_events")
        events = cur.fetchone()["n"] or 0

        cur.execute("""
            SELECT event, COUNT(*) AS n
            FROM usage_events
            GROUP BY event
            ORDER BY n DESC
            LIMIT 10
        """)
        top_events = cur.fetchall()

        cur.execute("""
            SELECT city, COUNT(*) AS n
            FROM usage_events
            WHERE event = 'search_city' AND city IS NOT NULL AND city <> ''
            GROUP BY city
            ORDER BY n DESC
            LIMIT 10
        """)
        top_city_search = cur.fetchall()

        cur.execute("""
            SELECT city, COUNT(*) AS n
            FROM usage_events
            WHERE event = 'search_nearby' AND city IS NOT NULL AND city <> ''
            GROUP BY city
            ORDER BY n DESC
            LIMIT 10
        """)
        top_city_nearby = cur.fetchall()

        cur.execute("""
            SELECT city, restaurant_id, COUNT(*) AS n
            FROM usage_events
            WHERE event = 'details_click' AND restaurant_id IS NOT NULL
            GROUP BY city, restaurant_id
            ORDER BY n DESC
            LIMIT 10
        """)
        top_clicks = cur.fetchall()

    def fmt_list(rows, a, b):
        if not rows:
            return "—"
        return "\n".join([f"• {r[a]}: <b>{r[b]}</b>" for r in rows])

    msg = (
        "<b>📊 STATISTICHE BOT</b>\n\n"
        f"👥 Utenti unici: <b>{users}</b>\n"
        f"🧾 Eventi totali: <b>{events}</b>\n\n"
        "<b>Top funzioni</b>\n"
        f"{fmt_list(top_events, 'event', 'n')}\n\n"
        "<b>Top città cercate (testo)</b>\n"
        f"{fmt_list(top_city_search, 'city', 'n')}\n\n"
        "<b>Top città ricavate da posizione</b>\n"
        f"{fmt_list(top_city_nearby, 'city', 'n')}\n\n"
        "<b>Top click su dettagli</b>\n"
        + (fmt_list(top_clicks, 'restaurant_id', 'n') if top_clicks else "—")
    )

    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)


# ==========================
# TEXT HANDLER
# ==========================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    if text == "🔍 Cerca per città":
        context.user_data["awaiting_city_search"] = True
        await update.message.reply_text("Scrivimi il nome della città (es: Bari).", reply_markup=main_keyboard())
        return

    if context.user_data.get("awaiting_city_search"):
        context.user_data["awaiting_city_search"] = False
        city = text.strip()
        log_usage(user.id, "search_city", city=city)
        context.user_data["last_city_search"] = city

        page_text, kb = build_city_page(user.id, city, page=0)
        if page_text is None:
            # tasto suggerisci -> SOLO NOTIFICA ADMIN (niente DB)
            suggest_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📩 Suggerisci questa città", callback_data=f"suggestcity:{city}")]]
            )
            await update.message.reply_text(
                f"😔 Al momento non ho locali per <b>{city}</b>.\n\nVuoi segnalarla all'admin?",
                parse_mode="HTML",
                reply_markup=suggest_kb,
            )
            await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
            return

        await update.message.reply_text(page_text, parse_mode="HTML", reply_markup=kb)
        await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
        return

    if text == "📍 Vicino a me":
        # raggio: NON lo tocchiamo (come richiesto)
        context.user_data["awaiting_radius"] = True
        await update.message.reply_text(
            "Scegli il raggio di ricerca:",
            reply_markup=ReplyKeyboardMarkup([["1 km", "3 km"], ["5 km", "10 km"]], resize_keyboard=True, one_time_keyboard=True),
        )
        return

    if context.user_data.get("awaiting_radius") and text.endswith("km"):
        try:
            radius = float(text.split()[0])
        except Exception:
            radius = 5.0
        context.user_data["nearby_radius_km"] = radius
        context.user_data["awaiting_radius"] = False

        await update.message.reply_text(
            f"Raggio impostato a {radius:g} km.\nOra inviami la posizione 📍",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Invia posizione 📍", request_location=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return

    if text == "⭐ I miei preferiti":
        favs = get_favorites(user.id)
        if not favs:
            await update.message.reply_text("Non hai ancora preferiti ⭐", reply_markup=main_keyboard())
            return

        await update.message.reply_text(f"Hai <b>{len(favs)}</b> preferiti:", parse_mode="HTML", reply_markup=main_keyboard())

        # li mostro in forma paginata semplice: 10 messaggi max
        for r in favs[:10]:
            detail_text, phone = format_restaurant_detail(r)
            tel = normalize_phone_for_tel(phone)
            kb_rows = []
            if tel:
                kb_rows.append([InlineKeyboardButton("📞 Chiama il ristorante", url=f"tel:{tel}")])
            kb_rows.append([InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{int(r['id'])}")])
            kb = InlineKeyboardMarkup(kb_rows)
            await update.message.reply_text(detail_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        return

    if text == "⚙️ Filtri":
        settings = get_user_settings(user.id)
        min_rating = settings.get("min_rating")
        current = f"{min_rating:.1f}⭐" if min_rating is not None else "nessuno"
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("≥ 4.0⭐", callback_data="filt:4.0"),
                 InlineKeyboardButton("≥ 4.5⭐", callback_data="filt:4.5")],
                [InlineKeyboardButton("❌ Nessun filtro", callback_data="filt:none")]
            ]
        )
        await update.message.reply_text(f"Rating minimo attuale: <b>{current}</b>\nScegli:", parse_mode="HTML", reply_markup=kb)
        return

    if text == "🛒 Shop":
        await update.message.reply_text(
            "🛒 <b>Shop Gluten Free</b>\n\n"
            "Al momento non ci sono prodotti gluten free segnalati.\n\n"
            "👉 Entra nel gruppo: @GlutenfreeItalia_bot",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
            disable_web_page_preview=True
        )
        return

    await update.message.reply_text("Usa i pulsanti qui sotto 👇", reply_markup=main_keyboard())


# ==========================
# LOCATION HANDLER
# ==========================

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude
    radius = float(context.user_data.get("nearby_radius_km", 5.0))

    # ricavo città per stats (best effort)
    city = await reverse_geocode_city(lat, lon)
    log_usage(user.id, "search_nearby", city=city)

    page_text, kb = build_nearby_page(user.id, lat, lon, radius_km=radius, page=0)
    if page_text is None:
        await update.message.reply_text(
            f"😔 Nessun locale trovato entro {radius:g} km.",
            reply_markup=main_keyboard()
        )
        return

    await update.message.reply_text(page_text, parse_mode="HTML", reply_markup=kb)
    await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())


# ==========================
# PHOTO HANDLER
# ==========================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_photo_for_user:
        await update.message.reply_text(
            "Per collegare una foto ad un locale, prima apri i dettagli e premi '📷 Aggiungi foto'.",
            reply_markup=main_keyboard(),
        )
        return

    rid = pending_photo_for_user.pop(user.id)
    photo = update.message.photo[-1]
    add_photo_record(user.id, rid, photo.file_id)
    await update.message.reply_text("📷 Foto salvata, grazie!", reply_markup=main_keyboard())


# ==========================
# CALLBACK HANDLER
# ==========================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = (query.data or "").strip()
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

    try:
        if data.startswith("page:"):
            _, city, page_s = data.split(":", 2)
            page = int(page_s)
            txt, kb = build_city_page(user.id, city, page)
            if txt:
                await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
            return

        if data.startswith("nearpage:"):
            _, lat_s, lon_s, rad_s, page_s = data.split(":", 4)
            lat = float(lat_s)
            lon = float(lon_s)
            rad = float(rad_s)
            page = int(page_s)
            txt, kb = build_nearby_page(user.id, lat, lon, rad, page)
            if txt:
                await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
            return

        if data.startswith("details:"):
            rid = int(data.split(":", 1)[1])

            # città dell'ultima ricerca testo (se disponibile)
            last_city = update.effective_chat and context.user_data.get("last_city_search")
            log_usage(user.id, "details_click", city=last_city, restaurant_id=rid)

            with closing(get_conn()) as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM restaurants WHERE id = ?", (rid,))
                r = cur.fetchone()

            if not r:
                await query.message.reply_text("⚠️ Locale non trovato.")
                return

            detail_text, phone = format_restaurant_detail(r)
            tel = normalize_phone_for_tel(phone)

            kb_rows = []
            if tel:
                kb_rows.append([InlineKeyboardButton("📞 Chiama il ristorante", url=f"tel:{tel}")])

            kb_rows.append(
                [
                    InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}"),
                    InlineKeyboardButton("⚠️ Segnala", callback_data=f"rep:{rid}"),
                ]
            )
            kb_rows.append([InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")])

            kb = InlineKeyboardMarkup(kb_rows)

            await query.message.reply_text(detail_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

            photos = get_photos_for_restaurant(rid)
            if photos:
                await query.message.reply_photo(photos[0], caption="📷 Foto dalla community")
            return

        if data.startswith("fav:"):
            rid = int(data.split(":", 1)[1])
            add_favorite(user.id, rid)
            await query.message.reply_text("⭐ Aggiunto ai preferiti.", reply_markup=main_keyboard())
            return

        if data.startswith("rep:"):
            rid = int(data.split(":", 1)[1])
            add_report(user.id, rid, "Segnalazione generica dal bot")
            await query.message.reply_text("⚠️ Segnalazione registrata. Grazie!", reply_markup=main_keyboard())
            return

        if data.startswith("photo:"):
            rid = int(data.split(":", 1)[1])
            pending_photo_for_user[user.id] = rid
            await query.message.reply_text("📷 Inviami una foto del piatto/menù per questo locale.")
            return

        if data.startswith("filt:"):
            val = data.split(":", 1)[1]
            if val == "none":
                set_user_min_rating(user.id, None)
                await query.message.reply_text("Filtro rating disattivato.", reply_markup=main_keyboard())
            else:
                set_user_min_rating(user.id, float(val))
                await query.message.reply_text(f"Rating minimo impostato a {val}⭐.", reply_markup=main_keyboard())
            return

        if data.startswith("suggestcity:"):
            city = data.split(":", 1)[1].strip()
            log_usage(user.id, "suggest_city", city=city)

            await query.message.reply_text("✅ Segnalazione inviata all’admin.", reply_markup=main_keyboard())

            # SOLO NOTIFICA ADMIN (niente DB)
            if ADMIN_CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=int(ADMIN_CHAT_ID),
                        text=f"📩 Città suggerita: <b>{city}</b>\nDa utente: {user.id} (@{user.username or '-'})",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

    except Exception as e:
        # messaggio rapido e notifica admin
        try:
            await query.message.reply_text(f"💥 Errore: {e}", reply_markup=main_keyboard())
        except Exception:
            pass

        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(ADMIN_CHAT_ID),
                    text=f"💥 Errore callback: <b>{e}</b>\nData: <code>{data}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass


# ==========================
# MAIN
# ==========================

def build_application():
    ensure_schema()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))

    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


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
