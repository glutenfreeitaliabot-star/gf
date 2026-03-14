
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from bot_glutenfree import (
    PREMIUM_BOT_LINK,
    build_application,
    ensure_schema,
    get_quota_payload,
    increment_daily_searches,
    query_nearby,
    query_restaurants_text,
    serialize_restaurant,
)
from import_app_restaurants import import_app_restaurants

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN mancante")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET mancante")

telegram_app = None


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


def resolve_user_id(init_data: str = "", user_id: int = 0) -> int:
    parsed = validate_telegram_init_data(init_data)
    if parsed and isinstance(parsed.get("user"), dict):
        try:
            return int(parsed["user"].get("id") or 0)
        except Exception:
            pass
    return int(user_id or 0)


def maybe_increment_quota(user_id: int) -> dict:
    qp = get_quota_payload(user_id)
    if qp["paywall_required"]:
        return qp
    if not qp["is_premium"]:
        increment_daily_searches(user_id)
    return get_quota_payload(user_id)


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
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def health():
    return {"ok": True, "service": "glutenfree-bot"}


@app.get("/api/premium-link")
async def api_premium_link():
    return {"premium_bot_link": PREMIUM_BOT_LINK}


@app.get("/api/quota")
async def api_quota(init_data: str = Query(default=""), user_id: int = Query(default=0)):
    uid = resolve_user_id(init_data, user_id)
    return get_quota_payload(uid)


@app.get("/api/restaurants")
async def api_restaurants(q: str = Query(default=""), limit: int = Query(default=50, ge=1, le=200)):
    rows = query_restaurants_text(q, limit=limit)
    return [serialize_restaurant(r) for r in rows]


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
    return {"ok": True, "paywall": False, "quota": qp, "items": [serialize_restaurant(r) for r in rows]}


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
