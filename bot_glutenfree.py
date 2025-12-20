import math
import sqlite3
import os
from contextlib import closing
from datetime import datetime
from typing import Optional, List, Tuple

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
    ConversationHandler,
    ContextTypes,
    filters,
)

# ==========================
# CONFIG
# ==========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "restaurants.db"
PAGE_SIZE = 5

# Conversation "aggiungi ristorante"
ADD_NAME, ADD_CITY, ADD_ADDRESS, ADD_NOTES = range(4)

pending_photo_for_user: dict[int, int] = {}

# ==========================
# DB UTILS
# ==========================

def get_conn():
    return sqlite3.connect(DB_PATH)


def _get_restaurants_columns(cur) -> set:
    cur.execute("PRAGMA table_info(restaurants)")
    cols = {row[1].lower() for row in cur.fetchall()}
    return cols


def ensure_schema():
    """
    NON ricrea restaurants.
    Crea solo tabelle di supporto e prova ad aggiungere colonne mancanti (best effort).
    """
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # support tables
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

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(referrer_id, referred_id)
            )
            """
        )

        # Best effort: se restaurants esiste, aggiungi phone se manca
        try:
            cols = _get_restaurants_columns(cur)
            if "phone" not in cols:
                cur.execute("ALTER TABLE restaurants ADD COLUMN phone TEXT")
            if "types" not in cols:
                # se non c'è types non lo aggiungo per forza (dipende dal tuo CSV),
                # ma non fa danni aggiungerlo.
                cur.execute("ALTER TABLE restaurants ADD COLUMN types TEXT")
        except Exception:
            # se restaurants non esiste ancora o ALTER non possibile, ignoriamo
            pass

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


def get_favorites(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cols = _get_restaurants_columns(cur)

        select_cols = [
            "r.id", "r.name", "r.city", "r.address", "r.notes",
            "r.rating", "r.lat", "r.lon", "r.last_update"
        ]
        if "types" in cols:
            select_cols.append("r.types")
        if "phone" in cols:
            select_cols.append("r.phone")

        sql = f"""
            SELECT {",".join(select_cols)}
            FROM favorites f
            JOIN restaurants r ON r.id = f.restaurant_id
            WHERE f.user_id = ?
            ORDER BY f.created_at DESC
        """
        cur.execute(sql, (user_id,))
        return cur.fetchall()


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


def get_photos_for_restaurant(restaurant_id: int):
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
        return [r[0] for r in cur.fetchall()]


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
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_disclaimer(phone: Optional[str]) -> str:
    disclaimer = (
        "\n\nℹ️ <b>Nota importante</b>\n"
        "Questo ristorante è mostrato in base a recensioni e informazioni pubbliche disponibili online.\n"
        "Le condizioni per il senza glutine possono variare nel tempo (cambi di gestione, menu o procedure).\n\n"
        "👉 Ti consigliamo sempre di contattare direttamente il ristorante prima di andare."
    )

    if phone and str(phone).strip():
        raw_phone = str(phone).strip()

        # rimuovo spazi, trattini, parentesi
        phone_clean = (
            raw_phone
            .replace(" ", "")
            .replace("-", "")
            .replace("(", "")
            .replace(")", "")
        )

        # aggiungo prefisso internazionale se manca
        if not phone_clean.startswith("+") and not phone_clean.startswith("00"):
            phone_clean = "+39" + phone_clean

        call_line = f'\n📞 <a href="tel:{phone_clean}">Chiama il ristorante</a>'

    else:
        call_line = "\n📞 Contatta direttamente il ristorante per conferma"

    return disclaimer + call_line


def format_restaurant_detail(row, user_location=None) -> Tuple[str, int]:
    """
    row minimo:
    id, name, city, address, notes, rating, lat, lon, last_update
    opzionali: types, phone (o phone, types)
    """
    # base 9 campi
    rid = row[0]
    name = row[1]
    city = row[2]
    address = row[3]
    notes = row[4]
    rating = row[5]
    lat = row[6]
    lon = row[7]
    last_update = row[8]

    # estrai phone se presente (in coda)
    phone = None
    if len(row) >= 10:
        # può essere types o phone in posizione 9
        # se contiene numeri/+ è probabile phone, altrimenti types
        maybe = row[9]
        if maybe and any(ch.isdigit() for ch in str(maybe)) or (maybe and str(maybe).strip().startswith("+")):
            phone = maybe
        # se c'è 11° campo, quello sarà l'altro
        if len(row) >= 11:
            maybe2 = row[10]
            if maybe2 and (any(ch.isdigit() for ch in str(maybe2)) or str(maybe2).strip().startswith("+")):
                phone = maybe2

    rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."

    distance_str = ""
    if user_location and lat is not None and lon is not None:
        d = haversine_km(user_location[0], user_location[1], lat, lon)
        if d is not None:
            distance_str = f"\n📏 Distanza: {d*1000:.0f} m" if d < 1 else f"\n📏 Distanza: {d:.1f} km"

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"
    disc = build_disclaimer(phone)

    update_str = f"\n🕒 Aggiornato: {last_update}" if last_update else ""

    text = (
        f"🍽 <b>{name}</b>\n"
        f"📍 <b>{city}</b> – {address or 'Indirizzo non disponibile'}\n"
        f"⭐ Rating medio Google: {rating_str}{update_str}"
        f"{distance_str}\n\n"
        f"<b>Note:</b> {notes or '—'}"
        f"{disc}\n"
        f"\n🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )
    return text, rid


# ==========================
# QUERY RISTORANTI (compatibili)
# ==========================

def _select_restaurant_columns(cur) -> List[str]:
    cols = _get_restaurants_columns(cur)
    base = ["id","name","city","address","notes","rating","lat","lon","last_update"]
    # aggiungo optionali se esistono
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
        select_cols = _select_restaurant_columns(cur)
        sql = f"""
            SELECT {",".join(select_cols)}
            FROM restaurants
            WHERE LOWER(city) = LOWER(?)
            ORDER BY (rating IS NULL) ASC, rating DESC, name ASC
        """
        cur.execute(sql, (city,))
        rows = cur.fetchall()

    if min_rating is not None:
        rows = [r for r in rows if (r[5] is None or r[5] >= min_rating)]
    return rows


def query_nearby(lat: float, lon: float, user_id: int, max_distance_km: float, max_results: int = 200):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        select_cols = _select_restaurant_columns(cur)
        cur.execute(
            f"""
            SELECT {",".join(select_cols)}
            FROM restaurants
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            """
        )
        rows = cur.fetchall()

    enriched = []
    for r in rows:
        rlat = r[6]
        rlon = r[7]
        d = haversine_km(lat, lon, rlat, rlon)
        if d is None:
            continue
        if d > max_distance_km:
            continue
        rating = r[5]
        if min_rating is not None and rating is not None and rating < min_rating:
            continue
        enriched.append((d, r))

    enriched.sort(key=lambda x: x[0])
    return [e[1] for e in enriched[:max_results]]


# ==========================
# LISTE PAGINATE
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

    lines = [
        f"🔎 Ho trovato <b>{total}</b> locali a <b>{city}</b> — pagina {page+1}/{total_pages}:\n"
    ]
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

    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


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

    lines = [
        f"📍 Locali entro <b>{radius_km} km</b> — trovati <b>{total}</b> (pagina {page+1}/{total_pages}):\n"
    ]
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
    lat_str = f"{lat:.5f}"
    lon_str = f"{lon:.5f}"
    rad_str = f"{radius_km:.2f}"
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"nearpage:{lat_str}:{lon_str}:{rad_str}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"nearpage:{lat_str}:{lon_str}:{rad_str}:{page+1}"))
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
            ["⭐ I miei preferiti", "🛒 Shop"],
            ["⚙️ Filtri"],
        ],
        resize_keyboard=True,
    )


# ==========================
# HANDLERS
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "start")

    msg = (
        "Ciao 👋 benvenuto in <b>GlutenFreeBot</b> 🧡\n\n"
        "Qui trovi ristoranti e locali pensati per chi vive davvero <b>senza glutine</b>.\n\n"
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
        disable_web_page_preview=True,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    if context.user_data.get("awaiting_city_search"):
        context.user_data["awaiting_city_search"] = False
        city = text.strip()
        log_usage(user.id, f"search_city:{city}")

        page_text, kb = build_city_page(user.id, city, page=0)
        if page_text is None:
            await update.message.reply_text(
                f"😔 Al momento non ho locali per <b>{city}</b> nel database.\n"
                "Se vuoi, scrivimi comunque questa città e cercheremo di aggiornarla presto.",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
            return

        await update.message.reply_text(page_text, parse_mode="HTML", reply_markup=kb)
        await update.message.reply_text("Puoi continuare dal menu 👇", reply_markup=main_keyboard())
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

    if text == "🔍 Cerca per città":
        context.user_data["awaiting_city_search"] = True
        await update.message.reply_text("Scrivi il nome della città (es: Bari):", reply_markup=main_keyboard())
        return

    if text == "📍 Vicino a me":
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
        favs = get_favorites(user.id)
        if not favs:
            await update.message.reply_text("Non hai ancora preferiti ⭐", reply_markup=main_keyboard())
            return

        await update.message.reply_text(f"Hai <b>{len(favs)}</b> preferiti:", parse_mode="HTML", reply_markup=main_keyboard())
        # li mostro in lista paginata come per città (semplice: primi 5)
        for r in favs[:5]:
            detail_text, rid = format_restaurant_detail(r)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")]])
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
        await update.message.reply_text(
            f"Rating minimo attuale: <b>{current}</b>\nScegli un'impostazione:",
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

    await update.message.reply_text("Usa i pulsanti qui sotto 👇", reply_markup=main_keyboard())


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_usage(user.id, "location_sent")

    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude
    radius = float(context.user_data.get("nearby_radius_km", 5.0))

    page_text, kb = build_nearby_page(user.id, lat, lon, radius_km=radius, page=0)
    if page_text is None:
        await update.message.reply_text(
            f"😔 Nessun locale trovato entro {radius:g} km.\n"
            "Scrivimi la città e proveremo ad aggiornarla presto.",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text(page_text, parse_mode="HTML", reply_markup=kb)
    await update.message.reply_text("Puoi continuare dal menu 👇", reply_markup=main_keyboard())


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
    log_usage(user.id, f"add_photo:{rid}")

    await update.message.reply_text("📷 Foto salvata, grazie!", reply_markup=main_keyboard())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    await query.answer()

    if data.startswith("page:"):
        _, city, page_s = data.split(":", 2)
        page = int(page_s)
        txt, kb = build_city_page(user.id, city, page=page)
        if txt:
            await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("nearpage:"):
        _, lat_s, lon_s, rad_s, page_s = data.split(":", 4)
        lat = float(lat_s)
        lon = float(lon_s)
        radius = float(rad_s)
        page = int(page_s)
        txt, kb = build_nearby_page(user.id, lat, lon, radius_km=radius, page=page)
        if txt:
            await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("details:"):
        rid = int(data.split(":", 1)[1])
        log_usage(user.id, f"details_click:{rid}")

        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cols = _get_restaurants_columns(cur)
            select_cols = ["id","name","city","address","notes","rating","lat","lon","last_update"]
            if "types" in cols:
                select_cols.append("types")
            if "phone" in cols:
                select_cols.append("phone")
            cur.execute(f"SELECT {','.join(select_cols)} FROM restaurants WHERE id = ?", (rid,))
            row = cur.fetchone()

        if not row:
            await query.message.reply_text("Locale non trovato.")
            return

        detail_text, rid = format_restaurant_detail(row)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}")],
                [InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")],
            ]
        )

        await query.message.reply_text(detail_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

        # manda foto community se presente
        photos = get_photos_for_restaurant(rid)
        if photos:
            await query.message.reply_photo(photos[0], caption="📷 Foto dalla community")
        return

    if data.startswith("fav:"):
        rid = int(data.split(":", 1)[1])
        add_favorite(user.id, rid)
        log_usage(user.id, f"fav:{rid}")
        await query.message.reply_text("⭐ Aggiunto ai preferiti.", reply_markup=main_keyboard())
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
            try:
                set_user_min_rating(user.id, float(val))
                await query.message.reply_text(f"Rating minimo impostato a {val}⭐.", reply_markup=main_keyboard())
            except Exception:
                await query.message.reply_text("Valore non valido.", reply_markup=main_keyboard())
        return


# ==========================
# MAIN
# ==========================

def build_application():
    ensure_schema()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(callback_handler))

    return app


if __name__ == "__main__":
    print("🔄 Importo ristoranti da app_restaurants.csv...")
    try:
        import_app_restaurants()
        print("✅ Import completato.")
    except Exception as e:
        print("⚠️ Errore durante l'import:", e)

    application = build_application()
    print("🤖 GlutenFreeBot avviato...")
    application.run_polling()
