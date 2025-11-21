import math
import sqlite3
import os
from contextlib import closing
from datetime import datetime
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
DB_PATH = "restaurants.db"

# Stati per ConversationHandler "aggiungi ristorante"
ADD_NAME, ADD_CITY, ADD_ADDRESS, ADD_NOTES = range(4)

# Memoria in RAM per gestire "aggiungi foto dopo"
pending_photo_for_user = {}  # {user_id: restaurant_id}


# ==========================
# UTILS DB
# ==========================

def get_conn():
    return sqlite3.connect(DB_PATH)


def ensure_schema():
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # Tabella restaurants già esistente, ma ci assicuriamo colonne extra
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

        # Semplice logica titoli
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
            # cancello le impostazioni
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


# ==========================
# LOGICA RISTORANTI
# ==========================

def eval_risk(notes: str) -> str:
    """
    Semplice euristica per "rischio contaminazione"
    """
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
    """
    Ritorna distanza in km tra due coordinate.
    """
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


def format_restaurant_row(row, user_location=None):
    """
    row: (id, name, city, address, notes, rating, lat, lon, last_update?)
    """
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


def query_by_city(city: str, user_id: int):
    settings = get_user_settings(user_id)
    min_rating = settings.get("min_rating")

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        sql = """
        SELECT id, name, city, address, notes, rating, lat, lon, last_update
        FROM restaurants
        WHERE LOWER(city) = LOWER(?)
        ORDER BY rating DESC NULLS LAST, name ASC
        """
        cur.execute(sql, (city,))
        rows = cur.fetchall()

    if min_rating is not None:
        rows = [r for r in rows if (r[5] is None or r[5] >= min_rating)]

    return rows


def query_nearby(lat: float, lon: float, user_id: int, max_results: int = 15):
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

    # Filtra per rating se impostato
    if min_rating is not None:
        enriched = [e for e in enriched if (e[1][5] is None or e[1][5] >= min_rating)]

    # Limita il numero
    enriched = enriched[:max_results]

    return [e[1] for e in enriched]


def query_recent_in_cities(cities: List[str], days: int = 7):
    # Semplice: filtra per last_update se presente e città
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        placeholders = ",".join("?" * len(cities))
        sql = f"""
        SELECT id, name, city, address, notes, rating, lat, lon, last_update
        FROM restaurants
        WHERE city IN ({placeholders})
        ORDER BY last_update DESC
        LIMIT 50
        """
        cur.execute(sql, cities)
        return cur.fetchall()


# ==========================
# HANDLER BOT
# ==========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["➕ Aggiungi ristorante", "⭐ I miei preferiti"],
            ["⚙️ Filtri", "🔔 Novità città seguite"],
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
        "• Aggiungi ristorante – aggiungi un locale segnalato da te\n"
        "• I miei preferiti – ristoranti che hai salvato ⭐\n"
        "• Filtri – imposta rating minimo\n"
        "• Novità città seguite – locali nuovi nelle città che segui\n"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())


# ---- CERCA PER CITTA' ----

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

    if text == "Invia posizione 📍":
        await update.message.reply_text(
            "Usa il bottone posizione di Telegram per mandarmi la geolocalizzazione."
        )
        return

    # default: messaggio testo qualsiasi
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

    rows = query_by_city(city, user.id)
    if not rows:
        await update.message.reply_text(
            f"Al momento non ho ristoranti gluten-friendly per <b>{city}</b>.",
            parse_mode="HTML",
        )
        return

    # offro possibilità di seguire la città
    subscribe_btn = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🔔 Segui {city}", callback_data=f"subcity:{city}"
                )
            ]
        ]
    )

    await update.message.reply_text(
        f"Ho trovato <b>{len(rows)}</b> ristoranti per <b>{city}</b>.\n"
        f"Te ne mostro alcuni:",
        parse_mode="HTML",
        reply_markup=subscribe_btn,
    )

    # Mostra max 10
    for r in rows[:10]:
        text, rid = format_restaurant_row(r)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}"),
                    InlineKeyboardButton("⚠️ Segnala", callback_data=f"rep:{rid}"),
                ],
                [
                    InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")
                ],
            ]
        )
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )

        # prova a mandare eventuali foto community
        photos = get_photos_for_restaurant(rid)
        if photos:
            await update.message.reply_photo(
                photos[0],
                caption="📷 Foto dalla community",
            )


# ---- VICINO A ME ----

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude

    rows = query_nearby(lat, lon, user.id)
    if not rows:
        await update.message.reply_text(
            "Al momento non ho ristoranti con coordinate vicino a te."
        )
        return

    await update.message.reply_text(
        f"Ho trovato <b>{len(rows)}</b> ristoranti vicino a te:",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    for r in rows:
        text, rid = format_restaurant_row(r, user_location=(lat, lon))
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}"),
                    InlineKeyboardButton("⚠️ Segnala", callback_data=f"rep:{rid}"),
                ],
                [
                    InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")
                ],
            ]
        )
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )

        photos = get_photos_for_restaurant(rid)
        if photos:
            await update.message.reply_photo(
                photos[0],
                caption="📷 Foto dalla community",
            )


# ---- PREFERITI ----

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
                    InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}"),
                ]
            ]
        )
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )
        photos = get_photos_for_restaurant(rid)
        if photos:
            await update.message.reply_photo(
                photos[0],
                caption="📷 Foto dalla community",
            )


# ---- FILTRI ----

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
                InlineKeyboardButton("❌ Nessun filtro rating", callback_data="filt:none")
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
                    InlineKeyboardButton("⭐ Preferito", callback_data=f"fav:{rid}"),
                    InlineKeyboardButton("⚠️ Segnala", callback_data=f"rep:{rid}"),
                ],
                [
                    InlineKeyboardButton("📷 Aggiungi foto", callback_data=f"photo:{rid}")
                ],
            ]
        )
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )


# ---- AGGIUNGI RISTORANTE (USER) ----

async def add_restaurant_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await update.message.reply_text(
        "Ok, aggiungiamo un nuovo ristorante suggerito da te.\n"
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

    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO restaurants
                (name, city, address, notes, source, lat, lon, rating, last_update)
            VALUES (?, ?, ?, ?, 'user', NULL, NULL, NULL, ?)
            """,
            (name, city, address, notes, datetime.utcnow().isoformat()),
        )
        conn.commit()

    add_points(user.id, 2)

    await update.message.reply_text(
        "Grazie! Il ristorante è stato aggiunto alla lista utenti. 🙌",
        reply_markup=main_keyboard(),
    )

    return ConversationHandler.END


async def add_restaurant_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await update.message.reply_text(
        "Aggiunta ristorante annullata.", reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# ---- CALLBACK INLINE BUTTONS (fav, report, photo, filters, subscribe city) ----

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

    # Segnala
    if data.startswith("rep:"):
        rid = int(data.split(":")[1])
        # semplice: salvo un report generico "Segnalazione da bot"
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


# ---- PHOTO HANDLER ----

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in pending_photo_for_user:
        # foto non legata a nessun ristorante
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
        entry_points=[MessageHandler(filters.Regex("^➕ Aggiungi ristorante$"), add_restaurant_start)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_restaurant_name)],
            ADD_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_restaurant_city)],
            ADD_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_restaurant_address)],
            ADD_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_restaurant_notes)],
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

    # Testo generico (menu, cerca città, ecc.)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


if __name__ == "__main__":
    application = build_application()
    print("🤖 GlutenFreeBot avviato...")
    application.run_polling()
