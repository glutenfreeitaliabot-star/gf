import math
import sqlite3
import os
from contextlib import closing
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any

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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # mettila su Railway
DB_PATH = "restaurants.db"
PAGE_SIZE = 5

pending_photo_for_user: Dict[int, int] = {}


# ==========================
# DB
# ==========================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    return conn


def _table_cols(cur, table: str) -> set:
    try:
        cur.execute(f"PRAGMA table_info({table})")
        return {r[1].lower() for r in cur.fetchall()}
    except Exception:
        return set()


def ensure_schema():
    """
    Non ricrea restaurants (così non distruggiamo dati).
    Crea tabelle extra e prova ad aggiungere colonne mancanti.
    """
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            user_id INTEGER NOT NULL,
            restaurant_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, restaurant_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            min_rating REAL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event TEXT,
            city TEXT,
            created_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS suggested_cities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            city TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        # best effort: aggiungi colonne a restaurants se esiste
        cols = _table_cols(cur, "restaurants")
        if cols:
            try:
                if "phone" not in cols:
                    cur.execute("ALTER TABLE restaurants ADD COLUMN phone TEXT")
            except Exception:
                pass
            try:
                if "types" not in cols:
                    cur.execute("ALTER TABLE restaurants ADD COLUMN types TEXT")
            except Exception:
                pass
            try:
                if "last_update" not in cols:
                    cur.execute("ALTER TABLE restaurants ADD COLUMN last_update TEXT")
            except Exception:
                pass

        conn.commit()


def log_usage(user_id: int, event: str, city: Optional[str] = None):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usage_events (user_id, event, city, created_at) VALUES (?, ?, ?, ?)",
            (user_id, event, city, datetime.utcnow().isoformat()),
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
            SELECT file_id FROM photos
            WHERE restaurant_id = ?
            ORDER BY created_at DESC
            LIMIT 3
            """,
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
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def normalize_phone_for_tel(phone: str) -> Optional[str]:
    if not phone:
        return None
    p = str(phone).strip()
    if not p:
        return None

    # tieni + e cifre
    cleaned = []
    for ch in p:
        if ch.isdigit() or ch == "+":
            cleaned.append(ch)
    p2 = "".join(cleaned)

    # se resta solo "+" o niente
    if p2 in ("", "+"):
        return None

    # se non ha prefisso internazionale, assumiamo Italia
    if not p2.startswith("+") and not p2.startswith("00"):
        p2 = "+39" + p2

    # converti 00xx -> +xx
    if p2.startswith("00"):
        p2 = "+" + p2[2:]

    return p2


def restaurant_disclaimer() -> str:
    return (
        "\n\nℹ️ <b>Nota importante</b>\n"
        "Questo ristorante è mostrato in base a recensioni e informazioni pubbliche disponibili online.\n"
        "Le condizioni per il senza glutine possono variare nel tempo (cambi di gestione, menu o procedure).\n\n"
        "👉 Ti consigliamo sempre di contattare direttamente il ristorante prima di andare."
    )


def build_restaurant_detail_text(r: Dict[str, Any], user_location=None) -> str:
    name = r.get("name") or ""
    city = r.get("city") or ""
    address = r.get("address") or "Indirizzo non disponibile"
    notes = r.get("notes") or "—"
    rating = r.get("rating")
    last_update = r.get("last_update")
    lat = r.get("lat")
    lon = r.get("lon")
    phone = r.get("phone")

    rating_str = f"{float(rating):.1f}⭐" if rating is not None else "n.d."
    update_str = f" (aggiornato: {last_update})" if last_update else ""

    distance_str = ""
    if user_location and lat is not None and lon is not None:
        d = haversine_km(user_location[0], user_location[1], lat, lon)
        if d is not None:
            distance_str = f"\n📏 Distanza: {d*1000:.0f} m" if d < 1 else f"\n📏 Distanza: {d:.1f} km"

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"

    phone_line = ""
    if phone and str(phone).strip():
        phone_line = f"\n📞 Telefono: <b>{phone}</b>"
    else:
        phone_line = "\n📞 Telefono: <b>non disponibile</b>"

    text = (
        f"🍽 <b>{name}</b>\n"
        f"📍 <b>{city}</b> – {address}\n"
        f"⭐ Rating medio Google: {rating_str}{update_str}"
        f"{distance_str}\n"
        f"{phone_line}\n\n"
        f"<b>Note:</b> {notes}"
        f"{restaurant_disclaimer()}\n"
        f"\n🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )
    return text


# ==========================
# QUERY (compatibili con colonne variabili)
# ==========================

def select_restaurants_cols(cur) -> List[str]:
    cols = _table_cols(cur, "restaurants")
    base = ["id", "name", "city", "address", "notes", "rating", "lat", "lon", "last_update"]
    if "types" in cols:
        base.append("types")
    if "phone" in cols:
        base.append("phone")
    return base


def fetch_restaurant_by_id(rid: int) -> Optional[Dict[str, Any]]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        sel = select_restaurants_cols(cur)
        cur.execute(f"SELECT {','.join(sel)} FROM restaurants WHERE id = ?", (rid,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip(sel, row))


def query_by_city(city: str, user_id: int) -> List[Dict[str, Any]]:
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        sel = select_restaurants_cols(cur)
        cur.execute(
            f"""
            SELECT {','.join(sel)}
            FROM restaurants
            WHERE LOWER(city) = LOWER(?)
            ORDER BY (rating IS NULL) ASC, rating DESC, name ASC
            """,
            (city,),
        )
        rows = [dict(zip(sel, r)) for r in cur.fetchall()]

    if min_rating is not None:
        rows = [r for r in rows if (r.get("rating") is None or float(r.get("rating")) >= float(min_rating))]
    return rows


def query_nearby(lat: float, lon: float, user_id: int, radius_km: float, max_results: int = 200) -> List[Dict[str, Any]]:
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        sel = select_restaurants_cols(cur)
        cur.execute(
            f"""
            SELECT {','.join(sel)}
            FROM restaurants
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            """
        )
        raw = [dict(zip(sel, r)) for r in cur.fetchall()]

    enriched: List[Tuple[float, Dict[str, Any]]] = []
    for r in raw:
        d = haversine_km(lat, lon, r.get("lat"), r.get("lon"))
        if d is None or d > radius_km:
            continue
        if min_rating is not None and r.get("rating") is not None and float(r.get("rating")) < float(min_rating):
            continue
        enriched.append((d, r))

    enriched.sort(key=lambda x: x[0])
    return [e[1] for e in enriched[:max_results]]


# ==========================
# LIST PAGES
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
        rid = r["id"]
        name = r.get("name") or "Senza nome"
        rating = r.get("rating")
        rating_str = f"{float(rating):.1f}⭐" if rating is not None else "n.d."
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

    for i, r in enumerate(subset, start=start + 1):
        rid = r["id"]
        name = r.get("name") or "Senza nome"
        city = r.get("city") or "—"
        rating = r.get("rating")
        rating_str = f"{float(rating):.1f}⭐" if rating is not None else "n.d."
        d = haversine_km(lat, lon, r.get("lat"), r.get("lon"))
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

    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


# ==========================
# UI
# ==========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["🛒 Shop", "⚙️ Filtri"],
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

    # attesa città
    if context.user_data.get("awaiting_city_search"):
        context.user_data["awaiting_city_search"] = False
        city = text.strip()
        log_usage(user.id, "search_city", city=city)

        page_text, kb = build_city_page(user.id, city, page=0)
        if page_text is None:
            kb2 = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📩 Suggerisci questa città", callback_data=f"suggestcity:{city}")]]
            )
            await update.message.reply_text(
                f"😔 Al momento non ho locali per <b>{city}</b>.\n\nVuoi segnalarla? La mettiamo in coda per l'aggiornamento.",
                parse_mode="HTML",
                reply_markup=kb2,
            )
            await update.message.reply_text("Puoi continuare dal menu 👇", reply_markup=main_keyboard())
            return

        await update.message.reply_text(page_text, parse_mode="HTML", reply_markup=kb)
        await update.message.reply_text("Puoi continuare dal menu 👇", reply_markup=main_keyboard())
        return

    # scelta raggio (già ok, non tocchiamo la logica)
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

    await update.message.reply_text("Usa i pulsanti qui sotto 👇", reply_markup=main_keyboard())


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude
    radius = float(context.user_data.get("nearby_radius_km", 5.0))

    # Nota: qui NON abbiamo la città precisa. La userai nelle /stats col reverse geocode se vuoi.
    log_usage(user.id, "search_nearby", city=None)

    page_text, kb = build_nearby_page(user.id, lat, lon, radius_km=radius, page=0)
    if page_text is None:
        await update.message.reply_text(
            f"😔 Nessun locale trovato entro {radius:g} km.\n\n"
            "Vuoi segnalarci la città/area? Scrivi il nome città e la mettiamo in coda.",
            reply_markup=main_keyboard(),
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
        log_usage(user.id, "details_click", city=None)

        r = fetch_restaurant_by_id(rid)
        if not r:
            await query.message.reply_text("Locale non trovato.")
            return

        detail_text = build_restaurant_detail_text(r)

        # Bottone CHIAMA (più affidabile del link HTML)
        phone = r.get("phone")
        tel = normalize_phone_for_tel(phone) if phone else None

        kb_rows = [
            [InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}")],
            [InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")],
        ]
        if tel:
            kb_rows.insert(0, [InlineKeyboardButton("📞 Chiama il ristorante", url=f"tel:{tel}")])

        kb = InlineKeyboardMarkup(kb_rows)

        await query.message.reply_text(
            detail_text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

        photos = get_photos_for_restaurant(rid)
        if photos:
            await query.message.reply_photo(photos[0], caption="📷 Foto dalla community")
        return

    if data.startswith("fav:"):
        rid = int(data.split(":", 1)[1])
        add_favorite(user.id, rid)
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
            set_user_min_rating(user.id, float(val))
            await query.message.reply_text(f"Rating minimo impostato a {val}⭐.", reply_markup=main_keyboard())
        return

    if data.startswith("suggestcity:"):
        city = data.split(":", 1)[1].strip()
        if not city:
            await query.message.reply_text("Città non valida.")
            return

        save_suggested_city(user.id, city)
        await query.message.reply_text("✅ Segnalazione inviata! La mettiamo in coda per l'aggiornamento.", reply_markup=main_keyboard())

        # Notifica admin
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
            "Per collegare una foto ad un locale, prima apri i dettagli e premi '📷 Aggiungi foto'.",
            reply_markup=main_keyboard(),
        )
        return

    rid = pending_photo_for_user.pop(user.id)
    photo = update.message.photo[-1]
    add_photo_record(user.id, rid, photo.file_id)
    await update.message.reply_text("📷 Foto salvata, grazie!", reply_markup=main_keyboard())


# ==========================
# MAIN
# ==========================

def build_application():
    ensure_schema()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))

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
        print("⚠️ Errore durante l'import:", e)

    application = build_application()
    print("🤖 GlutenFreeBot avviato...")
    application.run_polling()
