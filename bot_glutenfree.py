import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Optional, List

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
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

DB_PATH = "restaurants.db"
PAGE_SIZE = 5  # numero di ristoranti per pagina nelle liste
SCHEMA_VERSION = "v2-paginazione-suggested-shop"

# Stati per ConversationHandler "aggiungi ristorante"
ADD_NAME, ADD_CITY, ADD_ADDRESS, ADD_NOTES = range(4)

# Memoria in RAM per gestire "aggiungi foto dopo"
pending_photo_for_user = {}  # {user_id: restaurant_id}


# ==========================
# SHOP (Amazon affiliate)
# ==========================

SHOP_CATEGORIES = [
    {
        "id": "farine_mix",
        "name": "🌾 Farine & Mix",
        "description": "Mix per pane, pizza, dolci e farine naturali senza glutine.",
        "items": [
            {
                "name": "Mix pane/pizza senza glutine",
                "badge": "Best seller",
                "url": "https://www.amazon.it/INSERISCI_TUO_LINK1",  # TODO
            },
            {
                "name": "Farina di riso fine",
                "badge": "Base dispensa",
                "url": "https://www.amazon.it/INSERISCI_TUO_LINK2",
            },
        ],
    },
    {
        "id": "snack",
        "name": "🍪 Snack & Merendine",
        "description": "Snack veloci, barrette, biscotti e merendine gluten free.",
        "items": [
            {
                "name": "Barrette senza glutine",
                "badge": "Per l'ufficio",
                "url": "https://www.amazon.it/INSERISCI_TUO_LINK3",
            },
            {
                "name": "Biscotti senza glutine",
                "badge": "Top colazione",
                "url": "https://www.amazon.it/INSERISCI_TUO_LINK4",
            },
        ],
    },
    {
        "id": "pane_piadine",
        "name": "🥖 Pane, Piadine & Base Pizza",
        "description": "Prodotti pronti per panini, piadine e pizze veloci.",
        "items": [
            {
                "name": "Piadine senza glutine",
                "badge": "Sempre in frigo",
                "url": "https://www.amazon.it/INSERISCI_TUO_LINK5",
            },
        ],
    },
    {
        "id": "kit",
        "name": "🎁 Kit prova & Starter Pack",
        "description": "Box misti per chi vuole provare tanti prodotti diversi.",
        "items": [
            {
                "name": "Box assaggio senza glutine",
                "badge": "Idea regalo",
                "url": "https://www.amazon.it/INSERISCI_TUO_LINK6",
            },
        ],
    },
]


# ==========================
# UTILS DB
# ==========================

def get_conn():
    return sqlite3.connect(DB_PATH)


def ensure_schema():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # Tabella restaurants
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
                last_update TEXT
            )
            """
        )

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

        # Città seguite (per novità)
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

        # Impostazioni utente (filtri)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                min_rating REAL
            )
            """
        )

        # Segnalazioni / errori
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

        # Foto dei ristoranti
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

        # Gamification
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY,
                points INTEGER NOT NULL DEFAULT 0,
                title TEXT
            )
            """
        )

        # Suggerimenti ristoranti dagli utenti (da approvare)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS suggested_restaurants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                city TEXT NOT NULL,
                address TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new'
            )
            """
        )

        # Recensioni strutturate (futuro)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS restaurant_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                restaurant_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                review_url TEXT,
                review_date TEXT,
                gluten_score REAL,
                taste_score REAL,
                service_score REAL,
                price_score REAL,
                overall_score REAL,
                gluten_comment TEXT,
                general_comment TEXT,
                created_by_user_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
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

        cur.execute(
            "UPDATE user_stats SET title = ? WHERE user_id = ?",
            (title, user_id),
        )
        conn.commit()


def get_user_stats(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT points, title FROM user_stats WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return 0, "👤 Utente"
        return row[0], row[1]


def get_user_settings(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT min_rating FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
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
            "INSERT OR IGNORE INTO favorites (user_id, restaurant_id, created_at) "
            "VALUES (?, ?, ?)",
            (user_id, restaurant_id, datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_favorites(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT r.id, r.name, r.city, r.address, r.notes, r.rating, r.lat, r.lon
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
            """
            INSERT OR IGNORE INTO subscriptions (user_id, city, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id, city, datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_subscriptions(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT city FROM subscriptions WHERE user_id = ? ORDER BY city",
            (user_id,),
        )
        return [row[0] for row in cur.fetchall()]


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
        return [row[0] for row in cur.fetchall()]


def add_suggested_restaurant(user_id: int, name: str, city: str, address: str, notes: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO suggested_restaurants
                (user_id, name, city, address, notes, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'new')
            """,
            (user_id, name, city, address, notes, datetime.utcnow().isoformat()),
        )
        conn.commit()


# ==========================
# LOGICA RISTORANTI
# ==========================

def eval_risk(notes: str) -> str:
    if not notes:
        return "⚪️ Info non sufficiente"

    text = notes.lower()

    high_keys = [
        "contaminazione",
        "tracce di glutine",
        "non garantito",
        "stesso forno",
        "stessa friggitrice",
    ]
    safe_keys = [
        "no contaminazione",
        "senza contaminazione",
        "cucina separata",
        "forno dedicato",
        "aic",
        "certificato",
    ]

    if any(k in text for k in safe_keys):
        return "🟢 Attenzione alta al senza glutine"
    if any(k in text for k in high_keys):
        return "🟠 Possibile contaminazione, chiedi bene al locale"
    return "🟡 Verifica sul posto, info non chiara"


def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None

    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(
        dlambda / 2
    ) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def encode_city(city: str) -> str:
    return city.replace(" ", "_")


def decode_city(s: str) -> str:
    return s.replace("_", " ")


def query_by_city(city: str, user_id: int):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        sql = """
        SELECT id, name, city, address, notes, rating, lat, lon, last_update
        FROM restaurants
        WHERE LOWER(city) = LOWER(?)
        ORDER BY rating DESC, name ASC
        """
        cur.execute(sql, (city,))
        rows = cur.fetchall()

    if min_rating is not None:
        rows = [r for r in rows if (r[5] is None or r[5] >= min_rating)]

    return rows


def query_nearby(lat: float, lon: float, user_id: int, max_results: int = 50):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, city, address, notes, rating, lat, lon, last_update
            FROM restaurants
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            """
        )
        rows = cur.fetchall()

    enriched = []
    for r in rows:
        dist = haversine_km(lat, lon, r[6], r[7])
        enriched.append((dist, r))

    enriched = [e for e in enriched if e[0] is not None]
    enriched.sort(key=lambda x: x[0])

    if min_rating is not None:
        enriched = [e for e in enriched if (e[1][5] is None or e[1][5] >= min_rating)]

    enriched = enriched[:max_results]

    return [e[1] for e in enriched]


def query_recent_in_cities(cities: List[str], limit: int = 50):
    if not cities:
        return []

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(cities))
        sql = f"""
        SELECT id, name, city, address, notes, rating, lat, lon, last_update
        FROM restaurants
        WHERE city IN ({placeholders})
        ORDER BY last_update DESC
        LIMIT ?
        """
        cur.execute(sql, (*cities, limit))
        return cur.fetchall()


def build_city_page(user_id: int, city: str, page: int):
    rows = query_by_city(city, user_id)
    if not rows:
        return None, None

    total = len(rows)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_rows = rows[start:end]

    lines = [
        f"Ho trovato <b>{total}</b> ristoranti per <b>{city}</b> (pagina {page+1}/{total_pages}):",
        "",
    ]

    for idx, r in enumerate(page_rows, start=start + 1):
        rid, name, city_r, address, notes, rating, lat_r, lon_r, last_update = r
        rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."
        lines.append(f"{idx}. {name} – {rating_str}")

    lines.append("")
    lines.append("👇 Tocca un pulsante per i dettagli di un ristorante.")

    text = "\n".join(lines)

    keyboard_rows = []
    for idx, r in enumerate(page_rows, start=start + 1):
        rid = r[0]
        keyboard_rows.append(
            [InlineKeyboardButton(f"Dettagli {idx}", callback_data=f"details:{rid}")]
        )

    nav_row = []
    enc_city = encode_city(city)
    if total_pages > 1:
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    "⬅️ Indietro", callback_data=f"page:{enc_city}:{page-1}"
                )
            )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    "➡️ Avanti", callback_data=f"page:{enc_city}:{page+1}"
                )
            )
    if nav_row:
        keyboard_rows.append(nav_row)

    keyboard_rows.append(
        [InlineKeyboardButton(f"🔔 Segui {city}", callback_data=f"subcity:{city}")]
    )

    kb = InlineKeyboardMarkup(keyboard_rows)
    return text, kb


def build_nearby_page(user_id: int, lat: float, lon: float, page: int):
    rows = query_nearby(lat, lon, user_id, max_results=None)
    if not rows:
        return None, None

    total = len(rows)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_rows = rows[start:end]

    lines = [
        f"Ho trovato <b>{total}</b> ristoranti vicino a te (pagina {page+1}/{total_pages}):",
        "",
    ]

    for idx, r in enumerate(page_rows, start=start + 1):
        rid, name, city_r, address, notes, rating, lat_r, lon_r, last_update = r
        rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."
        dist = haversine_km(lat, lon, lat_r, lon_r)
        if dist is not None:
            dist_str = f"{dist*1000:.0f} m" if dist < 1 else f"{dist:.1f} km"
        else:
            dist_str = "n.d."
        lines.append(f"{idx}. {name} – {rating_str} – {dist_str}")

    lines.append("")
    lines.append("👇 Tocca un pulsante per i dettagli di un ristorante.")

    text = "\n".join(lines)

    keyboard_rows = []
    for idx, r in enumerate(page_rows, start=start + 1):
        rid = r[0]
        keyboard_rows.append(
            [InlineKeyboardButton(f"Dettagli {idx}", callback_data=f"details:{rid}")]
        )

    nav_row = []
    lat_str = f"{lat:.5f}"
    lon_str = f"{lon:.5f}"
    if total_pages > 1:
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    "⬅️ Indietro",
                    callback_data=f"nearpage:{lat_str}:{lon_str}:{page-1}",
                )
            )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    "➡️ Avanti",
                    callback_data=f"nearpage:{lat_str}:{lon_str}:{page+1}",
                )
            )
    if nav_row:
        keyboard_rows.append(nav_row)

    kb = InlineKeyboardMarkup(keyboard_rows)
    return text, kb


def format_restaurant_row(row, user_location=None):
    rid, name, city, address, notes, rating, lat, lon, last_update = row

    risk = eval_risk(notes or "")

    distance_str = ""
    if user_location and lat is not None and lon is not None:
        dist = haversine_km(user_location[0], user_location[1], lat, lon)
        if dist is not None:
            if dist < 1:
                distance_str = f"\n📏 Distanza: {dist*1000:.0f} m"
            else:
                distance_str = f"\n📏 Distanza: {dist:.1f} km"

    rating_str = f"{rating:.1f}⭐" if rating is not None else "n.d."
    update_str = f" (aggiornato: {last_update})" if last_update else ""

    maps_url = f"https://www.google.com/maps/search/?api=1&query={name.replace(' ', '+')}+{city.replace(' ', '+')}"

    text = (
        f"🍽 <b>{name}</b>\n"
        f"📍 <b>{city}</b> – {address or 'Indirizzo non disponibile'}\n"
        f"⭐ Rating medio Google: {rating_str}{update_str}\n"
        f"{distance_str}\n"
        f"\n<b>Note:</b> {notes or '—'}\n"
        f"\n<b>Rischio contaminazione:</b> {risk}\n"
        f"\n🌍 <a href=\"{maps_url}\">Apri in Google Maps</a>"
    )

    return text, rid


# ==========================
# HANDLER BOT
# ==========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["➕ Aggiungi ristorante", "⭐ I miei preferiti"],
            ["🛒 Shop", "🔔 Novità città seguite"],
            ["⚙️ Filtri"],
        ],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    points, title = get_user_stats(user.id)
    msg = (
        f"Ciao {user.first_name or ''}!\n\n"
        f"Benvenuto in <b>GlutenFreeBot</b> 🧡\n\n"
        f"Ti aiuto a trovare ristoranti con recensioni che citano "
        f"glutine / senza glutine / gluten free.\n\n"
        f"Il tuo profilo:\n"
        f"• Punti: <b>{points}</b>\n"
        f"• Titolo: <b>{title}</b>\n\n"
        f"Usa i pulsanti qui sotto per iniziare."
    )
    await update.message.reply_text(msg, reply_markup=main_keyboard(), parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Comandi principali:\n"
        "• /start – mostra il menu\n"
        "• Cerca per città – cerca ristoranti gluten-friendly in una città\n"
        "• Vicino a me – invia la posizione per vedere i locali vicini\n"
        "• Aggiungi ristorante – segnala un locale che conosci\n"
        "• I miei preferiti – ristoranti che hai salvato ⭐\n"
        "• Filtri – imposta rating minimo\n"
        "• Novità città seguite – locali nuovi nelle città che segui\n"
        "• Shop – prodotti senza glutine consigliati\n"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())


async def show_shop_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "🛒 <b>GlutenFree Shop</b>",
        "",
        "Qui trovi prodotti senza glutine selezionati da Antonio.",
        "Gli acquisti passano da Amazon e aiutano a sostenere il progetto.",
        "",
        "Scegli una categoria:",
    ]

    keyboard_rows = []
    for cat in SHOP_CATEGORIES:
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    cat["name"], callback_data=f"shopcat:{cat['id']}"
                )
            ]
        )

    kb = InlineKeyboardMarkup(keyboard_rows)

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "🔍 Cerca per città":
        await update.message.reply_text(
            "Scrivimi il nome della città (es: 'Bari').",
            reply_markup=main_keyboard(),
        )
        context.user_data["awaiting_city_search"] = True
        return

    if context.user_data.get("awaiting_city_search"):
        context.user_data["awaiting_city_search"] = False
        await search_city(update, context, text)
        return

    if text == "📍 Vicino a me":
        await update.message.reply_text(
            "Invia la tua posizione usando il tasto apposito.",
            reply_markup=ReplyKeyboardMarkup(
                [
                    [
                        KeyboardButton(
                            "Invia posizione 📍", request_location=True
                        )
                    ]
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return

    if text == "➕ Aggiungi ristorante":
        return await add_restaurant_start(update, context)

    if text == "⭐ I miei preferiti":
        return await my_favorites(update, context)

    if text == "⚙️ Filtri":
        return await show_filters(update, context)

    if text == "🔔 Novità città seguite":
        return await show_news(update, context)

    if text == "🛒 Shop":
        return await show_shop_menu(update, context)

    if text == "Invia posizione 📍":
        await update.message.reply_text(
            "Usa il bottone posizione di Telegram per mandarmi la geolocalizzazione."
        )
        return

    await update.message.reply_text(
        "Non ho capito il comando. Usa /start o i pulsanti sotto.",
        reply_markup=main_keyboard(),
    )


async def search_city(
    update: Update, context: ContextTypes.DEFAULT_TYPE, city_text: str
):
    user = update.effective_user
    city = city_text.strip()
    if not city:
        await update.message.reply_text("Inserisci un nome città valido.")
        return

    text, kb = build_city_page(user.id, city, page=0)
    if text is None:
        await update.message.reply_text(
            f"Al momento non ho ristoranti gluten-friendly per <b>{city}</b>.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude

    text, kb = build_nearby_page(user.id, lat, lon, page=0)
    if text is None:
        await update.message.reply_text(
            "Al momento non ho ristoranti con coordinate vicino a te.",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )

    await update.message.reply_text(
        "Puoi usare di nuovo il menu qui sotto 👇",
        reply_markup=main_keyboard(),
    )


async def my_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    favs = get_favorites(user.id)
    if not favs:
        await update.message.reply_text(
            "Non hai ancora nessun ristorante nei preferiti ⭐.\n"
            "Quando vedi un locale interessante, usa il bottone '⭐ Preferito'.",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text(
        f"Hai <b>{len(favs)}</b> ristoranti nei preferiti:",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    for r in favs[:15]:
        rid, name, city, address, notes, rating, lat, lon = r
        row_full = (rid, name, city, address, notes, rating, lat, lon, None)
        text, _ = format_restaurant_row(row_full)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⚠️ Segnala", callback_data=f"rep:{rid}"),
                    InlineKeyboardButton(
                        "📷 Aggiungi foto", callback_data=f"photo:{rid}"
                    ),
                ]
            ]
        )
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        photos = get_photos_for_restaurant(rid)
        if photos:
            await update.message.reply_photo(
                photos[0],
                caption="📷 Foto dalla community",
            )


async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    settings = get_user_settings(user.id)
    min_rating = settings.get("min_rating")
    current = f"{min_rating:.1f}" if min_rating is not None else "nessuno"

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⭐ ≥ 4.0", callback_data="filt:4.0"),
                InlineKeyboardButton("⭐ ≥ 4.5", callback_data="filt:4.5"),
            ],
            [
                InlineKeyboardButton(
                    "❌ Nessun filtro rating", callback_data="filt:none"
                )
            ],
        ]
    )

    await update.message.reply_text(
        f"Filtri attuali:\n• Rating minimo: <b>{current}</b>\n\n"
        "Scegli un'impostazione:",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def show_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    subs = get_subscriptions(user.id)
    if not subs:
        await update.message.reply_text(
            "Non segui ancora nessuna città.\n"
            "Quando fai una ricerca per città, usa il tasto '🔔 Segui'.",
            reply_markup=main_keyboard(),
        )
        return

    rows = query_recent_in_cities(subs)
    if not rows:
        await update.message.reply_text(
            "Non ho novità recenti nelle città che segui.",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text(
        "Ecco alcuni locali aggiornati di recente nelle città che segui:",
        reply_markup=main_keyboard(),
    )

    for r in rows[:10]:
        text, rid = format_restaurant_row(r)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⭐ Preferito", callback_data=f"fav:{rid}"
                    ),
                    InlineKeyboardButton(
                        "⚠️ Segnala", callback_data=f"rep:{rid}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📷 Aggiungi foto", callback_data=f"photo:{rid}"
                    )
                ],
            ]
        )
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )


# ---- AGGIUNGI RISTORANTE (SEGNALAZIONE UTENTE) ----

async def add_restaurant_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await update.message.reply_text(
        "Ok, segnaliamo un nuovo ristorante suggerito da te.\n"
        "Come si chiama il locale?",
        reply_markup=main_keyboard(),
    )
    return ADD_NAME


async def add_restaurant_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data["new_rest_name"] = update.message.text.strip()
    await update.message.reply_text("In che città si trova?")
    return ADD_CITY


async def add_restaurant_city(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data["new_rest_city"] = update.message.text.strip()
    await update.message.reply_text("Qual è l'indirizzo?")
    return ADD_ADDRESS


async def add_restaurant_address(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data["new_rest_address"] = update.message.text.strip()
    await update.message.reply_text(
        "Vuoi aggiungere una nota (es. esperienza senza glutine)? Se no, scrivi '-'"
    )
    return ADD_NOTES


async def add_restaurant_notes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    notes = update.message.text.strip()
    if notes == "-":
        notes = ""

    name = context.user_data.get("new_rest_name")
    city = context.user_data.get("new_rest_city")
    address = context.user_data.get("new_rest_address")

    add_suggested_restaurant(user.id, name, city, address, notes)
    add_points(user.id, 2)

    # Notifica admin
    if ADMIN_CHAT_ID:
        try:
            text = (
                "📩 <b>Nuova segnalazione ristorante</b>\n\n"
                f"👤 Utente: {user.full_name} (id: {user.id})\n"
                f"🏷 Nome: <b>{name}</b>\n"
                f"📍 Città: {city}\n"
                f"🗺 Indirizzo: {address or '-'}\n"
                f"📝 Note: {notes or '—'}"
            )
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML"
            )
        except Exception:
            pass

    await update.message.reply_text(
        "Grazie! La tua segnalazione è stata registrata e verrà verificata. 🙌",
        reply_markup=main_keyboard(),
    )

    return ConversationHandler.END


async def add_restaurant_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await update.message.reply_text(
        "Segnalazione ristorante annullata.", reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# ---- CALLBACK INLINE BUTTONS ----

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    # Preferito
    if data.startswith("fav:"):
        rid = int(data.split(":")[1])
        add_favorite(user.id, rid)
        add_points(user.id, 1)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⭐ Aggiunto ai preferiti.")
        return

    # Segnala (report)
    if data.startswith("rep:"):
        rid = int(data.split(":")[1])
        add_report(user.id, rid, "Segnalazione generica dal bot")
        add_points(user.id, 1)
        await query.message.reply_text(
            "⚠️ Segnalazione registrata. Grazie, ci aiuta a migliorare i dati."
        )
        return

    # Foto
    if data.startswith("photo:"):
        rid = int(data.split(":")[1])
        pending_photo_for_user[user.id] = rid
        await query.message.reply_text(
            "📷 Inviami una foto del piatto/menù per questo locale.\n"
            "Quando avrò la foto, la collegherò al ristorante."
        )
        return

    # Filtri rating
    if data.startswith("filt:"):
        val = data.split(":")[1]
        if val == "none":
            set_user_min_rating(user.id, None)
            await query.message.reply_text("Filtri rating disattivati.")
        else:
            min_r = float(val)
            set_user_min_rating(user.id, min_r)
            await query.message.reply_text(
                f"Impostato rating minimo a {min_r:.1f}⭐."
            )
        return

    # Segui città
    if data.startswith("subcity:"):
        city = data.split(":", 1)[1]
        subscribe_city(user.id, city)
        await query.message.reply_text(
            f"🔔 Ora segui la città di <b>{city}</b>.\n"
            f"Usa 'Novità città seguite' per vedere i locali aggiornati.",
            parse_mode="HTML",
        )
        return

    # SHOP: selezione categoria
    if data.startswith("shopcat:"):
        cat_id = data.split(":", 1)[1]
        category = next((c for c in SHOP_CATEGORIES if c["id"] == cat_id), None)
        if not category:
            await query.message.reply_text("Categoria non trovata.")
            return

        lines = [
            f"{category['name']}",
            "",
            category.get("description", ""),
            "",
            "Tocca uno dei prodotti qui sotto per aprirlo su Amazon:",
        ]

        keyboard_rows = []
        for item in category["items"]:
            label = item["name"]
            if item.get("badge"):
                label = f"{item['name']} – {item['badge']}"
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        label,
                        url=item["url"],
                    )
                ]
            )

        kb = InlineKeyboardMarkup(keyboard_rows)

        await query.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        return

    # Paginazione per città
    if data.startswith("page:"):
        _, enc_city, page_str = data.split(":")
        city = decode_city(enc_city)
        page = int(page_str)
        text, kb = build_city_page(user.id, city, page)
        if text:
            await query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        return

    # Paginazione "vicino a me"
    if data.startswith("nearpage:"):
        _, lat_str, lon_str, page_str = data.split(":")
        lat = float(lat_str)
        lon = float(lon_str)
        page = int(page_str)
        text, kb = build_nearby_page(user.id, lat, lon, page)
        if text:
            await query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        return

    # Dettagli ristorante
    if data.startswith("details:"):
        rid = int(data.split(":")[1])
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, name, city, address, notes, rating, lat, lon, last_update
                FROM restaurants
                WHERE id = ?
                """,
                (rid,),
            )
            row = cur.fetchone()

        if not row:
            await query.message.reply_text(
                "Ristorante non trovato (forse è stato rimosso)."
            )
            return

        # Dettaglio singolo locale
        text, rid = format_restaurant_row(row)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⭐ Preferito", callback_data=f"fav:{rid}"
                    ),
                    InlineKeyboardButton(
                        "⚠️ Segnala", callback_data=f"rep:{rid}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📷 Aggiungi foto", callback_data=f"photo:{rid}"
                    )
                ],
            ]
        )
        await query.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )

        photos = get_photos_for_restaurant(rid)
        if photos:
            await query.message.reply_photo(
                photos[0],
                caption="📷 Foto dalla community",
            )
        return


# ---- PHOTO HANDLER ----

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_photo_for_user:
        await update.message.reply_text(
            "Per collegare una foto ad un locale, prima usa il bottone '📷 Aggiungi foto'."
        )
        return

    rid = pending_photo_for_user.pop(user.id)
    photo = update.message.photo[-1]
    file_id = photo.file_id
    add_photo_record(user.id, rid, file_id)
    add_points(user.id, 2)

    await update.message.reply_text(
        "📷 Foto salvata e collegata al ristorante. Grazie!",
        reply_markup=main_keyboard(),
    )


# ==========================
# MAIN
# ==========================

def build_application():
    ensure_schema()

    app = Application.builder().token(BOT_TOKEN).build()

    # Comandi base
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Aggiungi ristorante (ConversationHandler)
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^➕ Aggiungi ristorante$"), add_restaurant_start
            )
        ],
        states={
            ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_restaurant_name)
            ],
            ADD_CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_restaurant_city)
            ],
            ADD_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_restaurant_address)
            ],
            ADD_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_restaurant_notes)
            ],
        },
        fallbacks=[CommandHandler("cancel", add_restaurant_cancel)],
    )
    app.add_handler(conv_handler)

    # Location
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    # Photo
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Callback query (inline buttons)
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Testo generico
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


if __name__ == "__main__":
    print(f"🚀 Avvio GlutenFreeBot – SCHEMA_VERSION = {SCHEMA_VERSION}")
    print("🔄 Importo ristoranti da app_restaurants.csv...")
    try:
        import_app_restaurants()
        print("✅ Import completato.")
    except Exception as e:
        print("⚠️ Errore durante l'import dei ristoranti:", e)

    application = build_application()
    print("🤖 GlutenFreeBot avviato...")
    application.run_polling()
