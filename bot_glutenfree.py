import logging from telegram import Update, InlineKeyboardButton,
InlineKeyboardMarkup, WebAppInfo from telegram.ext import
ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = “YOUR_TELEGRAM_BOT_TOKEN”

logging.basicConfig(level=logging.INFO)

def main_menu(): return InlineKeyboardMarkup([ [InlineKeyboardButton(“🌍
Apri GlutenFree App”,
web_app=WebAppInfo(url=“https://glutenfree-miniapp.vercel.app”))] ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text( “Benvenuto in GlutenFree Italia 🍽la
nuova app qui sotto:”, reply_markup=main_menu() )

def main(): app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler(“start”, start)) print(“Bot avviato”)
app.run_polling()

if name == “main”: main()
