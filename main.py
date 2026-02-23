import os
import logging
from flask import Flask, request, jsonify
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =====================
# Logging
# =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =====================
# ENV
# =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render auto provides

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

if not RENDER_URL:
    raise ValueError("RENDER_EXTERNAL_URL not found (Render only)")

MINIAPP_URL = "https://xeowallet.vercel.app"

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}"

# =====================
# Flask
# =====================
app = Flask(__name__)

# =====================
# Telegram App
# =====================
telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
bot_instance = Bot(token=BOT_TOKEN)

# =========================================================
# 🤖 COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    msg = (
        f"👋 <b>Hello {user.first_name}!</b>\n\n"
        "Welcome to <b>Xeo Wallet Bot</b>. 💼"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})]
    ])

    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /start to begin.")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(str(update.effective_user.id))

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("help", help_cmd))
telegram_app.add_handler(CommandHandler("id", id_cmd))

# =========================================================
# 🌐 ROUTES
# =========================================================

@app.route("/")
def home():
    return jsonify({"status": "online", "mode": "webhook"})

# 🔥 TELEGRAM WEBHOOK RECEIVER
@app.route(WEBHOOK_PATH, methods=["POST"])
async def telegram_webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, bot_instance)
        await telegram_app.process_update(update)
        return "ok"
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "error", 500

# =========================================================
# 🚀 STARTUP
# =========================================================

async def setup_webhook():
    logger.info(f"Setting webhook → {WEBHOOK_URL}")
    await bot_instance.delete_webhook(drop_pending_updates=True)
    await bot_instance.set_webhook(url=WEBHOOK_URL)

# =========================================================
# 🏁 MAIN
# =========================================================

if __name__ == "__main__":
    import asyncio

    loop = asyncio.get_event_loop()
    loop.run_until_complete(telegram_app.initialize())
    loop.run_until_complete(setup_webhook())

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
