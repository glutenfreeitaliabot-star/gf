import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from telegram import Update

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

    # ✅ IMPORTANTISSIMO: sistema/crea lo schema PRIMA dell'import CSV
    ensure_schema()

    # Debug: prova che il container vede il CSV giusto
    try:
        st = os.stat("app_restaurants.csv")
        print(f"[boot] app_restaurants.csv size={st.st_size} mtime={st.st_mtime}")
    except Exception as e:
        print(f"[boot] CSV non trovato o non leggibile: {e}")

    # Import DB da CSV (fondamentale in modalità webhook)
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


@app.get("/")
async def health():
    return {"ok": True, "service": "GlutenFreeBot webhook"}

@app.get("/debug/stats")
async def debug_stats():
    import os, sqlite3, csv

    # CSV stats
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
            csv_rows = sum(1 for _ in r) - 1  # - header
    except Exception as e:
        csv_error = str(e)

    # DB stats
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


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}
