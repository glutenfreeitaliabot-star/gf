
import math
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://glutenfree-miniapp.vercel.app")
PREMIUM_BOT_LINK = os.getenv("PREMIUM_BOT_LINK", "https://t.me/glutenfreeitaliabot?start=premium")
PREMIUM_PRICE_STARS = int(os.getenv("PREMIUM_PRICE_STARS", "299"))
PREMIUM_DURATION_DAYS = int(os.getenv("PREMIUM_DURATION_DAYS", "30"))
FREE_SEARCHES_PER_DAY = int(os.getenv("FREE_SEARCHES_PER_DAY", "3"))
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or 0)
CONTACT_LINK = os.getenv("CONTACT_LINK", "https://t.me/glutenfreeitaliabot")
DB_PATH = "restaurants.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_add_column(cur: sqlite3.Cursor, table: str, column_sql: str) -> None:
    column_name = column_sql.split()[0]
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column_name not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")


def ensure_schema() -> None:
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
                last_update TEXT
            )
            """
        )
        _safe_add_column(cur, "restaurants", "rating_online_gf REAL")
        _safe_add_column(cur, "restaurants", "types TEXT")
        _safe_add_column(cur, "restaurants", "phone TEXT")
        _safe_add_column(cur, "restaurants", "website TEXT")
        _safe_add_column(cur, "restaurants", "google_maps_url TEXT")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS premium_subscriptions (
                user_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payment_source TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS search_usage_daily (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                searches INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                event_value TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.commit()


def _normalize_text(value: Optional[str]) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _normalize_coords(lat_raw, lon_raw) -> Tuple[Optional[float], Optional[float]]:
    lat = _to_float(lat_raw)
    lon = _to_float(lon_raw)
    if lat is None or lon is None:
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> Optional[float]:
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def is_admin_user(user_id: int) -> bool:
    return bool(ADMIN_TELEGRAM_ID and user_id and int(user_id) == ADMIN_TELEGRAM_ID)


def has_premium_access(user_id: int) -> bool:
    return is_admin_user(user_id) or is_user_premium(user_id)


def log_usage_event(user_id: int, event_type: str, event_value: str = "") -> None:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usage_events (user_id, event_type, event_value, created_at) VALUES (?, ?, ?, ?)",
            (user_id or 0, event_type, event_value[:500], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def is_user_premium(user_id: int) -> bool:
    if is_admin_user(user_id):
        return True
    if not user_id:
        return False
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, expires_at FROM premium_subscriptions WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row or row["status"] != "active":
            return False
        try:
            return datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)
        except Exception:
            return False


def activate_premium(user_id: int) -> None:
    starts_at = datetime.now(timezone.utc)
    expires_at = starts_at + timedelta(days=PREMIUM_DURATION_DAYS)
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO premium_subscriptions (user_id, status, starts_at, expires_at, payment_source, updated_at)
            VALUES (?, 'active', ?, ?, 'telegram_stars', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                status='active',
                starts_at=excluded.starts_at,
                expires_at=excluded.expires_at,
                payment_source='telegram_stars',
                updated_at=excluded.updated_at
            """,
            (user_id, starts_at.isoformat(), expires_at.isoformat(), starts_at.isoformat()),
        )
        conn.commit()


def get_used_searches_today(user_id: int) -> int:
    if not user_id:
        return 0
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT searches FROM search_usage_daily WHERE user_id = ? AND day = ?", (user_id, _today_utc()))
        row = cur.fetchone()
        return int(row["searches"]) if row else 0


def increment_daily_searches(user_id: int) -> None:
    if not user_id:
        return
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO search_usage_daily (user_id, day, searches)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, day) DO UPDATE SET searches = searches + 1
            """,
            (user_id, _today_utc()),
        )
        conn.commit()


def get_quota_payload(user_id: int) -> dict:
    premium = has_premium_access(user_id)
    used = get_used_searches_today(user_id)
    remaining = 999999 if premium else max(0, FREE_SEARCHES_PER_DAY - used)
    return {
        "user_id": user_id,
        "is_premium": premium,
        "free_daily_limit": FREE_SEARCHES_PER_DAY,
        "used_today": used,
        "remaining_today": remaining,
        "paywall_required": (not premium and used >= FREE_SEARCHES_PER_DAY),
        "premium_bot_link": PREMIUM_BOT_LINK,
    }


def serialize_restaurant(row: sqlite3.Row) -> dict:
    lat, lon = _normalize_coords(row["lat"], row["lon"])
    return {
        "id": row["id"],
        "name": row["name"],
        "city": row["city"],
        "address": row["address"] or "",
        "notes": row["notes"] or "",
        "types": row["types"] or "",
        "phone": row["phone"] or "",
        "website": row["website"] or "" if "website" in row.keys() else "",
        "google_maps_url": row["google_maps_url"] or "" if "google_maps_url" in row.keys() else "",
        "rating": row["rating"],
        "rating_online_gf": row["rating_online_gf"] if "rating_online_gf" in row.keys() else None,
        "lat": lat,
        "lon": lon,
        "source": row["source"],
    }


def _restaurant_score_for_query(row: sqlite3.Row, q_norm: str) -> int:
    city = _normalize_text(row["city"])
    name = _normalize_text(row["name"])
    address = _normalize_text(row["address"])
    types = _normalize_text(row["types"])
    score = 0
    if city == q_norm:
        score += 140
    if name == q_norm:
        score += 130
    if q_norm in city:
        score += 90
    if q_norm in name:
        score += 80
    if q_norm in address:
        score += 35
    if q_norm in types:
        score += 25
    if city.startswith(q_norm):
        score += 15
    if name.startswith(q_norm):
        score += 15
    return score


def query_restaurants_text(query: str, limit: int = 50) -> List[sqlite3.Row]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM restaurants")
        rows = cur.fetchall()

    q_norm = _normalize_text(query)
    if not q_norm:
        rows.sort(key=lambda r: (r["rating"] is None, -(r["rating"] or 0), _normalize_text(r["name"])))
        return rows[:limit]

    scored = []
    for row in rows:
        score = _restaurant_score_for_query(row, q_norm)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], -(item[1]["rating"] or 0), _normalize_text(item[1]["name"])))
    return [row for _, row in scored[:limit]]


def query_by_city(city: str, limit: int = 12) -> List[sqlite3.Row]:
    return query_restaurants_text(city, limit=limit)


def query_nearby(lat_user: float, lon_user: float, radius_km: float = 20, limit: int = 30) -> List[Tuple[float, sqlite3.Row]]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM restaurants")
        rows = cur.fetchall()

    results: List[Tuple[float, sqlite3.Row]] = []
    for row in rows:
        lat, lon = _normalize_coords(row["lat"], row["lon"])
        if lat is None or lon is None:
            continue
        d = haversine_km(lat_user, lon_user, lat, lon)
        if d is not None and d <= radius_km:
            results.append((d, row))
    results.sort(key=lambda item: (item[0], -(item[1]["rating"] or 0), _normalize_text(item[1]["name"])))
    return results[:limit]


def inline_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🌍 Apri Mini App", web_app=WebAppInfo(url=MINIAPP_URL))],
            [InlineKeyboardButton("💎 Passa a Premium", callback_data="premium:open")],
        ]
    )


def reply_home_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🔍 Cerca per città", "📍 Vicino a me"],
            ["💎 Premium", "🌍 Mini App"],
        ],
        resize_keyboard=True,
    )


def _restaurant_line(row: sqlite3.Row, distance_km: Optional[float] = None) -> str:
    rating = f"{float(row['rating']):.1f}⭐" if row["rating"] is not None else "n.d."
    distance = f" • {distance_km:.1f} km" if distance_km is not None else ""
    types = f" • {row['types']}" if row["types"] else ""
    return f"• <b>{row['name']}</b>\n  📍 {row['city']}{types}\n  ⭐ {rating}{distance}"


async def _send_search_results(update: Update, title: str, rows: Iterable[sqlite3.Row], distances: Optional[dict] = None):
    rows = list(rows)
    if not rows:
        await update.message.reply_text(
            "Non ho trovato risultati. Prova con un nome città più semplice, ad esempio <b>Milano</b>, <b>Roma</b> o <b>Bologna</b>.",
            parse_mode="HTML",
            reply_markup=reply_home_keyboard(),
        )
        return

    lines = [title, ""]
    for row in rows[:10]:
        dist = distances.get(row["id"]) if distances else None
        lines.append(_restaurant_line(row, dist))
        lines.append("")

    lines.append("Apri la Mini App per una ricerca più avanzata e i dettagli premium.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=inline_home_keyboard())
    await update.message.reply_text("Menu 👇", reply_markup=reply_home_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_schema()
    user = update.effective_user

    if context.args and context.args[0] == "premium":
        await send_premium_invoice(update, context)
        return

    text = (
        f"Ciao {user.first_name or ''} 👋\n\n"
        "Benvenuto in <b>Glutenfree bot</b>.\n"
        "Puoi cercare una città direttamente nel bot oppure aprire la Mini App."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_home_keyboard())
    await update.message.reply_text("Scegli da qui 👇", reply_markup=inline_home_keyboard())


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_premium_invoice(update, context)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    premium_state = "attivo" if has_premium_access(user.id) else "non attivo"
    admin_state = "sì" if is_admin_user(user.id) else "no"
    await update.message.reply_text(
        (
            f"Il tuo Telegram ID è: <b>{user.id}</b>\n"
            f"Premium: <b>{premium_state}</b>\n"
            f"Admin: <b>{admin_state}</b>"
        ),
        parse_mode="HTML",
        reply_markup=reply_home_keyboard(),
    )


async def send_premium_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Glutenfree bot Premium",
        description=f"Abbonamento mensile • ricerche illimitate per {PREMIUM_DURATION_DAYS} giorni",
        payload="premium_monthly",
        currency="XTR",
        prices=[LabeledPrice("Premium mensile", PREMIUM_PRICE_STARS)],
        provider_token="",
        start_parameter="premium",
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if query.data == "premium:open":
        await send_premium_invoice(update, context)


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    activate_premium(user.id)
    log_usage_event(user.id, "premium_payment_success", "telegram_stars")
    await update.message.reply_text(
        f"✅ Premium attivato per {PREMIUM_DURATION_DAYS} giorni.\nApri la Mini App per usare ricerche illimitate e dettagli completi.",
        reply_markup=reply_home_keyboard(),
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.location:
        return
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    log_usage_event(update.effective_user.id, "bot_search_nearby", f"{lat},{lon}")
    nearby = query_nearby(lat, lon, radius_km=20)
    if not nearby:
        await update.message.reply_text(
            "Non ho trovato ristoranti con coordinate vicini alla tua posizione.",
            reply_markup=reply_home_keyboard(),
        )
        return
    distances = {row["id"]: dist for dist, row in nearby}
    await _send_search_results(update, "📍 <b>Ristoranti vicino a te</b>", [row for dist, row in nearby], distances=distances)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    if text == "🔍 Cerca per città":
        context.user_data["awaiting_city"] = True
        await update.message.reply_text(
            "Scrivi una città o anche solo parte del nome. Esempi: <b>Milano</b>, <b>Reggio</b>, <b>Bari</b>.",
            parse_mode="HTML",
            reply_markup=reply_home_keyboard(),
        )
        return

    if text == "📍 Vicino a me":
        log_usage_event(update.effective_user.id, "ui_click", "near_me_bot")
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Invia posizione 📍", request_location=True)], ["❌ Annulla"]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text("Mandami la tua posizione per cercare i locali più vicini.", reply_markup=kb)
        return

    if text == "❌ Annulla":
        context.user_data.clear()
        await update.message.reply_text("Operazione annullata.", reply_markup=reply_home_keyboard())
        return

    if text == "💎 Premium":
        await send_premium_invoice(update, context)
        return

    if text == "🌍 Mini App":
        await update.message.reply_text("Apri la Mini App da qui 👇", reply_markup=inline_home_keyboard())
        return

    if context.user_data.get("awaiting_city") or len(text) >= 2:
        context.user_data["awaiting_city"] = False
        rows = query_by_city(text)
        log_usage_event(update.effective_user.id, "bot_search_city", text)
        await _send_search_results(update, f"🔎 <b>Risultati per:</b> {text}", rows)
        return


def build_application() -> Application:
    ensure_schema()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler(["myid", "id"], myid_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app
