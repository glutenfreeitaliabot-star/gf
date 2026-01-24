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

from import_app_restaurants import import_app_restaurants
from maps_utils import build_google_maps_multi_url


# ==========================
# CONFIG
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # chat id admin per /stats e notifiche
DB_PATH = "restaurants.db"

PAGE_SIZE = 5
pending_photo_for_user = {}

RADIUS_OPTIONS = [1, 3, 5, 10, 15, 20]


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
        # ==========================
        # MIGRAZIONI SAFE (DB già esistenti)
        # ==========================
        cur.execute("PRAGMA table_info(restaurants)")
        cols = {row[1] for row in cur.fetchall()}  # row[1] = nome colonna

        if "types" not in cols:
            cur.execute("ALTER TABLE restaurants ADD COLUMN types TEXT")
        if "phone" not in cols:
            cur.execute("ALTER TABLE restaurants ADD COLUMN phone TEXT")
        if "rating_online_gf" not in cols:
            cur.execute("ALTER TABLE restaurants ADD COLUMN rating_online_gf REAL")

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
                ON CONFLICT(user_id) DO UPDATE SET min_rating = excluded.min_rating
                """,
                (user_id, value),
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
            SELECT file_id
            FROM photos
            WHERE restaurant_id = ?
            ORDER BY created_at DESC
            LIMIT 3
            """,
            (restaurant_id,),
        )
        return [r["file_id"] for r in cur.fetchall()]


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
    """
    Normalizza lat/lon anche se:
    - stringhe con virgola
    - spazi
    - valori scambiati
    - valori fuori range
    """
    lat = _to_float(lat_raw)
    lon = _to_float(lon_raw)
    if lat is None or lon is None:
        return None, None

    # fuori range -> prova swap
    if abs(lat) > 90 or abs(lon) > 180:
        lat, lon = lon, lat

    # ancora fuori range => invalido
    if abs(lat) > 90 or abs(lon) > 180:
        return None, None

    # Heuristica Italia: lat tipicamente 36-47, lon 6-19
    # Se invertiti, swap
    if (6 <= lat <= 19) and (36 <= lon <= 47):
        lat, lon = lon, lat

    return lat, lon


def haversine_km(lat1, lon1, lat2, lon2) -> Optional[float]:
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


def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["⭐ I miei preferiti", "⚙️ Filtri"],
            ["🛒 Shop"],
        ],
        resize_keyboard=True,
    )


def radius_keyboard():
    rows = [
        [f"{RADIUS_OPTIONS[0]} km", f"{RADIUS_OPTIONS[1]} km"],
        [f"{RADIUS_OPTIONS[2]} km", f"{RADIUS_OPTIONS[3]} km"],
        [f"{RADIUS_OPTIONS[4]} km", f"{RADIUS_OPTIONS[5]} km"],
        ["❌ Annulla"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def location_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Invia posizione 📍", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def format_restaurant_detail(r: sqlite3.Row, user_location: Optional[Tuple[float, float]] = None) -> str:
    name = r["name"]
    city = r["city"]
    address = r["address"] or "Indirizzo non disponibile"
    notes = r["notes"] or "—"
    rating_val = r["rating"]
    last_update = r["last_update"]
    phone = (r["phone"] or "").strip() if "phone" in r.keys() and r["phone"] else ""

    rating = f"{float(rating_val):.1f}⭐" if rating_val is not None else "n.d."
    update_str = f" (aggiornato: {last_update})" if last_update else ""

    lat, lon = _normalize_coords(r["lat"], r["lon"])

    distance_str = ""
    if user_location and lat is not None and lon is not None:
        d = haversine_km(user_location[0], user_location[1], lat, lon)
        if d is not None:
            distance_str = f"\n📏 Distanza: {d*1000:.0f} m" if d < 1 else f"\n📏 Distanza: {d:.1f} km"

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"

    if phone:
        phone_line = f"\n📞 Telefono: <b>{phone}</b>\n👉 Tocca il numero per chiamare"
    else:
        phone_line = "\n📞 Telefono: <b>non disponibile</b>\n👉 Contatta direttamente il ristorante"

    text = (
        f"🍽 <b>{name}</b>\n"
        f"📍 <b>{city}</b> – {address}\n"
        f"⭐ Rating Google: {rating}{update_str}"
        f"{distance_str}"
        f"{phone_line}\n\n"
        f"<b>Note:</b> {notes}\n\n"
        "ℹ️ <b>Nota importante</b>\n"
        "Mostriamo questo locale in base a informazioni e recensioni pubbliche online.\n"
        "Le condizioni per il senza glutine possono cambiare (menu, gestione, procedure).\n"
        "👉 Contatta sempre il ristorante prima di andare.\n\n"
        f"🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )
    return text


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


def query_nearby(user_id: int, lat_user: float, lon_user: float, radius_km: float, max_results: int = 200) -> List[sqlite3.Row]:
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    lat_user = _to_float(lat_user)
    lon_user = _to_float(lon_user)
    if lat_user is None or lon_user is None:
        return []

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        # NON filtriamo su IS NOT NULL perché spesso nel DB hai stringhe vuote non NULL
        cur.execute("SELECT * FROM restaurants")
        rows = cur.fetchall()

    valid_coords = 0
    results: List[Tuple[float, sqlite3.Row]] = []

    for r in rows:
        if min_rating is not None and r["rating"] is not None and float(r["rating"]) < float(min_rating):
            continue

        lat, lon = _normalize_coords(r["lat"], r["lon"])
        if lat is None or lon is None:
            continue

        valid_coords += 1
        d = haversine_km(lat_user, lon_user, lat, lon)
        if d is not None and d <= radius_km:
            results.append((d, r))

    # Debug console (Railway logs)
    print(f"[nearby] radius={radius_km}km | rows={len(rows)} | valid_coords={valid_coords} | matches={len(results)}")

    results.sort(key=lambda x: x[0])
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

    lines = [f"{title} (pagina {page+1}/{total_pages}):", ""]
    kb_rows = []

    for idx, r in enumerate(subset, start=1):
        rid = int(r["id"])
        display_n = start + idx
        rating_val = r["rating"]
        rating = f"{float(rating_val):.1f}⭐" if rating_val is not None else "n.d."
        lines.append(f"{display_n}. {r['name']} – {rating}")
        kb_rows.append([InlineKeyboardButton(f"Dettagli {display_n}", callback_data=f"details:{rid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"listpage:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"listpage:{page+1}"))
    if nav:
        kb_rows.append(nav)

    # 🗺 Mostra su mappa (Top 10) — usa TUTTA la lista, non solo la pagina
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



# ==========================
# HANDLERS
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "start")

    msg = (
        "Ciao 👋 benvenuto in <b>GlutenFreeBot</b> 🧡\n\n"
        "Trova ristoranti con recensioni che parlano di gluten free.\n\n"
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
                f"😔 Nessun risultato per <b>{city}</b>.\nVuoi segnalarla all’admin?",
                parse_mode="HTML",
                reply_markup=kb,
            )
            await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
            return

        context.user_data["last_list_rows_ids"] = [int(r["id"]) for r in rows]
        context.user_data["last_list_title"] = f"🔎 Ho trovato <b>{len(rows)}</b> locali a <b>{city}</b>"
        context.user_data["last_list_type"] = "city"

        msg, kb = build_list_message(rows, context.user_data["last_list_title"], page=0, user_location=None)
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
        await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
        return

    if text == "📍 Vicino a me":
        context.user_data["awaiting_radius"] = True
        await update.message.reply_text("Scegli il raggio di ricerca:", reply_markup=radius_keyboard())
        return

    if context.user_data.get("awaiting_radius"):
        if text == "❌ Annulla":
            context.user_data["awaiting_radius"] = False
            await update.message.reply_text("Ok, annullato.", reply_markup=main_keyboard())
            return

        if "km" in text.lower():
            try:
                radius = float(text.lower().replace("km", "").replace(" ", "").strip())
            except Exception:
                radius = 5.0

            if radius not in [float(x) for x in RADIUS_OPTIONS]:
                radius = 5.0

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
        min_rating = settings.get("min_rating")
        current = f"{min_rating:.1f}⭐" if min_rating is not None else "nessuno"
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("≥ 4.0⭐", callback_data="filt:4.0"),
                 InlineKeyboardButton("≥ 4.5⭐", callback_data="filt:4.5")],
                [InlineKeyboardButton("❌ Nessun filtro", callback_data="filt:none")],
            ]
        )
        await update.message.reply_text(
            f"Rating minimo attuale: <b>{current}</b>\nScegli:",
            parse_mode="HTML",
            reply_markup=kb,
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
            f"😔 Nessun locale trovato entro <b>{radius:g} km</b>.\nVuoi segnalarci la zona all’admin?",
            parse_mode="HTML",
            reply_markup=kb,
        )
        await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())
        return

    context.user_data["last_nearby_coords"] = (_to_float(lat_user), _to_float(lon_user))
    context.user_data["last_list_rows_ids"] = [int(r["id"]) for r in rows]
    context.user_data["last_list_title"] = f"📍 Locali entro <b>{radius:g} km</b> — trovati <b>{len(rows)}</b>"
    context.user_data["last_list_type"] = "nearby"

    msg, kb = build_list_message(
    rows,
    context.user_data["last_list_title"],
    page=0,
    user_location=context.user_data.get("last_nearby_coords"),
    )
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)
    await update.message.reply_text("Menu 👇", reply_markup=main_keyboard())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_photo_for_user:
        await update.message.reply_text(
            "Per collegare una foto ad un locale, apri i dettagli e premi '📷 Aggiungi foto'.",
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
        if context.user_data.get("last_list_type") == "nearby":
            user_loc = context.user_data.get("last_nearby_coords")

        detail = format_restaurant_detail(r, user_location=user_loc)

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}"),
                 InlineKeyboardButton("⚠️ Segnala", callback_data=f"rep:{rid}")],
                [InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")],
            ]
        )
        await query.message.reply_text(detail, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

        photos = get_photos_for_restaurant(rid)
        if photos:
            await query.message.reply_photo(photos[0], caption="📷 Foto dalla community")
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
        if context.user_data.get("last_list_type") == "nearby":
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
            await query.message.reply_text("Filtro rating disattivato.", reply_markup=main_keyboard())
        else:
            set_user_min_rating(user.id, float(val))
            await query.message.reply_text(f"Rating minimo impostato a {val}⭐.", reply_markup=main_keyboard())
        return

    if data.startswith("suggest:"):
        payload = data.split(":", 1)[1].strip()
        log_usage(user.id, "suggest_city", city=payload)

        await query.message.reply_text("✅ Segnalazione inviata all’admin.", reply_markup=main_keyboard())

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
    
    
    
