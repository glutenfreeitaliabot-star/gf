import math
import sqlite3
import os
from contextlib import closing
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any

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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # mettilo su Railway (variabile d'ambiente)
DB_PATH = "restaurants.db"

PAGE_SIZE = 5

# Stati per ConversationHandler "aggiungi ristorante"
ADD_NAME, ADD_CITY, ADD_ADDRESS, ADD_NOTES = range(4)

# Memoria in RAM per gestire "aggiungi foto dopo"
pending_photo_for_user: Dict[int, int] = {}  # {user_id: restaurant_id}

# ==========================
# DB UTILS
# ==========================

def get_conn():
    return sqlite3.connect(DB_PATH)


def _table_cols(cur, table: str) -> set:
    try:
        cur.execute(f"PRAGMA table_info({table})")
        return {r[1].lower() for r in cur.fetchall()}
    except Exception:
        return set()


def ensure_schema():
    """
    Schema robusto:
    - crea restaurants se non esiste
    - aggiunge colonne mancanti (phone/types ecc.) se esiste già
    """
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # restaurants base (se non esiste)
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

        # tabelle di supporto
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
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER NOT NULL,
                city TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, city)
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
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY,
                points INTEGER NOT NULL DEFAULT 0,
                title TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event TEXT,
                city TEXT,
                created_at TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS suggested_cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                city TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # best-effort: aggiungi colonne mancanti a restaurants
        cols = _table_cols(cur, "restaurants")

        def add_col_if_missing(col_name: str, col_def: str):
            nonlocal cols
            if col_name.lower() not in cols:
                try:
                    cur.execute(f"ALTER TABLE restaurants ADD COLUMN {col_def}")
                    cols = _table_cols(cur, "restaurants")
                except Exception:
                    pass

        add_col_if_missing("phone", "phone TEXT")
        add_col_if_missing("types", "types TEXT")

        conn.commit()


def log_usage(user_id: int, event: str, city: Optional[str] = None):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usage_events (user_id, event, city, created_at) VALUES (?, ?, ?, ?)",
            (user_id, event, city, datetime.utcnow().isoformat()),
        )
        conn.commit()


def add_points(user_id: int, points: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_stats (user_id, points) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET points = points + ?",
            (user_id, points, points),
        )
        cur.execute("SELECT points FROM user_stats WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        total = row[0] if row else 0

        if total >= 50:
            title = "🦄 Gluten Master"
        elif total >= 30:
            title = "🏆 Top Contributor"
        elif total >= 15:
            title = "🎖️ Scout del Glutine"
        elif total >= 5:
            title = "🔍 Esploratore Gluten Free"
        else:
            title = "👤 Utente"

        cur.execute("UPDATE user_stats SET title = ? WHERE user_id = ?", (title, user_id))
        conn.commit()


def get_user_stats(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT points, title FROM user_stats WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0, "👤 Utente"
        return row[0], row[1]


def get_user_settings(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT min_rating FROM user_settings WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return {"min_rating": None}
        return {"min_rating": row[0]}


def set_user_min_rating(user_id: int, value: Optional[float]):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        if value is None:
            cur.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        else:
            cur.execute(
                "INSERT INTO user_settings (user_id, min_rating) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET min_rating = ?",
                (user_id, value, value),
            )
        conn.commit()


def add_favorite(user_id: int, restaurant_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO favorites (user_id, restaurant_id, created_at) VALUES (?, ?, ?)",
            (user_id, restaurant_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_favorites(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cols = _table_cols(cur, "restaurants")
        select_cols = ["r.id", "r.name", "r.city", "r.address", "r.notes", "r.rating", "r.lat", "r.lon", "r.last_update"]
        if "types" in cols:
            select_cols.append("r.types")
        if "phone" in cols:
            select_cols.append("r.phone")

        cur.execute(
            f"""
            SELECT {",".join(select_cols)}
            FROM favorites f
            JOIN restaurants r ON r.id = f.restaurant_id
            WHERE f.user_id = ?
            ORDER BY f.created_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


def subscribe_city(user_id: int, city: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, city, created_at) VALUES (?, ?, ?)",
            (user_id, city, datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_subscriptions(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT city FROM subscriptions WHERE user_id = ? ORDER BY city", (user_id,))
        return [r[0] for r in cur.fetchall()]


def add_report(user_id: int, restaurant_id: int, reason: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO reports (user_id, restaurant_id, reason, created_at, status) VALUES (?, ?, ?, ?, 'new')",
            (user_id, restaurant_id, reason, datetime.utcnow().isoformat()),
        )
        conn.commit()


def add_photo_record(user_id: int, restaurant_id: int, file_id: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO photos (restaurant_id, file_id, user_id, created_at) VALUES (?, ?, ?, ?)",
            (restaurant_id, file_id, user_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_photos_for_restaurant(restaurant_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT file_id FROM photos WHERE restaurant_id = ? ORDER BY created_at DESC LIMIT 3",
            (restaurant_id,),
        )
        return [r[0] for r in cur.fetchall()]


def save_suggested_city(user_id: int, city: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO suggested_cities (user_id, city, created_at) VALUES (?, ?, ?)",
            (user_id, city, datetime.utcnow().isoformat()),
        )
        conn.commit()


# ==========================
# GEO / FORMAT
# ==========================

def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def normalize_phone_for_tel(phone: Optional[str]) -> Optional[str]:
    """
    Converte numeri tipo '051 991 3677' -> '+390519913677'
    Lascia + già presente.
    """
    if not phone:
        return None
    p = str(phone).strip()
    if not p:
        return None

    # tieni solo cifre e +
    cleaned = []
    for ch in p:
        if ch.isdigit() or ch == "+":
            cleaned.append(ch)
    p2 = "".join(cleaned)

    if p2 in ("", "+"):
        return None

    # 00xx -> +xx
    if p2.startswith("00"):
        p2 = "+" + p2[2:]

    # se manca prefisso internazionale, assumiamo Italia
    if not p2.startswith("+"):
        p2 = "+39" + p2

    return p2


def restaurant_disclaimer_text() -> str:
    return (
        "\n\nℹ️ <b>Nota importante</b>\n"
        "Questo ristorante è mostrato in base a recensioni e informazioni pubbliche disponibili online.\n"
        "Le condizioni per il senza glutine possono variare nel tempo (cambi di gestione, menu o procedure).\n\n"
        "👉 Ti consigliamo sempre di contattare direttamente il ristorante prima di andare."
    )


def format_restaurant_detail(row: tuple, user_location=None) -> Tuple[str, int, Optional[str]]:
    """
    Supporta SELECT variabile. Interpreta row in base ai campi standard:
    id, name, city, address, notes, rating, lat, lon, last_update, [types?], [phone?]
    Ritorna: (text, rid, phone)
    """
    rid = row[0]
    name = row[1]
    city = row[2]
    address = row[3]
    notes = row[4]
    rating = row[5]
    lat = row[6]
    lon = row[7]
    last_update = row[8]

    # prova a prendere phone se presente negli ultimi campi
    phone = None
    if len(row) >= 10:
        # negli ultimi 2 può esserci phone (stringa con cifre)
        for v in row[9:]:
            if v is None:
                continue
            s = str(v).strip()
            if any(ch.isdigit() for ch in s):
                # potrebbe essere types, ma types spesso contiene '|'
                if "|" not in s and len(s) <= 30:
                    phone = s

    rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."
    update_str = f" (aggiornato: {last_update})" if last_update else ""

    distance_str = ""
    if user_location and lat is not None and lon is not None:
        dist = haversine_km(user_location[0], user_location[1], lat, lon)
        if dist is not None:
            distance_str = f"\n📏 Distanza: {dist*1000:.0f} m" if dist < 1 else f"\n📏 Distanza: {dist:.1f} km"

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"

    phone_line = ""
    if phone and str(phone).strip():
        phone_line = f"\n📞 Telefono: <b>{phone}</b>"
    else:
        phone_line = "\n📞 Telefono: <b>non disponibile</b>"

    text = (
        f"🍽 <b>{name}</b>\n"
        f"📍 <b>{city}</b> – {address or 'Indirizzo non disponibile'}\n"
        f"⭐ Rating medio Google: {rating_str}{update_str}"
        f"{distance_str}"
        f"{phone_line}\n\n"
        f"<b>Note:</b> {notes or '—'}"
        f"{restaurant_disclaimer_text()}\n"
        f"\n🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )

    return text, rid, phone


# ==========================
# QUERY
# ==========================

def _select_restaurant_columns(cur) -> List[str]:
    cols = _table_cols(cur, "restaurants")
    base = ["id", "name", "city", "address", "notes", "rating", "lat", "lon", "last_update"]
    if "types" in cols:
        base.append("types")
    if "phone" in cols:
        base.append("phone")
    return base


def query_by_city(city: str, user_id: int):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        sel = _select_restaurant_columns(cur)
        cur.execute(
            f"""
            SELECT {",".join(sel)}
            FROM restaurants
            WHERE LOWER(city) = LOWER(?)
            ORDER BY (rating IS NULL) ASC, rating DESC, name ASC
            """,
            (city,),
        )
        rows = cur.fetchall()

    if min_rating is not None:
        rows = [r for r in rows if (r[5] is None or float(r[5]) >= float(min_rating))]
    return rows


def query_nearby(lat: float, lon: float, user_id: int, max_distance_km: float, max_results: int = 200):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        sel = _select_restaurant_columns(cur)
        cur.execute(
            f"""
            SELECT {",".join(sel)}
            FROM restaurants
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            """
        )
        rows = cur.fetchall()

    enriched = []
    for r in rows:
        d = haversine_km(lat, lon, r[6], r[7])
        if d is None or d > max_distance_km:
            continue
        if min_rating is not None and r[5] is not None and float(r[5]) < float(min_rating):
            continue
        enriched.append((d, r))

    enriched.sort(key=lambda x: x[0])
    return [e[1] for e in enriched[:max_results]]


# ==========================
# PAGED LISTS
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

    for i, r in enumerate(subset, start=start + 1):
        rid = r[0]
        name = r[1]
        rating = r[5]
        rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."
        lines.append(f"{i}. {name} – {rating_str}")
        kb_rows.append([InlineKeyboardButton(f"Dettagli {i}", callback_data=f"details:{rid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"page:{city}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"page:{city}:{page+1}"))
    if nav:
        kb_rows.append(nav)

    kb = InlineKeyboardMarkup(kb_rows)
    return "\n".join(lines), kb


def build_nearby_page(user_id: int, lat: float, lon: float, radius_km: float, page: int):
    rows = query_nearby(lat, lon, user_id, max_distance_km=radius_km)
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

    for i, r in enumerate(subset, start=start + 1):
        rid, name, city = r[0], r[1], r[2]
        rating = r[5]
        rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."
        d = haversine_km(lat, lon, r[6], r[7])
        dist_str = "n.d." if d is None else (f"{d*1000:.0f} m" if d < 1 else f"{d:.1f} km")
        lines.append(f"{i}. {name} – {city} – {rating_str} – {dist_str}")
        kb_rows.append([InlineKeyboardButton(f"Dettagli {i}", callback_data=f"details:{rid}")])

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

    kb = InlineKeyboardMarkup(kb_rows)
    return "\n".join(lines), kb


# ==========================
# UI
# ==========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["➕ Aggiungi ristorante", "⭐ I miei preferiti"],
            ["⚙️ Filtri", "🛒 Shop"],
        ],
        resize_keyboard=True,
    )


# ==========================
# HANDLERS
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "start")

    points, title = get_user_stats(user.id)

    msg = (
        f"Ciao 👋 benvenuto in <b>GlutenFreeBot</b> 🧡\n\n"
        f"Qui trovi ristoranti e locali pensati per chi vive davvero <b>senza glutine</b>.\n\n"
        f"🍽 Cerca per città\n"
        f"📍 Trova locali vicino a te\n"
        f"⭐ Salva i preferiti\n"
        f"🛒 Shop (in arrivo)\n\n"
        f"✨ Ma non finisce qui…\n\n"
        f"Su Instagram raccontiamo il lato umano del gluten free:\n"
        f"consigli veri, esperienze reali, nuove scoperte.\n\n"
        f"📸 <a href=\"https://www.instagram.com/glutenfreebot?igsh=bzYxdXd3cDF0MTly&utm_source=qr\">@glutenfreebot</a>\n\n"
        f"Il tuo profilo:\n"
        f"• Punti: <b>{points}</b>\n"
        f"• Titolo: <b>{title}</b>\n\n"
        f"Usa i pulsanti qui sotto per iniziare."
    )

    await update.message.reply_text(msg, reply_markup=main_keyboard(), parse_mode="HTML", disable_web_page_preview=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Usa /start e i pulsanti.", reply_markup=main_keyboard())


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

        page_text, kb = build_city_page(user.id, city, page=0)
        if page_text is None:
            suggest_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📩 Suggerisci questa città", callback_data=f"suggestcity:{city}")]]
            )
            await update.message.reply_text(
                f"😔 Al momento non ho locali per <b>{city}</b>.\n\nVuoi segnalarla? La mettiamo in coda per aggiornare il database.",
                parse_mode="HTML",
                reply_markup=suggest_kb,
            )
            await update.message.reply_text("Puoi continuare dal menu 👇", reply_markup=main_keyboard())
            return

        await update.message.reply_text(page_text, parse_mode="HTML", reply_markup=kb)
        await update.message.reply_text("Puoi continuare dal menu 👇", reply_markup=main_keyboard())
        return

    if text == "📍 Vicino a me":
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

    if text == "➕ Aggiungi ristorante":
        await update.message.reply_text("Ok! Come si chiama il locale?", reply_markup=main_keyboard())
        context.user_data["add_step"] = ADD_NAME
        return

    # semplice flow add (testuale)
    if context.user_data.get("add_step") == ADD_NAME:
        context.user_data["new_rest_name"] = text
        context.user_data["add_step"] = ADD_CITY
        await update.message.reply_text("In che città si trova?")
        return
    if context.user_data.get("add_step") == ADD_CITY:
        context.user_data["new_rest_city"] = text
        context.user_data["add_step"] = ADD_ADDRESS
        await update.message.reply_text("Qual è l'indirizzo?")
        return
    if context.user_data.get("add_step") == ADD_ADDRESS:
        context.user_data["new_rest_address"] = text
        context.user_data["add_step"] = ADD_NOTES
        await update.message.reply_text("Vuoi aggiungere una nota? Se no, scrivi '-'")
        return
    if context.user_data.get("add_step") == ADD_NOTES:
        notes = text if text != "-" else ""
        name = context.user_data.get("new_rest_name", "").strip()
        city = context.user_data.get("new_rest_city", "").strip()
        address = context.user_data.get("new_rest_address", "").strip()

        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO restaurants (name, city, address, notes, source, lat, lon, rating, last_update)
                VALUES (?, ?, ?, ?, 'user', NULL, NULL, NULL, ?)
                """,
                (name, city, address, notes, datetime.utcnow().isoformat()),
            )
            conn.commit()

        add_points(user.id, 2)
        context.user_data["add_step"] = None
        await update.message.reply_text("Grazie! Locale aggiunto 🙌", reply_markup=main_keyboard())
        return

    if text == "⭐ I miei preferiti":
        favs = get_favorites(user.id)
        if not favs:
            await update.message.reply_text("Non hai ancora preferiti ⭐", reply_markup=main_keyboard())
            return
        await update.message.reply_text(f"Hai <b>{len(favs)}</b> preferiti:", parse_mode="HTML", reply_markup=main_keyboard())
        # mostro i primi 10 come dettaglio (puoi paginarli dopo)
        for r in favs[:10]:
            detail_text, rid, phone = format_restaurant_detail(r)
            tel = normalize_phone_for_tel(phone)
            kb_rows = []
            if tel:
                kb_rows.append([InlineKeyboardButton("📞 Chiama il ristorante", url=f"tel:{tel}")])
            kb_rows.append([InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")])
            kb = InlineKeyboardMarkup(kb_rows) if kb_rows else None
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

    await update.message.reply_text("Non ho capito. Usa /start o i pulsanti.", reply_markup=main_keyboard())


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude

    radius = float(context.user_data.get("nearby_radius_km", 5.0))
    log_usage(user.id, "search_nearby", city=None)

    page_text, kb = build_nearby_page(user.id, lat, lon, radius_km=radius, page=0)
    if page_text is None:
        await update.message.reply_text(
            f"😔 Nessun locale trovato entro {radius:g} km.\n\n"
            "Vuoi segnalarci la città? Scrivi il nome città e la mettiamo in coda.",
            reply_markup=main_keyboard()
        )
        await update.message.reply_text("Puoi continuare dal menu 👇", reply_markup=main_keyboard())
        return

    await update.message.reply_text(page_text, parse_mode="HTML", reply_markup=kb)
    await update.message.reply_text("Puoi continuare dal menu 👇", reply_markup=main_keyboard())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    await query.answer()

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
        log_usage(user.id, "details_click")

        with closing(get_conn()) as conn:
            cur = conn.cursor()
            sel = _select_restaurant_columns(cur)
            cur.execute(f"SELECT {','.join(sel)} FROM restaurants WHERE id = ?", (rid,))
            row = cur.fetchone()

        if not row:
            await query.message.reply_text("Locale non trovato.")
            return

        detail_text, rid, phone = format_restaurant_detail(row)
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
        add_points(user.id, 1)
        await query.message.reply_text("⭐ Aggiunto ai preferiti.", reply_markup=main_keyboard())
        return

    if data.startswith("rep:"):
        rid = int(data.split(":", 1)[1])
        add_report(user.id, rid, "Segnalazione generica dal bot")
        add_points(user.id, 1)
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
        if not city:
            await query.message.reply_text("Città non valida.")
            return

        save_suggested_city(user.id, city)
        await query.message.reply_text("✅ Segnalazione inviata! La mettiamo in coda.", reply_markup=main_keyboard())

        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(ADMIN_CHAT_ID),
                    text=f"📩 Nuova città suggerita: <b>{city}</b>\nDa utente: {user.id} (@{user.username or '-'})",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_photo_for_user:
        await update.message.reply_text(
            "Per collegare una foto, apri un locale e premi '📷 Aggiungi foto'.",
            reply_markup=main_keyboard()
        )
        return

    rid = pending_photo_for_user.pop(user.id)
    photo = update.message.photo[-1]
    add_photo_record(user.id, rid, photo.file_id)
    add_points(user.id, 2)

    await update.message.reply_text("📷 Foto salvata, grazie!", reply_markup=main_keyboard())


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
