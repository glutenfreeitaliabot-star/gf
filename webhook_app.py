import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import parse_qsl, quote_plus, urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from bot_glutenfree import (
    ADMIN_TELEGRAM_ID,
    CONTACT_LINK,
    MINIAPP_URL,
    PREMIUM_BOT_LINK,
    activate_premium,
    build_application,
    deactivate_premium,
    ensure_schema,
    get_conn,
    get_quota_payload,
    has_premium_access,
    increment_daily_searches,
    is_admin_user,
    log_usage_event,
    query_nearby,
    query_restaurants_text,
    serialize_restaurant,
    upsert_restaurant_review,
)
from import_app_restaurants import import_app_restaurants

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
ALLOWED_ORIGINS_ENV = os.getenv("ALLOWED_ORIGINS", "")
GOOGLE_MAPS_BROWSER_API_KEY = os.getenv("GOOGLE_MAPS_BROWSER_API_KEY", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN mancante")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET mancante")

telegram_app = None


class ReviewIn(BaseModel):
    stars: int
    review_text: str = ""


def _build_allowed_origins() -> list[str]:
    origins = set()
    if MINIAPP_URL:
        try:
            parsed = urlparse(MINIAPP_URL)
            if parsed.scheme and parsed.netloc:
                origins.add(f"{parsed.scheme}://{parsed.netloc}")
        except Exception:
            pass

    for item in ALLOWED_ORIGINS_ENV.split(","):
        value = item.strip()
        if value:
            origins.add(value)

    origins.update(
        {
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        }
    )
    return sorted(origins)


def validate_telegram_init_data(init_data: str) -> Optional[dict]:
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(pairs.get("auth_date", "0"))
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if auth_date <= 0 or abs(now_ts - auth_date) > 86400:
            return None

        if "user" in pairs:
            pairs["user"] = json.loads(pairs["user"])
        return pairs
    except Exception:
        return None


def _parsed_user_id(parsed: Optional[dict]) -> int:
    if parsed and isinstance(parsed.get("user"), dict):
        try:
            return int(parsed["user"].get("id") or 0)
        except Exception:
            return 0
    return 0


def resolve_user_id(init_data: str = "", user_id: int = 0) -> int:
    del user_id
    return _parsed_user_id(validate_telegram_init_data(init_data))


def require_telegram_user(init_data: str = "") -> Tuple[int, dict]:
    parsed = validate_telegram_init_data(init_data)
    uid = _parsed_user_id(parsed)
    if not uid:
        raise HTTPException(status_code=401, detail="Valid Telegram init_data required")
    return uid, parsed


def maybe_increment_quota(user_id: int) -> dict:
    qp = get_quota_payload(user_id)
    if qp["paywall_required"]:
        return qp
    if not qp["is_premium"]:
        increment_daily_searches(user_id)
    return get_quota_payload(user_id)


def serialize_restaurant_public(row):
    item = serialize_restaurant(row)
    return {
        "id": item["id"],
        "name": item["name"],
        "city": item["city"],
        "types": item["types"],
        "rating": item["rating"],
        "rating_web": item["rating_web"],
        "rating_online_gf": item["rating_online_gf"],
        "community_rating": item["community_rating"],
        "community_reviews_count": item["community_reviews_count"],
        "notes": item["notes"],
        "lat": item["lat"],
        "lon": item["lon"],
    }


def get_restaurant_by_id(restaurant_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM restaurants WHERE id = ? AND COALESCE(is_active, 1) = 1", (restaurant_id,))
        return cur.fetchone()


def build_admin_dashboard() -> dict:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM restaurants WHERE COALESCE(is_active, 1) = 1")
        restaurants_total = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM premium_subscriptions WHERE status = 'active'")
        premium_active = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM premium_subscriptions")
        subscriptions_total = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(DISTINCT user_id) AS c FROM search_usage_daily")
        unique_search_users = cur.fetchone()["c"]

        today = datetime.now(timezone.utc).date().isoformat()
        cur.execute("SELECT COALESCE(SUM(searches), 0) AS c FROM search_usage_daily WHERE day = ?", (today,))
        searches_today = cur.fetchone()["c"]

        cur.execute("SELECT COALESCE(SUM(searches), 0) AS c FROM search_usage_daily")
        searches_total = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM restaurant_reviews")
        reviews_total = cur.fetchone()["c"]

        cur.execute(
            "SELECT user_id, status, starts_at, expires_at, payment_source, updated_at FROM premium_subscriptions ORDER BY updated_at DESC LIMIT 100"
        )
        premium_rows = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT day, COALESCE(SUM(searches), 0) AS searches FROM search_usage_daily GROUP BY day ORDER BY day DESC LIMIT 14"
        )
        searches_by_day = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT event_type, COUNT(*) AS count FROM usage_events GROUP BY event_type ORDER BY count DESC LIMIT 20")
        events_breakdown = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT user_id, event_type, event_value, created_at FROM usage_events ORDER BY id DESC LIMIT 120")
        recent_events = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT rr.user_id, rr.restaurant_id, rr.stars, rr.review_text, rr.updated_at, r.name AS restaurant_name
            FROM restaurant_reviews rr
            LEFT JOIN restaurants r ON r.id = rr.restaurant_id
            ORDER BY rr.updated_at DESC
            LIMIT 100
            """
        )
        recent_reviews = [dict(r) for r in cur.fetchall()]

    return {
        "restaurants_total": restaurants_total,
        "premium_active": premium_active,
        "subscriptions_total": subscriptions_total,
        "unique_search_users": unique_search_users,
        "searches_today": searches_today,
        "searches_total": searches_total,
        "reviews_total": reviews_total,
        "premium_rows": premium_rows,
        "searches_by_day": searches_by_day,
        "events_breakdown": events_breakdown,
        "recent_events": recent_events,
        "recent_reviews": recent_reviews,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    ensure_schema()
    try:
        import_app_restaurants()
        print("✅ CSV import completato")
    except Exception as e:
        print("⚠️ Errore import CSV:", e)

    telegram_app = build_application()
    await telegram_app.initialize()
    await telegram_app.start()
    yield
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
async def health():
    return {"ok": True, "service": "glutenfree-bot"}


@app.get("/api/premium-link")
async def api_premium_link():
    return {"premium_bot_link": PREMIUM_BOT_LINK}


@app.get("/api/public-config")
async def api_public_config():
    return {
        "ok": True,
        "google_maps_browser_api_key": GOOGLE_MAPS_BROWSER_API_KEY,
    }


@app.get("/api/me")
async def api_me(init_data: str = Query(default=""), user_id: int = Query(default=0)):
    del user_id
    parsed = validate_telegram_init_data(init_data)
    uid = _parsed_user_id(parsed)
    user = parsed.get("user") if isinstance(parsed, dict) else None
    return {
        "ok": True,
        "authenticated": bool(uid),
        "user_id": uid,
        "username": (user or {}).get("username", ""),
        "first_name": (user or {}).get("first_name", ""),
        "is_admin": is_admin_user(uid),
        "is_premium": has_premium_access(uid),
        "quota": get_quota_payload(uid),
        "contact_link": CONTACT_LINK,
        "admin_telegram_id_configured": bool(ADMIN_TELEGRAM_ID),
    }


@app.get("/api/quota")
async def api_quota(init_data: str = Query(default=""), user_id: int = Query(default=0)):
    uid = resolve_user_id(init_data, user_id)
    return get_quota_payload(uid)


@app.get("/api/admin/dashboard")
async def api_admin_dashboard(init_data: str = Query(default=""), user_id: int = Query(default=0)):
    del user_id
    uid, _ = require_telegram_user(init_data)
    if not is_admin_user(uid):
        raise HTTPException(status_code=403, detail="Admin only")
    return {"ok": True, "dashboard": build_admin_dashboard()}


@app.post("/api/admin/test-premium")
async def api_admin_test_premium(init_data: str = Query(default=""), user_id: int = Query(default=0)):
    del user_id
    uid, _ = require_telegram_user(init_data)
    if not is_admin_user(uid):
        raise HTTPException(status_code=403, detail="Admin only")
    activate_premium(uid)
    log_usage_event(uid, "admin_force_premium", "self")
    return {"ok": True, "message": "Premium attivato per il tuo utente admin."}


@app.post("/api/admin/remove-premium")
async def api_admin_remove_premium(init_data: str = Query(default=""), user_id: int = Query(default=0)):
    del user_id
    uid, _ = require_telegram_user(init_data)
    if not is_admin_user(uid):
        raise HTTPException(status_code=403, detail="Admin only")
    deactivate_premium(uid)
    log_usage_event(uid, "admin_remove_premium", "self")
    return {"ok": True, "message": "Premium disattivato per il tuo utente admin."}


@app.get("/api/restaurants/{restaurant_id}/details")
async def api_restaurant_details(restaurant_id: int, init_data: str = Query(default=""), user_id: int = Query(default=0)):
    del user_id
    uid, _ = require_telegram_user(init_data)
    if not has_premium_access(uid):
        raise HTTPException(status_code=403, detail="Premium required")
    row = get_restaurant_by_id(restaurant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    item = serialize_restaurant(row)
    log_usage_event(uid, "restaurant_details_open", str(restaurant_id))
    return {"ok": True, "item": item}


@app.post("/api/restaurants/{restaurant_id}/booked")
async def api_restaurant_booked(restaurant_id: int, init_data: str = Query(default=""), user_id: int = Query(default=0)):
    del user_id
    uid, _ = require_telegram_user(init_data)
    row = get_restaurant_by_id(restaurant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    log_usage_event(uid, "restaurant_booked", str(restaurant_id))
    sent = False
    if telegram_app is not None:
        try:
            review_url = f"{MINIAPP_URL}/search.html?q={quote_plus(row['name'])}"
            await telegram_app.bot.send_message(
                chat_id=uid,
                text=(
                    f"📅 Hai prenotato da <b>{row['name']}</b>.\n\n"
                    f"Quando vuoi, torna su <b>Glutenfree bot</b> e lascia una recensione per aiutare la community."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌍 Apri la Mini App", url=review_url)]]),
            )
            sent = True
        except Exception:
            sent = False
    return {"ok": True, "message_sent": sent}


@app.post("/api/restaurants/{restaurant_id}/review")
async def api_restaurant_review(
    restaurant_id: int,
    payload: ReviewIn,
    init_data: str = Query(default=""),
    user_id: int = Query(default=0),
):
    del user_id
    uid, _ = require_telegram_user(init_data)
    if payload.stars < 1 or payload.stars > 5:
        raise HTTPException(status_code=400, detail="Stars must be between 1 and 5")
    row = get_restaurant_by_id(restaurant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    upsert_restaurant_review(uid, restaurant_id, payload.stars, payload.review_text)
    log_usage_event(uid, "restaurant_review_submit", f"{restaurant_id}:{payload.stars}")
    refreshed = get_restaurant_by_id(restaurant_id) or row
    item = serialize_restaurant(refreshed)
    return {"ok": True, "item": item}


@app.get("/api/restaurants")
async def api_restaurants(q: str = Query(default=""), limit: int = Query(default=50, ge=1, le=200)):
    rows = query_restaurants_text(q, limit=limit)
    return [serialize_restaurant_public(r) for r in rows]


@app.get("/api/restaurants/search")
async def api_restaurants_search(
    q: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    init_data: str = Query(default=""),
    user_id: int = Query(default=0),
):
    uid = resolve_user_id(init_data, user_id)
    qp = get_quota_payload(uid)
    if qp["paywall_required"]:
        return {"ok": False, "paywall": True, "quota": qp, "items": []}

    qp = maybe_increment_quota(uid)
    rows = query_restaurants_text(q, limit=limit)
    log_usage_event(uid, "api_search_text", q or "")
    return {"ok": True, "paywall": False, "quota": qp, "items": [serialize_restaurant_public(r) for r in rows]}


@app.get("/api/restaurants/nearby")
async def api_restaurants_nearby(
    lat: float = Query(...),
    lon: float = Query(...),
    radius_km: float = Query(default=20, ge=1, le=100),
    limit: int = Query(default=30, ge=1, le=100),
    init_data: str = Query(default=""),
    user_id: int = Query(default=0),
):
    uid = resolve_user_id(init_data, user_id)
    qp = get_quota_payload(uid)
    if qp["paywall_required"]:
        return {"ok": False, "paywall": True, "quota": qp, "items": []}

    qp = maybe_increment_quota(uid)
    rows = query_nearby(lat, lon, radius_km=radius_km, limit=limit)
    log_usage_event(uid, "api_search_nearby", f"{lat},{lon}")
    items = []
    for distance_km, row in rows:
        item = serialize_restaurant(row)
        item["distance_km"] = round(distance_km, 2)
        items.append(item)
    return {"ok": True, "paywall": False, "quota": qp, "items": items}


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Telegram application not ready")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
