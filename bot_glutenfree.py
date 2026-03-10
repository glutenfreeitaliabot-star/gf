import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict

import httpx
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
    ConversationHandler,
    filters,
)

from import_app_restaurants import import_app_restaurants
from maps_utils import build_google_maps_multi_url


# ==========================
# CONFIG
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
DB_PATH = "restaurants.db"

PAGE_SIZE = 5
pending_photo_for_user: Dict[int, int] = {}

RADIUS_OPTIONS = [1, 3, 5, 10, 15, 20]
TRIP_SEARCH_RADIUS_KM = 8
TRIP_MAX_CITIES = 5

SEGNALA_NOME, SEGNALA_CITTA, SEGNALA_TIPO, SEGNALA_NOTE = range(4)
TRAVEL_CITIES, TRAVEL_ADDRESS = range(100, 102)

TYPE_LABELS = {
    "restaurant": "Ristorante",
    "cafe": "Caffè",
    "bakery": "Bakery",
    "bar": "Bar",
    "grocery_or_supermarket": "Supermercato",
}


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
                lat TEXT,
                lon TEXT,
                rating REAL,
                rating_online_gf REAL,
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
                min_rating REAL,
                type_filter TEXT
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

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS restaurant_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT NOT NULL,
                city TEXT NOT NULL,
                suggestion_type TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        cur.execute("PRAGMA table_info(restaurants)")
        cols = {row[1] for row in cur.fetchall()}
        if "types" not in cols:
            cur.execute("ALTER TABLE restaurants ADD COLUMN types TEXT")
        if "phone" not in cols:
            cur.execute("ALTER TABLE restaurants ADD COLUMN phone TEXT")
        if "rating_online_gf" not in cols:
            cur.execute("ALTER TABLE restaurants ADD COLUMN rating_online_gf REAL")

        cur.execute("PRAGMA table_info(user_settings)")
        us_cols = {row[1] for row in cur.fetchall()}
        if "min_rating" not in us_cols:
            cur.execute("ALTER TABLE user_settings ADD COLUMN min_rating REAL")
        if "type_filter" not in us_cols:
            cur.execute("ALTER TABLE user_settings ADD COLUMN type_filter TEXT")

        conn.commit()



def log_usage(user_id: int, event: str, city: Optional[str] = None, restaurant_id: Optional[int] = None):
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



def get_user_settings(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT min_rating, type_filter FROM user_settings WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return {
            "min_rating": row["min_rating"] if row else None,
            "type_filter": row["type_filter"] if row else None,
        }



def set_user_min_rating(user_id: int, value: Optional[float]):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, min_rating)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET min_rating = excluded.min_rating
            """,
            (user_id, value),
        )
        if value is None:
            cur.execute("UPDATE user_settings SET min_rating = NULL WHERE user_id = ?", (user_id,))
        conn.commit()



def set_user_type_filter(user_id: int, value: Optional[str]):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, type_filter)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET type_filter = excluded.type_filter
            """,
            (user_id, value),
        )
        if value is None:
            cur.execute("UPDATE user_settings SET type_filter = NULL WHERE user_id = ?", (user_id,))
        conn.commit()



def clear_user_filters(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, min_rating, type_filter)
            VALUES (?, NULL, NULL)
            ON CONFLICT(user_id) DO UPDATE SET min_rating = NULL, type_filter = NULL
            """,
            (user_id,),
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
        return dedupe_restaurants(cur.fetchall())



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
            SELECT file_id
            FROM photos
            WHERE restaurant_id = ?
            ORDER BY created_at DESC
            LIMIT 3
            """,
            (restaurant_id,),
        )
        return [r["file_id"] for r in cur.fetchall()]



def save_suggestion(user_id: int, name: str, city: str, suggestion_type: str, notes: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO restaurant_suggestions (user_id, name, city, suggestion_type, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, name, city, suggestion_type, notes, datetime.utcnow().isoformat()),
        )
        conn.commit()


# ==========================
# UTILS
# ==========================

def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.strip()
            if v == "":
                return None
            v = v.replace(",", ".")
        return float(v)
    except Exception:
        return None



def _normalize_coords(lat_raw, lon_raw) -> Tuple[Optional[float], Optional[float]]:
    lat = _to_float(lat_raw)
    lon = _to_float(lon_raw)
    if lat is None or lon is None:
        return None, None

    if abs(lat) > 90 or abs(lon) > 180:
        lat, lon = lon, lat

    if abs(lat) > 90 or abs(lon) > 180:
        return None, None

    if (6 <= lat <= 19) and (36 <= lon <= 47):
        lat, lon = lon, lat

    return lat, lon



def haversine_km(lat1, lon1, lat2, lon2) -> Optional[float]:
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))



def _clean_text(value: Optional[str]) -> str:
    return " ".join((value or "").strip().lower().split())



def restaurant_dedupe_key(r: sqlite3.Row) -> Tuple[str, str, str]:
    return (
        _clean_text(r["name"]),
        _clean_text(r["city"]),
        _clean_text(r["address"]),
    )



def restaurant_sort_score(r: sqlite3.Row) -> Tuple[float, float, int, int]:
    rating = float(r["rating"]) if r["rating"] is not None else -1.0
    rating_gf = float(r["rating_online_gf"]) if r["rating_online_gf"] is not None else -1.0
    has_coords = 1 if _normalize_coords(r["lat"], r["lon"])[0] is not None else 0
    return (rating, rating_gf, has_coords, -int(r["id"]))



def dedupe_restaurants(rows: List[sqlite3.Row]) -> List[sqlite3.Row]:
    best_by_key: Dict[Tuple[str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = restaurant_dedupe_key(row)
        current = best_by_key.get(key)
        if current is None or restaurant_sort_score(row) > restaurant_sort_score(current):
            best_by_key[key] = row
    deduped = list(best_by_key.values())
    deduped.sort(
        key=lambda r: (
            r["rating"] is None,
            -(float(r["rating"]) if r["rating"] is not None else -1.0),
            _clean_text(r["name"]),
        )
    )
    return deduped



def format_type_label(type_value: Optional[str]) -> str:
    if not type_value:
        return "tutte"
    return TYPE_LABELS.get(type_value, type_value)



def format_active_filters(settings: dict) -> str:
    parts = []
    mr = settings.get("min_rating")
    tf = settings.get("type_filter")
    if mr is not None:
        parts.append(f"rating ≥ {float(mr):.1f}⭐")
    if tf:
        parts.append(f"tipo: {format_type_label(tf)}")
    return ", ".join(parts) if parts else "nessuno"



def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["🧳 Viaggio", "⭐ I miei preferiti"],
            ["⚙️ Filtri", "➕ Segnala ristorante"],
            ["🛒 Shop"],
        ],
        resize_keyboard=True,
    )



def radius_keyboard():
    rows = [
        [f"{RADIUS_OPTIONS[0]} km", f"{RADIUS_OPTIONS[1]} km", f"{RADIUS_OPTIONS[2]} km"],
        [f"{RADIUS_OPTIONS[3]} km", f"{RADIUS_OPTIONS[4]} km", f"{RADIUS_OPTIONS[5]} km"],
        ["✍️ Inserisci km manualmente"],
        ["❌ Annulla"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)



def location_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Invia posizione 📍", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )



def filter_keyboard(settings: dict) -> InlineKeyboardMarkup:
    min_rating = settings.get("min_rating")
    type_filter = settings.get("type_filter")

    def rating_btn(label: str, value: str, active: bool) -> InlineKeyboardButton:
        prefix = "✅ " if active else ""
        return InlineKeyboardButton(f"{prefix}{label}", callback_data=f"filt:{value}")

    def type_btn(value: str) -> InlineKeyboardButton:
        label = TYPE_LABELS[value]
        prefix = "✅ " if type_filter == value else ""
        return InlineKeyboardButton(f"{prefix}{label}", callback_data=f"type:{value}")

    return InlineKeyboardMarkup(
        [
            [
                rating_btn("Nessun rating", "none", min_rating is None),
                rating_btn("≥ 4.0⭐", "4.0", min_rating == 4.0),
                rating_btn("≥ 4.5⭐", "4.5", min_rating == 4.5),
            ],
            [type_btn("restaurant"), type_btn("cafe")],
            [type_btn("bakery"), type_btn("bar")],
            [type_btn("grocery_or_supermarket")],
            [InlineKeyboardButton("✅ Tutte le tipologie" if type_filter is None else "Tutte le tipologie", callback_data="type:none")],
            [InlineKeyboardButton("🧹 Reset completo", callback_data="reset:filters")],
        ]
    )



def build_trip_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📣 Segnala al founder di migliorare questo viaggio", callback_data="trip_notify")]]
    )



def format_restaurant_detail(r: sqlite3.Row, user_location: Optional[Tuple[float, float]] = None) -> str:
    name = r["name"]
    city = r["city"]
    address = r["address"] or "Indirizzo non disponibile"
    notes = r["notes"] or "—"
    rating_val = r["rating"]
    rating_gf_val = r["rating_online_gf"] if "rating_online_gf" in r.keys() else None
    last_update = r["last_update"]
    phone = (r["phone"] or "").strip() if "phone" in r.keys() and r["phone"] else ""

    rating = f"{float(rating_val):.1f}⭐" if rating_val is not None else "n.d."
    rating_gf = f"{float(rating_gf_val):.1f}🌾" if rating_gf_val is not None else "n.d."
    update_str = f" (aggiornato: {last_update})" if last_update else ""

    lat, lon = _normalize_coords(r["lat"], r["lon"])
    distance_str = ""
    if user_location and lat is not None and lon is not None:
        d = haversine_km(user_location[0], user_location[1], lat, lon)
        if d is not None:
            distance_str = f"\n📏 Distanza: {d*1000:.0f} m" if d < 1 else f"\n📏 Distanza: {d:.1f} km"

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"

    if phone:
        phone_line = f"\n📞 Telefono: <b>{phone}</b>"
    else:
        phone_line = "\n📞 Telefono: <b>non disponibile</b>"

    return (
        f"🍽 <b>{name}</b>\n"
        f"📍 <b>{city}</b> – {address}\n"
        f"⭐ Rating Google: <b>{rating}</b>{update_str}\n"
        f"🌾 Rating dove citano Gluten Free: <b>{rating_gf}</b>"
        f"{distance_str}"
        f"{phone_line}\n\n"
        f"<b>Note:</b> {notes}\n\n"
        "ℹ️ <b>Nota importante</b>\n"
        "Mostriamo questo locale in base a informazioni e recensioni pubbliche online.\n"
        "Le condizioni per il senza glutine possono cambiare.\n"
        "👉 Contatta sempre il ristorante prima di andare.\n\n"
        f"🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )


async def geocode_address(address: str) -> Tuple[Optional[float], Optional[float]]:
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "jsonv2", "limit": 1}
    headers = {"User-Agent": "GlutenFreeBot/1.0"}
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        print(f"[geocode] errore su '{address}': {exc}")
        return None, None

    if not data:
        return None, None

    try:
        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        return None, None


# ==========================
# QUERY
# ==========================

def query_by_city(city: str, user_id: int) -> List[sqlite3.Row]:
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")
    type_filter = settings.get("type_filter")

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

    if type_filter:
        tf = str(type_filter).strip().lower()
        rows = [
            r for r in rows
            if (r["types"] and tf in {t.strip().lower() for t in str(r["types"]).split("|")})
        ]

    return dedupe_restaurants(rows)



def query_nearby(user_id: int, lat_user: float, lon_user: float, radius_km: float, max_results: int = 200) -> List[sqlite3.Row]:
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")
    type_filter = settings.get("type_filter")

    lat_user = _to_float(lat_user)
    lon_user = _to_float(lon_user)
    if lat_user is None or lon_user is None:
        return []

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM restaurants")
        rows = cur.fetchall()

    results: List[Tuple[float, sqlite3.Row]] = []
    seen_keys = set()

    for r in rows:
        if min_rating is not None and r["rating"] is not None and float(r["rating"]) < float(min_rating):
            continue

        if type_filter:
            tf = str(type_filter).strip().lower()
            if not (r["types"] and tf in {t.strip().lower() for t in str(r["types"]).split("|")}):
                continue

        lat, lon = _normalize_coords(r["lat"], r["lon"])
        if lat is None or lon is None:
            continue

        d = haversine_km(lat_user, lon_user, lat, lon)
        if d is None or d > radius_km:
            continue

        key = restaurant_dedupe_key(r)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        results.append((d, r))

    print(f"[nearby] radius={radius_km}km | rows={len(rows)} | matches={len(results)}")
    results.sort(key=lambda x: (x[0], -(float(x[1]["rating"]) if x[1]["rating"] is not None else -1.0), _clean_text(x[1]["name"])))
    return [x[1] for x in results[:max_results]]



def build_list_message(
    rows: List[sqlite3.Row],
    title: str,
    page: int,
    user_location: Optional[Tuple[float, float]] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    total = len(rows)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    subset = rows[start:end]

    lines = [f"{title} (pagina {page + 1}/{total_pages}):", ""]
    kb_rows = []

    for idx, r in enumerate(subset, start=1):
        rid = int(r["id"])
        display_n = start + idx
        rating_val = r["rating"]
        rating_gf_val = r["rating_online_gf"] if "rating_online_gf" in r.keys() else None
        rating = f"{float(rating_val):.1f}⭐" if rating_val is not None else "n.d."
        rating_gf = f"{float(rating_gf_val):.1f}🌾" if rating_gf_val is not None else "n.d."
        lines.append(f"{display_n}. {r['name']} – {rating} | {rating_gf}")
        kb_rows.append([InlineKeyboardButton(f"Dettagli {display_n}", callback_data=f"details:{rid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"listpage:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"listpage:{page + 1}"))
    if nav:
        kb_rows.append(nav)

    maps_url = build_google_maps_multi_url(
        rows,
        normalize_coords_fn=_normalize_coords,
        user_location=user_location,
        limit=10,
        travelmode="driving",
    )
    if maps_url:
        kb_rows.append([InlineKeyboardButton("🗺 Mostra su mappa (Top 10)", url=maps_url)])

    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


async def send_results_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    rows: List[sqlite3.Row],
    title: str,
    list_type: str,
    user_location: Optional[Tuple[float, float]] = None,
):
    context.user_data["last_list_rows_ids"] = [int(r["id"]) for r in rows]
    context.user_data["last_list_title"] = title
    context.user_data["last_list_type"] = list_type
    if user_location:
        context.user_data["last_nearby_coords"] = user_location

    msg, kb = build_list_message(rows, title, page=0, user_location=user_location)
    await update.effective_message.reply_text(msg, parse_mode="HTML", reply_markup=kb)


# ===============================
# COMMUNITY: SEGNALAZIONI + FEEDBACK
# ===============================

def followup_rating_buttons(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⭐ Sicuro e consigliato", callback_data=f"rategood:{rid}")],
            [InlineKeyboardButton("⚠️ Attenzione contaminazione", callback_data=f"ratewarn:{rid}")],
            [InlineKeyboardButton("❌ Esperienza negativa", callback_data=f"ratebad:{rid}")],
        ]
    )


async def segnala_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "suggest_start")
    context.user_data.pop("suggest_data", None)
    await update.message.reply_text("➕ Segnalazione nuovo ristorante\n\nScrivi il *nome* del locale:", parse_mode="Markdown")
    return SEGNALA_NOME


async def segnala_nome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["suggest_data"] = {"name": update.message.text.strip()}
    await update.message.reply_text("Perfetto. Ora scrivi la *città*:", parse_mode="Markdown")
    return SEGNALA_CITTA


async def segnala_citta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["suggest_data"]["city"] = update.message.text.strip()
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("100% GF", callback_data="stype:100")],
            [InlineKeyboardButton("Opzioni GF", callback_data="stype:options")],
            [InlineKeyboardButton("Non sicuro / da verificare", callback_data="stype:unknown")],
        ]
    )
    await update.message.reply_text("Che tipo di locale è?", reply_markup=kb)
    return SEGNALA_TIPO


async def segnala_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    stype = (q.data or "").split(":", 1)[1] if ":" in (q.data or "") else "unknown"
    context.user_data["suggest_data"]["type"] = stype
    await q.edit_message_text("Ultimo step: scrivi una nota (indirizzo, link, cosa hai mangiato, ecc.).\nSe non vuoi, scrivi 'skip'.")
    return SEGNALA_NOTE


async def segnala_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    notes = update.message.text.strip()
    if notes.lower() == "skip":
        notes = ""
    d = context.user_data.get("suggest_data") or {}
    name = d.get("name", "").strip()
    city = d.get("city", "").strip()
    stype = d.get("type", "unknown")
    if not name or not city:
        await update.message.reply_text("⚠️ Segnalazione incompleta. Riparti da capo con '➕ Segnala ristorante'.")
        return ConversationHandler.END

    save_suggestion(user.id, name, city, stype, notes)
    log_usage(user.id, "suggest_submit", city=city)

    await update.message.reply_text("Grazie! ✅ Segnalazione registrata. Dopo verifica potrà finire nella lista.")
    return ConversationHandler.END


async def segnala_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "suggest_cancel")
    await update.message.reply_text("Operazione annullata.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def followup_job_callback(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    rid = data.get("rid")
    if not chat_id or not rid:
        return
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🧡 Com'è andata al ristorante che avevi prenotato? Il tuo feedback aiuta la community.",
            reply_markup=followup_rating_buttons(int(rid)),
        )
        uid = data.get("user_id")
        if uid:
            log_usage(int(uid), "followup_sent", restaurant_id=int(rid))
    except Exception:
        pass


# ===============================
# VIAGGIO
# ===============================

async def viaggio_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "trip_start")
    context.user_data.pop("trip_data", None)
    await update.message.reply_text(
        "🧳 Modalità viaggio\n\nScrivi le città da visitare separate da virgola.\nEsempio: Roma, Firenze, Bologna",
        reply_markup=main_keyboard(),
    )
    return TRAVEL_CITIES


async def viaggio_cities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    cities = [c.strip() for c in raw.split(",") if c.strip()]
    if not cities:
        await update.message.reply_text("Scrivi almeno una città. Esempio: Roma, Napoli")
        return TRAVEL_CITIES

    cities = cities[:TRIP_MAX_CITIES]
    context.user_data["trip_data"] = {
        "cities": cities,
        "stays": [],
        "index": 0,
    }
    current_city = cities[0]
    await update.message.reply_text(f"Perfetto. Indicami l'indirizzo dell'alloggio a <b>{current_city}</b>.", parse_mode="HTML")
    return TRAVEL_ADDRESS


async def viaggio_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trip_data = context.user_data.get("trip_data") or {}
    cities = trip_data.get("cities") or []
    idx = int(trip_data.get("index") or 0)
    if idx >= len(cities):
        return ConversationHandler.END

    address = (update.message.text or "").strip()
    if not address:
        await update.message.reply_text("Scrivi un indirizzo valido.")
        return TRAVEL_ADDRESS

    trip_data.setdefault("stays", []).append({"city": cities[idx], "address": address})
    idx += 1
    trip_data["index"] = idx
    context.user_data["trip_data"] = trip_data

    if idx < len(cities):
        await update.message.reply_text(f"Ora indicami l'indirizzo dell'alloggio a <b>{cities[idx]}</b>.", parse_mode="HTML")
        return TRAVEL_ADDRESS

    await process_trip_results(update, context)
    return ConversationHandler.END


async def viaggio_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "trip_cancel")
    context.user_data.pop("trip_data", None)
    await update.message.reply_text("Viaggio annullato.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def process_trip_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    trip_data = context.user_data.get("trip_data") or {}
    stays = trip_data.get("stays") or []
    summary_lines = []
    found_any = False

    for stay in stays:
        city = stay["city"]
        address = stay["address"]
        log_usage(user.id, "trip_city", city=city)

        full_address = f"{address}, {city}, Italia"
        lat, lon = await geocode_address(full_address)
        if lat is not None and lon is not None:
            rows = query_nearby(user.id, lat, lon, TRIP_SEARCH_RADIUS_KM, max_results=10)
            title = f"🧳 <b>{city}</b> — locali vicino all'alloggio"
            user_location = (lat, lon)
        else:
            rows = query_by_city(city, user.id)
            title = f"🧳 <b>{city}</b> — risultati per città"
            user_location = None

        summary_lines.append(f"{city} | {address}")

        if not rows:
            await update.message.reply_text(
                f"😔 Nessun risultato trovato per <b>{city}</b> partendo da <i>{address}</i>.",
                parse_mode="HTML",
                reply_markup=build_trip_feedback_keyboard(),
            )
            continue

        found_any = True
        await update.message.reply_text(
            f"🏨 Alloggio a <b>{city}</b>: <i>{address}</i>",
            parse_mode="HTML",
        )
        await send_results_list(update, context, rows, title, list_type="trip", user_location=user_location)
        await update.effective_message.reply_text(
            "Se i risultati non ti convincono, usa il pulsante qui sotto e lo giro al founder.",
            reply_markup=build_trip_feedback_keyboard(),
        )

    context.user_data["last_trip_summary"] = "\n".join(summary_lines)
    context.user_data.pop("trip_data", None)

    if not found_any:
        log_usage(user.id, "trip_no_results")

    await update.effective_message.reply_text("Menu 👇", reply_markup=main_keyboard())


# ==========================
# HANDLERS
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "start")

    msg = (
        "Ciao 👋 benvenuto in <b>GlutenFreeBot</b> 🧡\n\n"
        "Trova ristoranti con recensioni che parlano di gluten free.\n\n"
        "Di default non applichiamo filtri: la ricerca parte completa. Niente trucchetti ninja.\n\n"
        "📸 Seguici su Instagram:\n"
        "<a href=\"https://www.instagram.com/glutenfreebot?igsh=bzYxdXd3cDF0MTly&utm_source=qr\">@glutenfreebot</a>\n\n"
        "Usa i pulsanti qui sotto 👇"
    )
    await update.message.reply_text(
        msg, parse_mode="HTML", reply_markup=main_keyboard(), disable_web_page_preview=True
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_CHAT_ID or str(update.effective_user.id) != str(ADMIN_CHAT_ID):
        return

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT user_id) AS n FROM usage_events")
        users = cur.fetchone()["n"] or 0
        cur.execute("SELECT COUNT(*) AS n FROM usage_events")
        events_total = cur.fetchone()["n"] or 0
        cur.execute(
            """
            SELECT event, COUNT(*) AS n
            FROM usage_events
            GROUP BY event
            ORDER BY n DESC
            LIMIT 20
            """
        )
        events = cur.fetchall()

    msg = "<b>📊 STATS (ADMIN)</b>\n\n"
    msg += f"👥 Utenti unici: <b>{users}</b>\n"
    msg += f"🧾 Eventi totali: <b>{events_total}</b>\n\n"
    msg += "<b>Top funzioni</b>\n"
    for e in events:
        msg += f"• {e['event']}: <b>{e['n']}</b>\n"

    await update.message.reply_text(msg, parse_mode="HTML")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    if text == "🔍 Cerca per città":
        context.user_data["awaiting_city"] = True
        context.user_data["awaiting_radius"] = False
        context.user_data["awaiting_manual_radius"] = False
        await update.message.reply_text("Scrivi il nome della città (es: Bari):", reply_markup=main_keyboard())
        return

    if context.user_data.get("awaiting_city"):
        context.user_data["awaiting_city"] = False
        city = text.strip()
        context.user_data["last_city_search"] = city
        log_usage(user.id, "search_city", city=city)

        rows = query_by_city(city, user.id)
        if not rows:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📩 Suggerisci città", callback_data=f"suggest:{city}")]]
            )
            await update.message.reply_text(
                f"😔 Nessun risultato per <b>{city}</b>.\nVuoi segnalarla all'admin?",
                parse_mode="HTML",
                reply_markup=kb,
            )
            await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
            return

        title = (
            f"🔎 Ho trovato <b>{len(rows)}</b> locali a <b>{city}</b>\n"
            f"🔎 Filtri: <b>{format_active_filters(get_user_settings(user.id))}</b>"
        )
        await send_results_list(update, context, rows, title, list_type="city")
        await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
        return

    if text == "📍 Vicino a me":
        context.user_data["awaiting_radius"] = True
        context.user_data["awaiting_manual_radius"] = False
        await update.message.reply_text(
            "Scegli il raggio di ricerca oppure inseriscilo a mano.",
            reply_markup=radius_keyboard(),
        )
        return

    if text == "✍️ Inserisci km manualmente":
        context.user_data["awaiting_radius"] = False
        context.user_data["awaiting_manual_radius"] = True
        await update.message.reply_text(
            "Scrivi il raggio in km. Esempi validi: 2, 7.5, 12",
            reply_markup=ReplyKeyboardMarkup([["❌ Annulla"]], resize_keyboard=True, one_time_keyboard=True),
        )
        return

    if context.user_data.get("awaiting_manual_radius"):
        if text == "❌ Annulla":
            context.user_data["awaiting_manual_radius"] = False
            await update.message.reply_text("Ok, annullato.", reply_markup=main_keyboard())
            return

        radius = _to_float(text)
        if radius is None or radius <= 0 or radius > 100:
            await update.message.reply_text("Inserisci un numero valido tra 0.5 e 100 km.")
            return

        context.user_data["nearby_radius_km"] = radius
        context.user_data["awaiting_manual_radius"] = False
        await update.message.reply_text(
            f"Raggio impostato a <b>{radius:g} km</b>.\nOra inviami la posizione 📍",
            parse_mode="HTML",
            reply_markup=location_keyboard(),
        )
        return

    if context.user_data.get("awaiting_radius"):
        if text == "❌ Annulla":
            context.user_data["awaiting_radius"] = False
            await update.message.reply_text("Ok, annullato.", reply_markup=main_keyboard())
            return

        if "km" in text.lower():
            radius = _to_float(text.lower().replace("km", "").strip())
            if radius is None:
                await update.message.reply_text("Seleziona una delle opzioni oppure inserisci il valore manualmente.", reply_markup=radius_keyboard())
                return

            context.user_data["nearby_radius_km"] = radius
            context.user_data["awaiting_radius"] = False
            await update.message.reply_text(
                f"Raggio impostato a <b>{radius:g} km</b>.\nOra inviami la posizione 📍",
                parse_mode="HTML",
                reply_markup=location_keyboard(),
            )
            return

        await update.message.reply_text("Seleziona una delle opzioni 👇", reply_markup=radius_keyboard())
        return

    if text == "🧳 Viaggio":
        return await viaggio_start(update, context)

    if text == "⭐ I miei preferiti":
        favs = get_favorites(user.id)
        if not favs:
            await update.message.reply_text("Non hai ancora preferiti ⭐", reply_markup=main_keyboard())
            return
        await update.message.reply_text(f"Hai <b>{len(favs)}</b> preferiti:", parse_mode="HTML", reply_markup=main_keyboard())
        for r in favs[:10]:
            detail = format_restaurant_detail(r)
            await update.message.reply_text(detail, parse_mode="HTML", disable_web_page_preview=True)
        return

    if text == "⚙️ Filtri":
        settings = get_user_settings(user.id)
        await update.message.reply_text(
            "⚙️ <b>Filtri ricerca</b>\n\n"
            f"Attivi ora: <b>{format_active_filters(settings)}</b>\n"
            "Di default non c'è nessun filtro attivo, quindi la ricerca è completa.",
            parse_mode="HTML",
            reply_markup=filter_keyboard(settings),
        )
        return

    if text == "🛒 Shop":
        await update.message.reply_text(
            "🛒 <b>Shop Gluten Free</b>\n\n"
            "Al momento non ci sono prodotti gluten free segnalati.\n\n"
            "👉 Entra nel gruppo: @GlutenfreeItalia_bot",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )
        return

    await update.message.reply_text("Usa il menu 👇", reply_markup=main_keyboard())


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    loc = update.message.location
    lat_user, lon_user = loc.latitude, loc.longitude

    radius = float(context.user_data.get("nearby_radius_km") or 5.0)
    log_usage(user.id, "search_nearby")

    rows = query_nearby(user.id, lat_user, lon_user, radius_km=radius)

    if not rows:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📩 Suggerisci città/zona", callback_data="suggest:posizione")]]
        )
        await update.message.reply_text(
            f"😔 Nessun locale trovato entro <b>{radius:g} km</b>.\nVuoi segnalarci la zona all'admin?",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
        return

    title = (
        f"📍 Locali entro <b>{radius:g} km</b> — trovati <b>{len(rows)}</b>\n"
        f"🔎 Filtri: <b>{format_active_filters(get_user_settings(user.id))}</b>"
    )
    await send_results_list(update, context, rows, title, list_type="nearby", user_location=(lat_user, lon_user))
    await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_photo_for_user:
        await update.message.reply_text(
            "Per collegare una foto a un locale, apri i dettagli e premi '📷 Aggiungi foto'.",
            reply_markup=main_keyboard(),
        )
        return

    rid = pending_photo_for_user.pop(user.id)
    photo = update.message.photo[-1]
    add_photo_record(user.id, rid, photo.file_id)
    await update.message.reply_text("📷 Foto salvata, grazie!", reply_markup=main_keyboard())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = (query.data or "").strip()
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

    if data.startswith("details:"):
        rid = int(data.split(":", 1)[1])

        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM restaurants WHERE id = ?", (rid,))
            r = cur.fetchone()

        if not r:
            await query.message.reply_text("⚠️ Locale non trovato.", reply_markup=main_keyboard())
            return

        city_ctx = context.user_data.get("last_city_search")
        log_usage(user.id, "details_click", city=city_ctx, restaurant_id=rid)

        user_loc = None
        if context.user_data.get("last_list_type") in {"nearby", "trip"}:
            user_loc = context.user_data.get("last_nearby_coords")

        detail = format_restaurant_detail(r, user_location=user_loc)

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}"),
                    InlineKeyboardButton("⚠️ Segnala", callback_data=f"rep:{rid}"),
                ],
                [
                    InlineKeyboardButton("✅ Ho prenotato", callback_data=f"book:{rid}"),
                    InlineKeyboardButton("❌ Non utile", callback_data=f"notuse:{rid}"),
                ],
                [InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")],
            ]
        )
        await query.message.reply_text(detail, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

        photos = get_photos_for_restaurant(rid)
        if photos:
            await query.message.reply_photo(photos[0], caption="📷 Foto dalla community")
        return

    if data.startswith("book:"):
        rid = int(data.split(":", 1)[1])
        city_ctx = context.user_data.get("last_city_search")
        log_usage(user.id, "feedback_booked", city=city_ctx, restaurant_id=rid)

        try:
            context.job_queue.run_once(
                followup_job_callback,
                when=timedelta(hours=72),
                data={"chat_id": query.message.chat_id, "rid": rid, "user_id": user.id},
                name=f"followup_{user.id}_{rid}",
            )
        except Exception:
            pass

        await query.message.reply_text("✅ Perfetto. Tra qualche giorno ti chiederemo com'è andata 🙌")
        return

    if data.startswith("notuse:"):
        rid = int(data.split(":", 1)[1])
        city_ctx = context.user_data.get("last_city_search")
        log_usage(user.id, "feedback_not_useful", city=city_ctx, restaurant_id=rid)
        await query.message.reply_text("Ricevuto 👍 Questo ci aiuta a migliorare.")
        return

    if data.startswith("rategood:") or data.startswith("ratewarn:") or data.startswith("ratebad:"):
        tag, rid_str = data.split(":", 1)
        rid = int(rid_str) if rid_str else None
        evt = {"rategood": "followup_good", "ratewarn": "followup_warn", "ratebad": "followup_bad"}.get(tag, "followup_unknown")
        log_usage(user.id, evt, restaurant_id=rid)
        try:
            await query.edit_message_text("Grazie! 💚 Feedback registrato.")
        except Exception:
            await query.message.reply_text("Grazie! 💚 Feedback registrato.")
        return

    if data.startswith("listpage:"):
        page = int(data.split(":", 1)[1])
        ids = context.user_data.get("last_list_rows_ids") or []
        title = context.user_data.get("last_list_title") or "Risultati"
        if not ids:
            await query.message.reply_text("⚠️ Lista non disponibile, rifai la ricerca.", reply_markup=main_keyboard())
            return

        with closing(get_conn()) as conn:
            cur = conn.cursor()
            placeholders = ",".join("?" * len(ids))
            cur.execute(f"SELECT * FROM restaurants WHERE id IN ({placeholders})", ids)
            rows = cur.fetchall()

        rows_by_id = {int(r["id"]): r for r in rows}
        ordered = [rows_by_id[i] for i in ids if i in rows_by_id]

        user_loc = None
        if context.user_data.get("last_list_type") in {"nearby", "trip"}:
            user_loc = context.user_data.get("last_nearby_coords")

        msg, kb = build_list_message(ordered, title, page=page, user_location=user_loc)
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb)
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
        else:
            set_user_min_rating(user.id, float(val))
        settings = get_user_settings(user.id)
        await query.message.reply_text(
            f"Filtri aggiornati: <b>{format_active_filters(settings)}</b>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if data.startswith("type:"):
        val = data.split(":", 1)[1]
        set_user_type_filter(user.id, None if val == "none" else val)
        settings = get_user_settings(user.id)
        await query.message.reply_text(
            f"Filtri aggiornati: <b>{format_active_filters(settings)}</b>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if data == "reset:filters":
        clear_user_filters(user.id)
        await query.message.reply_text(
            "Filtri azzerati. La ricerca ora mostra tutto.",
            reply_markup=main_keyboard(),
        )
        return

    if data == "trip_notify":
        summary = context.user_data.get("last_trip_summary") or "Viaggio senza dettagli salvati"
        log_usage(user.id, "trip_notify")
        await query.message.reply_text("Messaggio inviato. Il founder riceverà la tua richiesta di miglioramento.")
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(ADMIN_CHAT_ID),
                    text=(
                        "🧳 Richiesta miglioramento viaggio\n"
                        f"Utente: {user.id} (@{user.username or '-'})\n\n"
                        f"Itinerario:\n{summary}"
                    ),
                )
            except Exception:
                pass
        return

    if data.startswith("suggest:"):
        payload = data.split(":", 1)[1].strip()
        log_usage(user.id, "suggest_city", city=payload)

        await query.message.reply_text("✅ Segnalazione inviata all'admin.", reply_markup=main_keyboard())

        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(ADMIN_CHAT_ID),
                    text=f"📩 Suggerimento città/zona: {payload}\nDa utente: {user.id} (@{user.username or '-'})",
                )
            except Exception:
                pass
        return



def build_application():
    ensure_schema()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))

    segnala_conv = ConversationHandler(
        entry_points=[
            CommandHandler("segnala", segnala_start),
            MessageHandler(filters.Regex("^➕ Segnala ristorante$"), segnala_start),
        ],
        states={
            SEGNALA_NOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, segnala_nome)],
            SEGNALA_CITTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, segnala_citta)],
            SEGNALA_TIPO: [CallbackQueryHandler(segnala_tipo, pattern=r"^stype:")],
            SEGNALA_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, segnala_note)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Annulla$"), segnala_cancel),
            CommandHandler("cancel", segnala_cancel),
        ],
        allow_reentry=True,
    )
    app.add_handler(segnala_conv)

    viaggio_conv = ConversationHandler(
        entry_points=[
            CommandHandler("viaggio", viaggio_start),
            MessageHandler(filters.Regex("^🧳 Viaggio$"), viaggio_start),
        ],
        states={
            TRAVEL_CITIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, viaggio_cities)],
            TRAVEL_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, viaggio_address)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Annulla$"), viaggio_cancel),
            CommandHandler("cancel", viaggio_cancel),
        ],
        allow_reentry=True,
    )
    app.add_handler(viaggio_conv)

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
