import os
import logging
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes
)

from bot_glutenfree import build_application, ensure_schema

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")

app = FastAPI()

telegram_app: Application | None = None


# -------------------------------
# BOT HANDLERS
# -------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = {
        "inline_keyboard": [[
            {
                "text": "🌍 Apri GlutenFree App",
                "web_app": {
                    "url": os.getenv(
                        "MINIAPP_URL",
                        "https://glutenfree-miniapp.vercel.app"
                    )
                }
            }
        ]]
    }

    await update.message.reply_text(
        "Benvenuto in GlutenFree Italia 🍽\n\nApri la nuova app qui sotto:",
        reply_markup=keyboard
    )


def build_bot():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))

    return application


# -------------------------------
# FASTAPI STARTUP
# -------------------------------

@app.on_event("startup")
async def startup():

    global telegram_app

    ensure_schema()

    telegram_app = build_bot()

    await telegram_app.initialize()
    await telegram_app.start()


@app.on_event("shutdown")
async def shutdown():

    global telegram_app

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()


# -------------------------------
# TELEGRAM WEBHOOK
# -------------------------------

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):

    if secret != WEBHOOK_SECRET:
        return {"status": "unauthorized"}

    data = await request.json()

    update = Update.de_json(data, telegram_app.bot)

    await telegram_app.process_update(update)

    return {"status": "ok"}


# -------------------------------
# HEALTHCHECK
# -------------------------------

@app.get("/")
async def root():
    return {"status": "running"}