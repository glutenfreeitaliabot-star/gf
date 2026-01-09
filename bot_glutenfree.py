import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Optional, List, Tuple

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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "restaurants.db")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

pending_photo_for_user = {}  # user_id -> restaurant_id

# ==========================
# DB helpers
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
                message TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                restaurant_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                city TEXT,
                restaurant_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                chat_id INTEGER,
                action TEXT NOT NULL,
                payload TEXT,
                city TEXT,
                restaurant_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_actions_user_id ON user_actions(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_actions_action ON user_actions(action)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_actions_city ON user_actions(city)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_actions_created_at ON user_actions(created_at)")

        conn.commit()


def log_usage(user_id: int, event: str, city: Optional[str] = None, restaurant_id: Optional[int] = None):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO usage_events (user_id, event, city, restaurant_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                event,
                city,
                int(restaurant_id) if restaurant_id is not None else None,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()


def log_action(
    user_id: int,
    action: str,
    payload: Optional[str] = None,
    city: Optional[str] = None,
    restaurant_id: Optional[int] = None,
    username: Optional[str] = None,
    chat_id: Optional[int] = None,
):
    """Log generico e dettagliato di qualsiasi azione utente.

    payload viene troncato per evitare esplosioni di DB.
    """
    if payload is not None and len(payload) > 1000:
        payload = payload[:1000] + "…"

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_actions (user_id, username, chat_id, action, payload, city, restaurant_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                (username or None),
                int(chat_id) if chat_id is not None else None,
                action,
                payload,
                city,
                int(restaurant_id) if restaurant_id is not None else None,
                datetime.utcnow().isoformat(),
            ),
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
            (int(user_id), int(restaurant_id), datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_favorites(user_id: int) -> List[sqlite3.Row]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT r.*
            FROM restaurants r
            JOIN favorites f ON f.restaurant_id = r.id
            WHERE f.user_id = ?
            ORDER BY f.created_at DESC
            """,
            (int(user_id),),
        )
        return cur.fetchall()


def add_report(user_id: int, restaurant_id: int, message: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO reports (user_id, restaurant_id, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(user_id), int(restaurant_id), message, datetime.utcnow().isoformat()),
        )
        conn.commit()


def add_photo(restaurant_id: int, file_id: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO photos (restaurant_id, file_id, created_at)
            VALUES (?, ?, ?)
            """,
            (int(restaurant_id), file_id, datetime.utcnow().isoformat()),
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
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(restaurant_id),),
        )
        rows = cur.fetchall()
        return [r["file_id"] for r in rows]


def get_user_settings(user_id: int) -> dict:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT min_rating FROM user_settings WHERE user_id = ?", (int(user_id),))
        row = cur.fetchone()
    return {"min_rating": row["min_rating"] if row else None}


def set_user_min_rating(user_id: int, rating: Optional[float]):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, min_rating)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET min_rating=excluded.min_rating
            """,
            (int(user_id), rating),
        )
        conn.commit()


# ==========================
# Distance helpers
# ==========================


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> Optional[float]:
    try:
        R = 6371.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c
    except Exception:
        return None


def _normalize_coords(lat: Optional[str], lon: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    if not lat or not lon:
        return None, None
    try:
        return float(str(lat).strip()), float(str(lon).strip())
    except Exception:
        return None, None


# ==========================
# Text formatting
# ==========================


def format_restaurant_row(r: sqlite3.Row, idx: int, distance_km: Optional[float] = None) -> str:
    name = r["name"]
    city = r["city"]
    rating_val = r["rating"]
    rating = f"{float(rating_val):.1f}⭐" if rating_val is not None else "n.d."

    dist = ""
    if distance_km is not None:
        dist = f" • {distance_km*1000:.0f}m" if distance_km < 1 else f" • {distance_km:.1f}km"

    return f"{idx}. <b>{name}</b> ({city}) • {rating}{dist}"


def format_restaurant_detail(r: sqlite3.Row, user_location: Optional[Tuple[float, float]] = None) -> str:
    name = r["name"]
    city = r["city"]
    address = r["address"] or ""
    notes = r["notes"] or ""
    phone = (r["phone"] or "").strip()
    rating_val = r["rating"]
    last_update = (r["last_update"] or "").strip()

    rating = f"{float(rating_val):.1f}⭐" if rating_val is not None else "n.d."
    update_str = f" (aggiornato: {last_update})" if last_update else ""

    lat, lon = _normalize_coords(r["lat"], r["lon"])

    distance_str = ""
    if user_location and lat is not None and lon is not None:
        d = haversine_km(user_location[0], user_location[1], lat, lon)
        if d is not None:
            distance_str = f"\n📏 Distanza: {d*1000:.0f} m" if d < 1 else f"\n📏 Distanza: {d:.1f} km"

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"
    if lat is not None and lon is not None:
        maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

    text = (
        f"🍽️ <b>{name}</b>\n"
        f"📍 {city}\n"
        f"🏠 {address}\n"
        f"⭐ Rating: {rating}{update_str}"
        f"{distance_str}\n\n"
    )

    if phone:
        text += f"📞 {phone}\n\n"
    if notes:
        text += f"📝 {notes}\n\n"

    text += (
        "⚠️ <b>Nota</b>: le info vengono da community/app e potrebbero non essere perfette. "
        "Verifica sempre in loco (ingredienti e contaminazione) prima di andare.\n\n"
        f"🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )
    return text


def format_share_message(r: sqlite3.Row) -> str:
    """Testo pronto da inoltrare/condividere."""
    name = r["name"]
    city = r["city"]
    address = r["address"] or ""
    notes = r["notes"] or ""
    phone = (r["phone"] or "").strip()
    rating_val = r["rating"]
    rating = f"{float(rating_val):.1f}⭐" if rating_val is not None else "n.d."
    lat, lon = _normalize_coords(r["lat"], r["lon"])

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"
    if lat is not None and lon is not None:
        maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

    parts = [
        f"🍽️ <b>{name}</b>",
        f"📍 {city}",
    ]
    if address:
        parts.append(f"🏠 {address}")
    parts.append(f"⭐ Rating: {rating}")
    if phone:
        parts.append(f"📞 {phone}")
    if notes:
        parts.append(f"📝 {notes}")
    parts.append(f"🗺️ {maps_url}")
    parts.append("")
    parts.append("⚠️ Nota: verifica sempre in loco la gestione del senza glutine (ingredienti/contaminazione).")
    return "\n".join(parts)


# ==========================
# Queries
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
            (city.strip(),),
        )
        rows = cur.fetchall()

    if min_rating is None:
        return rows

    filtered = []
    for r in rows:
        rv = r["rating"]
        if rv is None:
            filtered.append(r)
        else:
            try:
                if float(rv) >= float(min_rating):
                    filtered.append(r)
            except Exception:
                pass
    return filtered


def query_nearby(lat: float, lon: float, radius_km: float, user_id: int) -> List[Tuple[sqlite3.Row, float]]:
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM restaurants")
        rows = cur.fetchall()

    out = []
    for r in rows:
        rlat, rlon = _normalize_coords(r["lat"], r["lon"])
        if rlat is None or rlon is None:
            continue

        d = haversine_km(lat, lon, rlat, rlon)
        if d is None or d > radius_km:
            continue

        if min_rating is not None:
            rv = r["rating"]
            if rv is not None:
                try:
                    if float(rv) < float(min_rating):
                        continue
                except Exception:
                    pass

        out.append((r, d))

    out.sort(key=lambda x: x[1])
    return out


# ==========================
# UI helpers
# ==========================


def main_keyboard():
    buttons = [
        ["🔍 Cerca per città", "📍 Vicino a me"],
        ["⭐ Preferiti", "⚙️ Filtri"],
        ["🛒 Shop"],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def radius_keyboard():
    buttons = [["1 km", "3 km", "5 km"], ["10 km", "20 km"], ["❌ Annulla"]]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def filters_keyboard(current_min: Optional[float]):
    label = f"Min rating: {current_min}" if current_min is not None else "Min rating: (nessuno)"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⭐ 4.0+", callback_data="minrating:4.0")],
            [InlineKeyboardButton("⭐ 4.5+", callback_data="minrating:4.5")],
            [InlineKeyboardButton("⭐ 4.8+", callback_data="minrating:4.8")],
            [InlineKeyboardButton("❌ Rimuovi filtro", callback_data="minrating:off")],
        ]
    )
    return label, kb


def build_list_message(rows: List[sqlite3.Row], title: str, page: int = 0, per_page: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    total = len(rows)
    start = page * per_page
    end = start + per_page
    chunk = rows[start:end]

    msg = f"<b>{title}</b>\n\n"
    for i, r in enumerate(chunk, start=1 + start):
        msg += format_restaurant_row(r, i) + "\n"

    buttons = []
    for i, r in enumerate(chunk, start=1 + start):
        rid = int(r["id"])
        buttons.append([InlineKeyboardButton(f"Dettagli {i}", callback_data=f"details:{rid}")])

    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️ Indietro", callback_data=f"listpage:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Avanti ➡️", callback_data=f"listpage:{page+1}"))
    if nav:
        buttons.append(nav)

    kb = InlineKeyboardMarkup(buttons)
    return msg, kb


def build_list_message_nearby(rows: List[Tuple[sqlite3.Row, float]], title: str, page: int = 0, per_page: int = 10) -> Tuple[str, InlineKeyboardMarkup]:
    total = len(rows)
    start = page * per_page
    end = start + per_page
    chunk = rows[start:end]

    msg = f"<b>{title}</b>\n\n"
    for i, (r, d) in enumerate(chunk, start=1 + start):
        msg += format_restaurant_row(r, i, distance_km=d) + "\n"

    buttons = []
    for i, (r, d) in enumerate(chunk, start=1 + start):
        rid = int(r["id"])
        buttons.append([InlineKeyboardButton(f"Dettagli {i}", callback_data=f"details:{rid}")])

    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("⬅️ Indietro", callback_data=f"listpage:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Avanti ➡️", callback_data=f"listpage:{page+1}"))
    if nav:
        buttons.append(nav)

    kb = InlineKeyboardMarkup(buttons)
    return msg, kb


# ==========================
# Bot handlers
# ==========================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()
    log_usage(user.id, "start")
    await update.message.reply_text(
        "👋 Ciao! Sono il bot GlutenFree Italia.\n\n"
        "Puoi cercare ristoranti gluten free per città o vicino a te.\n"
        "Usa i bottoni qui sotto 👇",
        reply_markup=main_keyboard(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    if text == "🔍 Cerca per città":
        context.user_data["state"] = "awaiting_city"
        log_usage(user.id, "search_city_start")
        await update.message.reply_text("🏙️ Scrivi la città esatta (es. Milano).")
        return

    if text == "📍 Vicino a me":
        context.user_data["state"] = "awaiting_radius"
        log_usage(user.id, "nearby_start")
        await update.message.reply_text("📏 Seleziona il raggio di ricerca:", reply_markup=radius_keyboard())
        return

    if text == "⭐ Preferiti":
        log_usage(user.id, "favorites_open")
        favs = get_favorites(user.id)
        if not favs:
            await update.message.reply_text("⭐ Non hai ancora preferiti.", reply_markup=main_keyboard())
            return
        context.user_data["last_list_rows_ids"] = [int(r["id"]) for r in favs]
        context.user_data["last_list_title"] = "⭐ Preferiti"
        context.user_data["last_list_type"] = "favorites"
        msg, kb = build_list_message(favs, "⭐ Preferiti", page=0)
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
        return

    if text == "⚙️ Filtri":
        settings = get_user_settings(user.id)
        label, kb = filters_keyboard(settings.get("min_rating"))
        log_usage(user.id, "filters_open")
        await update.message.reply_text(f"⚙️ Filtri\n\n{label}", reply_markup=kb)
        await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
        return

    if text == "🛒 Shop":
        log_usage(user.id, "shop_open")
        await update.message.reply_text(
            "🛒 Shop (placeholder)\n\nQui potremo mettere prodotti consigliati (senza glutine).",
            reply_markup=main_keyboard(),
        )
        return

    # States
    state = context.user_data.get("state")

    if state == "awaiting_city":
        city = text
        context.user_data["state"] = None
        context.user_data["last_city_search"] = city
        log_usage(user.id, "search_city_submit", city=city)

        rows = query_by_city(city, user.id)
        if not rows:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📩 Suggerisci città", callback_data=f"suggest:{city}")]])
            await update.message.reply_text(
                f"😔 Nessun risultato per <b>{city}</b>.\nVuoi segnalarla all’admin?",
                parse_mode="HTML",
                reply_markup=kb,
            )
            await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
            return

        context.user_data["last_list_rows_ids"] = [int(r["id"]) for r in rows]
        context.user_data["last_list_title"] = f"Risultati: {city}"
        context.user_data["last_list_type"] = "city"
        msg, kb = build_list_message(rows, f"Risultati: {city}", page=0)
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
        await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
        return

    if state == "awaiting_radius":
        if text == "❌ Annulla":
            context.user_data["state"] = None
            await update.message.reply_text("Operazione annullata.", reply_markup=main_keyboard())
            return

        # parse "5 km"
        try:
            radius = float(text.replace("km", "").replace("KM", "").strip())
        except Exception:
            await update.message.reply_text("⚠️ Raggio non valido. Seleziona un valore dal menu.", reply_markup=radius_keyboard())
            return

        context.user_data["state"] = None
        context.user_data["nearby_radius_km"] = radius
        log_usage(user.id, "nearby_radius_selected")

        # request location
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Invia la mia posizione", request_location=True)], ["❌ Annulla"]],
            resize_keyboard=True,
        )
        await update.message.reply_text("📍 Ora inviami la tua posizione:", reply_markup=kb)
        context.user_data["state"] = "awaiting_location"
        return

    # Default fallback
    await update.message.reply_text("Non ho capito 🤔. Usa il menu 👇", reply_markup=main_keyboard())


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = context.user_data.get("state")

    if state != "awaiting_location":
        await update.message.reply_text("Usa il menu 👇", reply_markup=main_keyboard())
        return

    loc = update.message.location
    if not loc:
        await update.message.reply_text("⚠️ Non ho ricevuto la posizione.", reply_markup=main_keyboard())
        return

    context.user_data["state"] = None
    context.user_data["last_nearby_coords"] = (loc.latitude, loc.longitude)
    context.user_data["last_list_type"] = "nearby"
    radius = float(context.user_data.get("nearby_radius_km") or 5)
    log_usage(user.id, "nearby_location_received")

    rows = query_nearby(loc.latitude, loc.longitude, radius, user.id)
    if not rows:
        await update.message.reply_text("😔 Nessun locale trovato nel raggio selezionato.", reply_markup=main_keyboard())
        return

    context.user_data["last_list_rows_ids"] = [int(r["id"]) for r, _ in rows]
    context.user_data["last_list_title"] = f"Vicino a me ({radius:.0f} km)"
    msg, kb = build_list_message_nearby(rows, f"Vicino a me ({radius:.0f} km)", page=0)
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
    await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message.photo:
        return

    rid = pending_photo_for_user.get(user.id)
    if not rid:
        await update.message.reply_text("📷 Foto ricevuta, ma non so a quale locale associarla. Apri i dettagli e premi “Aggiungi foto”.")
        return

    file_id = update.message.photo[-1].file_id
    add_photo(rid, file_id)
    pending_photo_for_user.pop(user.id, None)
    log_usage(user.id, "photo_added", restaurant_id=rid)
    await update.message.reply_text("✅ Foto aggiunta! Grazie 🙏", reply_markup=main_keyboard())


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
            """
        )
        events = cur.fetchall()

    msg = "<b>📊 STATS (ADMIN)</b>\n\n"
    msg += f"👥 Utenti unici: <b>{users}</b>\n"
    msg += f"🧾 Eventi totali: <b>{events_total}</b>\n\n"
    msg += "<b>Top funzioni</b>\n"
    for e in events:
        msg += f"• {e['event']}: <b>{e['n']}</b>\n"

    # Top città cercate (strutturato)
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT city, COUNT(*) AS n
            FROM user_actions
            WHERE action = 'search_city' AND city IS NOT NULL AND TRIM(city) <> ''
            GROUP BY city
            ORDER BY n DESC
            LIMIT 15
            """
        )
        top_cities = cur.fetchall()

        cur.execute(
            """
            SELECT action, COUNT(*) AS n
            FROM user_actions
            GROUP BY action
            ORDER BY n DESC
            LIMIT 12
            """
        )
        top_actions = cur.fetchall()

        cur.execute(
            """
            SELECT user_id, username, action, created_at
            FROM user_actions
            ORDER BY id DESC
            LIMIT 10
            """
        )
        last_actions = cur.fetchall()

    msg += "\n<b>Top città cercate</b>\n"
    if top_cities:
        for r in top_cities:
            msg += f"• {r['city']}: <b>{r['n']}</b>\n"
    else:
        msg += "• (nessun dato)\n"

    msg += "\n<b>Top azioni utente (dettagliate)</b>\n"
    if top_actions:
        for r in top_actions:
            msg += f"• {r['action']}: <b>{r['n']}</b>\n"
    else:
        msg += "• (nessun dato)\n"

    msg += "\n<b>Ultime azioni</b>\n"
    if last_actions:
        for r in last_actions:
            uname = r["username"] or "-"
            msg += f"• {r['created_at']}: {r['user_id']} (@{uname}) → {r['action']}\n"

    await update.message.reply_text(msg, parse_mode="HTML")


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
        if context.user_data.get("last_list_type") == "nearby":
            user_loc = context.user_data.get("last_nearby_coords")

        detail = format_restaurant_detail(r, user_location=user_loc)

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}"),
                 InlineKeyboardButton("⚠️ Segnala", callback_data=f"rep:{rid}")],
                [InlineKeyboardButton("📤 Condividi", callback_data=f"share:{rid}"),
                 InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")],
            ]
        )
        await query.message.reply_text(detail, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

        photos = get_photos_for_restaurant(rid)
        if photos:
            await query.message.reply_photo(photos[0], caption="📷 Foto dalla community")
        return

    if data.startswith("share:"):
        rid = int(data.split(":", 1)[1])

        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM restaurants WHERE id = ?", (rid,))
            r = cur.fetchone()

        if not r:
            await query.message.reply_text("⚠️ Locale non trovato.", reply_markup=main_keyboard())
            return

        city_ctx = context.user_data.get("last_city_search")
        log_usage(user.id, "share_click", city=city_ctx, restaurant_id=rid)

        share_text = format_share_message(r)
        await query.message.reply_text(
            "📤 <b>Messaggio pronto da inoltrare</b> (forward o copia-incolla):\n\n" + share_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
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

        msg, kb = build_list_message(ordered, title, page=page)
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

    if data.startswith("minrating:"):
        val = data.split(":", 1)[1]
        if val == "off":
            set_user_min_rating(user.id, None)
            log_usage(user.id, "filter_minrating_off")
            await query.message.reply_text("✅ Filtro rimosso.", reply_markup=main_keyboard())
            return
        try:
            rating = float(val)
        except Exception:
            rating = None
        set_user_min_rating(user.id, rating)
        log_usage(user.id, "filter_minrating_set")
        await query.message.reply_text(f"✅ Impostato filtro min rating: {rating}", reply_markup=main_keyboard())
        return

    if data.startswith("suggest:"):
        payload = data.split(":", 1)[1].strip()
        log_usage(user.id, "suggest_city", city=payload)
        await query.message.reply_text("📩 Suggerimento inviato all’admin. Grazie!", reply_markup=main_keyboard())
        if ADMIN_CHAT_ID:
            try:
                await query.get_bot().send_message(
                    chat_id=int(ADMIN_CHAT_ID),
                    text=f"📩 Suggerimento città/zona: {payload}\nDa utente: {user.id} (@{user.username or '-'})",
                )
            except Exception:
                pass
        return


async def audit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logger 'catch-all' per i messaggi: registra cosa fa l'utente (testo, location, foto, ecc.)."""
    try:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not update.message:
            return

        state = context.user_data.get("state")

        if update.message.text:
            text = (update.message.text or "").strip()
            log_action(
                user.id,
                action="message_text",
                payload=text,
                city=context.user_data.get("last_city_search"),
                username=user.username,
                chat_id=chat.id if chat else None,
            )

            if state == "awaiting_city":
                log_action(
                    user.id,
                    action="search_city",
                    payload=text,
                    city=text,
                    username=user.username,
                    chat_id=chat.id if chat else None,
                )

            if state == "awaiting_radius":
                log_action(
                    user.id,
                    action="set_radius_input",
                    payload=text,
                    city=context.user_data.get("last_city_search"),
                    username=user.username,
                    chat_id=chat.id if chat else None,
                )

        if update.message.location:
            loc = update.message.location
            log_action(
                user.id,
                action="message_location",
                payload=f"{loc.latitude},{loc.longitude}",
                city=context.user_data.get("last_city_search"),
                username=user.username,
                chat_id=chat.id if chat else None,
            )

        if update.message.photo:
            log_action(
                user.id,
                action="message_photo",
                payload="photo",
                city=context.user_data.get("last_city_search"),
                username=user.username,
                chat_id=chat.id if chat else None,
            )
    except Exception:
        return


async def audit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logger per callback buttons."""
    try:
        q = update.callback_query
        if not q:
            return
        user = q.from_user
        chat = q.message.chat if q.message else None
        data = (q.data or "").strip()

        log_action(
            user.id,
            action="callback",
            payload=data,
            city=context.user_data.get("last_city_search"),
            username=user.username,
            chat_id=chat.id if chat else None,
        )

        if data.startswith("details:"):
            rid = int(data.split(":", 1)[1])
            log_action(
                user.id,
                action="view_details",
                payload=data,
                city=context.user_data.get("last_city_search"),
                restaurant_id=rid,
                username=user.username,
                chat_id=chat.id if chat else None,
            )
        elif data.startswith("share:"):
            rid = int(data.split(":", 1)[1])
            log_action(
                user.id,
                action="share_click",
                payload=data,
                city=context.user_data.get("last_city_search"),
                restaurant_id=rid,
                username=user.username,
                chat_id=chat.id if chat else None,
            )
        elif data.startswith("fav:"):
            rid = int(data.split(":", 1)[1])
            log_action(
                user.id,
                action="favorite_click",
                payload=data,
                city=context.user_data.get("last_city_search"),
                restaurant_id=rid,
                username=user.username,
                chat_id=chat.id if chat else None,
            )
        elif data.startswith("rep:"):
            rid = int(data.split(":", 1)[1])
            log_action(
                user.id,
                action="report_click",
                payload=data,
                city=context.user_data.get("last_city_search"),
                restaurant_id=rid,
                username=user.username,
                chat_id=chat.id if chat else None,
            )
        elif data.startswith("photo:"):
            rid = int(data.split(":", 1)[1])
            log_action(
                user.id,
                action="add_photo_click",
                payload=data,
                city=context.user_data.get("last_city_search"),
                restaurant_id=rid,
                username=user.username,
                chat_id=chat.id if chat else None,
            )
    except Exception:
        return


# ==========================
# Import (CSV app)
# ==========================


def import_app_restaurants(csv_path: str = "app_restaurants.csv"):
    if not os.path.exists(csv_path):
        return

    import csv

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # normalize headers
    def get_val(row, *keys):
        for k in keys:
            if k in row and row[k] is not None:
                return str(row[k]).strip()
        return ""

    to_insert = []
    for r in rows:
        name = get_val(r, "name", "Name", "nome", "Nome")
        city = get_val(r, "city", "City", "città", "Città", "citta", "Citta")
        address = get_val(r, "address", "Address", "indirizzo", "Indirizzo")
        notes = get_val(r, "notes", "Notes", "note", "Note", "descrizione", "Descrizione")
        lat = get_val(r, "lat", "Lat", "latitude", "Latitude", "LAT")
        lon = get_val(r, "lon", "Lon", "longitude", "Longitude", "LON")
        rating = get_val(r, "rating", "Rating", "valutazione", "Valutazione")
        last_update = get_val(r, "last_update", "LastUpdate", "lastUpdate", "Aggiornato", "aggiornato")
        types = get_val(r, "types", "Types", "tipo", "Tipo")
        phone = get_val(r, "phone", "Phone", "telefono", "Telefono")

        if not name or not city:
            continue

        rating_val = None
        if rating:
            try:
                rating_val = float(str(rating).replace(",", "."))
            except Exception:
                rating_val = None

        to_insert.append((name, city, address, notes, "app", lat or None, lon or None, rating_val, last_update or None, types or None, phone or None))

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        # remove old app rows
        cur.execute("DELETE FROM restaurants WHERE source = 'app'")
        cur.executemany(
            """
            INSERT INTO restaurants (name, city, address, notes, source, lat, lon, rating, last_update, types, phone)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            to_insert,
        )
        conn.commit()


# ==========================
# App builder
# ==========================


def build_application():
    ensure_schema()
    app = Application.builder().token(BOT_TOKEN).build()

    # Audit logging (gruppo 0): registra tutto ciò che fa l'utente senza bloccare gli altri handler
    app.add_handler(MessageHandler(filters.ALL, audit_message), group=0)
    app.add_handler(CallbackQueryHandler(audit_callback), group=0)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
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
