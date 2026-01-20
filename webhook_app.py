import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from telegram import Update

from bot_glutenfree import build_application


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


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    update = Update.de_json(data, application.bot)

    await application.process_update(update)

    return {"ok": True}
