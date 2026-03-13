import os
import sqlite3
from contextlib import asynccontextmanager, closing
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

import bot_glutenfree as bg
from bot_glutenfree import build_application, ensure_schema
from import_app_restaurants import import_app_restaurants


BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN mancante. Imposta la variabile d'ambiente BOT_TOKEN.")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET mancante. Imposta la variabile d'ambiente WEBHOOK_SECRET.")

application = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global application

    ensure_schema()

    try:
        st = os.stat("app_restaurants.csv")
        print(f"[boot] app_restaurants.csv size={st.st_size} mtime={st.st_mtime}")
    except Exception as e:
        print(f"[boot] CSV non trovato o non leggibile: {e}")

    try:
        print("🔄 Importo ristoranti da app_restaurants.csv (webhook startup)...")
        import_app_restaurants()
        print("✅ Import completato.")
    except Exception as e:
        print("⚠️ Errore import CSV:", e)

    application = build_application()
    await application.initialize()
    await application.start()

    yield

    await application.stop()
    await application.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_user_settings(user_id: int):
    if not user_id:
        return {"min_rating": None, "type_filter": None}
    return bg.get_user_settings(user_id)


def _apply_user_filters(rows, user_id: int):
    settings = _get_user_settings(user_id)
    min_rating = settings.get("min_rating")
    type_filter = settings.get("type_filter")

    if min_rating is not None:
        rows = [r for r in rows if (r["rating"] is None or float(r["rating"]) >= float(min_rating))]

    if type_filter:
        tf = str(type_filter).strip().lower()
        rows = [
            r for r in rows
            if (r["types"] and tf in {t.strip().lower() for t in str(r["types"]).split("|") if t.strip()})
        ]

    return rows


def _is_favorite(user_id: int, restaurant_id: int) -> bool:
    if not user_id:
        return False
    with closing(bg.get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND restaurant_id = ? LIMIT 1",
            (user_id, restaurant_id),
        )
        return cur.fetchone() is not None


def _toggle_favorite(user_id: int, restaurant_id: int) -> bool:
    with closing(bg.get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND restaurant_id = ? LIMIT 1",
            (user_id, restaurant_id),
        )
        exists = cur.fetchone() is not None

        if exists:
            cur.execute(
                "DELETE FROM favorites WHERE user_id = ? AND restaurant_id = ?",
                (user_id, restaurant_id),
            )
            conn.commit()
            return False

        bg.add_favorite(user_id, restaurant_id)
        return True


def _maps_url(r):
    name = str(r["name"] or "").replace(" ", "+")
    city = str(r["city"] or "").replace(" ", "+")
    return f"https://www.google.com/maps/search/?api=1&query={name}+{city}"


def _serialize_restaurant(r, user_id: int = 0, user_location: Optional[tuple] = None):
    lat, lon = bg._normalize_coords(r["lat"], r["lon"])
    distance_km = None
    if user_location and lat is not None and lon is not None:
        distance_km = bg.haversine_km(user_location[0], user_location[1], lat, lon)

    return {
        "id": int(r["id"]),
        "name": r["name"],
        "city": r["city"],
        "address": r["address"],
        "notes": r["notes"],
        "source": r["source"],
        "lat": lat,
        "lon": lon,
        "rating": r["rating"],
        "rating_online_gf": r["rating_online_gf"] if "rating_online_gf" in r.keys() else None,
        "last_update": r["last_update"],
        "types": r["types"],
        "phone": r["phone"],
        "maps_url": _maps_url(r),
        "distance_km": distance_km,
        "is_favorite": _is_favorite(user_id, int(r["id"])),
    }


@app.get("/")
async def health():
    return {"ok": True, "service": "GlutenFreeBot webhook"}


@app.get("/api/health")
async def api_health():
    return {"ok": True, "service": "miniapp-api"}


@app.get("/debug/stats")
async def debug_stats():
    import csv

    csv_path = "app_restaurants.csv"
    csv_rows = None
    csv_size = None
    csv_mtime = None
    csv_error = None
    try:
        st = os.stat(csv_path)
        csv_size = st.st_size
        csv_mtime = st.st_mtime
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            r = csv.reader(f)
            csv_rows = sum(1 for _ in r) - 1
    except Exception as e:
        csv_error = str(e)

    db_path = "restaurants.db"
    db_total = None
    db_app = None
    db_error = None
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM restaurants")
        db_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM restaurants WHERE source='app'")
        db_app = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        db_error = str(e)

    return {
        "csv": {"path": csv_path, "rows": csv_rows, "size": csv_size, "mtime": csv_mtime, "error": csv_error},
        "db": {"path": db_path, "restaurants_total": db_total, "restaurants_source_app": db_app, "error": db_error},
    }


@app.get("/api/types")
async def api_types():
    items = set()
    with closing(bg.get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT types FROM restaurants WHERE types IS NOT NULL AND TRIM(types) <> ''")
        for row in cur.fetchall():
            for item in str(row["types"]).split("|"):
                item = item.strip()
                if item:
                    items.add(item)
    return {"types": sorted(items)}


@app.get("/api/settings")
async def api_settings(user_id: int = 0):
    return _get_user_settings(user_id)


@app.post("/api/settings")
async def api_save_settings(request: Request):
    payload = await request.json()
    user_id = int(payload.get("user_id") or 0)
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id obbligatorio")

    min_rating = payload.get("min_rating")
    type_filter = payload.get("type_filter")

    if min_rating in ("", "none", "null"):
        min_rating = None
    if type_filter in ("", "none", "null"):
        type_filter = None

    bg.set_user_min_rating(user_id, float(min_rating) if min_rating is not None else None)
    bg.set_user_type_filter(user_id, str(type_filter) if type_filter is not None else None)
    return _get_user_settings(user_id)


@app.get("/api/restaurants")
async def api_restaurants(
    q: str = "",
    city: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: float = 5,
    limit: int = 50,
    user_id: int = 0,
):
    limit = max(1, min(int(limit), 100))

    if lat is not None and lon is not None:
        rows = bg.query_nearby(user_id, lat, lon, radius_km=radius_km, max_results=limit)
        return [_serialize_restaurant(r, user_id=user_id, user_location=(lat, lon)) for r in rows]

    with closing(bg.get_conn()) as conn:
        cur = conn.cursor()
        sql = "SELECT * FROM restaurants WHERE 1=1"
        params = []

        if city.strip():
            sql += " AND LOWER(city) = LOWER(?)"
            params.append(city.strip())

        if q.strip():
            like = f"%{q.strip().lower()}%"
            sql += " AND (LOWER(name) LIKE ? OR LOWER(city) LIKE ? OR LOWER(IFNULL(address, '')) LIKE ?)"
            params.extend([like, like, like])

        sql += " ORDER BY (rating IS NULL) ASC, rating DESC, name ASC LIMIT ?"
        params.append(limit)

        cur.execute(sql, params)
        rows = cur.fetchall()

    rows = _apply_user_filters(rows, user_id)
    return [_serialize_restaurant(r, user_id=user_id) for r in rows]


@app.get("/api/restaurants/{restaurant_id}")
async def api_restaurant_detail(
    restaurant_id: int,
    user_id: int = 0,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
):
    with closing(bg.get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM restaurants WHERE id = ?", (restaurant_id,))
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Ristorante non trovato")

    user_location = (lat, lon) if lat is not None and lon is not None else None
    return _serialize_restaurant(row, user_id=user_id, user_location=user_location)


@app.get("/api/favorites")
async def api_favorites(user_id: int = 0):
    if not user_id:
        return []
    rows = bg.get_favorites(user_id)
    return [_serialize_restaurant(r, user_id=user_id) for r in rows]


@app.post("/api/favorites/toggle")
async def api_favorites_toggle(request: Request):
    payload = await request.json()
    user_id = int(payload.get("user_id") or 0)
    restaurant_id = int(payload.get("restaurant_id") or 0)
    if not user_id or not restaurant_id:
        raise HTTPException(status_code=400, detail="user_id e restaurant_id obbligatori")

    status = _toggle_favorite(user_id, restaurant_id)
    return {"ok": True, "is_favorite": status}


@app.post("/api/reports")
async def api_reports(request: Request):
    payload = await request.json()
    user_id = int(payload.get("user_id") or 0)
    restaurant_id = int(payload.get("restaurant_id") or 0)
    reason = str(payload.get("reason") or "Segnalazione da Mini App")

    if not user_id or not restaurant_id:
        raise HTTPException(status_code=400, detail="user_id e restaurant_id obbligatori")

    bg.add_report(user_id, restaurant_id, reason)
    return {"ok": True}


@app.post("/api/suggestions")
async def api_suggestions(request: Request):
    payload = await request.json()
    user_id = int(payload.get("user_id") or 0)
    name = str(payload.get("name") or "").strip()
    city = str(payload.get("city") or "").strip()
    suggestion_type = str(payload.get("suggestion_type") or "unknown").strip()
    notes = str(payload.get("notes") or "").strip()

    if not user_id or not name or not city:
        raise HTTPException(status_code=400, detail="user_id, name e city obbligatori")

    bg.save_suggestion(user_id, name, city, suggestion_type, notes)
    bg.log_usage(user_id, "miniapp_suggest_submit", city=city)
    return {"ok": True}


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}
