from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from flask import Flask, request, jsonify
import asyncio
import os
import logging
import signal
import threading
import atexit
from threading import Thread

# =====================
# Configuration & Logging
# =====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

MINIAPP_URL = "https://xeowallet.vercel.app"

# =====================
# Flask App Setup
# =====================
app = Flask(__name__)

# =====================
# Bot State
# =====================
_state = {
    "loop": None,
    "app": None,
    "ready": threading.Event(),
}

# =========================================================
# 🛡️ BOT READY HELPERS
# =========================================================

def bot_is_ready():
    return _state["ready"].is_set() and _state["loop"] is not None


def wait_for_bot(timeout=30):
    logger.info(f"[WAIT] Waiting for bot ready event (timeout={timeout}s)...")
    result = _state["ready"].wait(timeout=timeout)
    logger.info(f"[WAIT] Bot ready event result: {result}, loop={_state['loop']}, app={_state['app']}")
    return result

# =========================================================
# 🧠 CHANNEL CHECK LOGIC
# =========================================================

async def check_user_in_channel(user_id: int, channel: str):
    try:
        member = await _state["app"].bot.get_chat_member(
            chat_id=channel,
            user_id=user_id
        )
        if member.status in ["member", "administrator", "creator"]:
            return "joined"
        return "not_joined"

    except Exception as e:
        err = str(e).lower()
        if (
            "chat not found" in err
            or "not enough rights" in err
            or "have no rights" in err
            or "forbidden" in err
        ):
            logger.warning(f"Bot lacks access to {channel}")
            return "bot_not_admin"
        logger.warning(f"Channel check failed for {channel}: {e}")
        return "error"


async def verify_user_channels(user_id: int, channels: list):
    not_joined = []
    bot_missing = []
    for ch in channels:
        result = await check_user_in_channel(user_id, ch)
        if result == "not_joined":
            not_joined.append(ch)
        elif result == "bot_not_admin":
            bot_missing.append(ch)
    return not_joined, bot_missing

# =========================================================
# 🤖 TELEGRAM COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = (
        f"👋 <b>Hello {user.first_name}!</b>\n\n"
        "Welcome to <b>Xeo Wallet Bot</b>. 💼\n"
        "You will receive notifications for all your wallet transactions here.\n\n"
        "Use /help to see available commands."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})]
    ])
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📝 <b>Available Commands:</b>\n\n"
        "• /start - Start the bot\n"
        "• /help - Show this help message\n"
        "• /id - Get your Telegram ID\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 Channel: @Xeo_Wallet\n"
        "👨‍💻 Developer: @Gamenter\n"
        "🤖 Bot: @XeoWalletBot\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "💡 All wallet transactions will be notified automatically here."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})]
    ])
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"<b>{update.effective_user.id}</b>",
        parse_mode="HTML"
    )

# =========================================================
# 💰 TRANSACTION NOTIFICATIONS
# =========================================================

async def send_transaction_notification_async(data: dict):
    user_id = data.get("user_id")
    t_type = data.get("type", "Unknown")
    amount = data.get("amount", "0")
    status = data.get("status", "Unknown")
    sender = data.get("sender", "N/A")
    comment = data.get("comment", "No comment")
    balance = data.get("balance", "0")

    if not user_id:
        logger.error("Missing user_id in transaction data")
        return False

    try:
        if status.lower() == "success":
            if t_type.lower() in ["send_credit", "api_debit"]:
                type_emoji = "🏧"
            elif t_type.lower() == "addfund":
                type_emoji = "📥"
            elif t_type.lower() == "withdraw":
                type_emoji = "📤"
            else:
                type_emoji = "⭐"
            status_emoji = "✅"
        else:
            status_emoji = "❌"
            type_emoji = "⚠️"

        msg = (
            f"💰 <b>Transaction Alert!</b>\n\n"
            f"{type_emoji} <b>Type:</b> {t_type}\n"
            f"💵 <b>Amount:</b> ₹{amount}\n"
            f"{status_emoji} <b>Status:</b> {status}\n"
            f"👤 <b>Sender:</b> {sender}\n"
            f"💬 <b>Comment:</b> {comment}\n\n"
            f"💼 <b>New Balance:</b> ₹{balance}"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})]
        ])

        await _state["app"].bot.send_message(
            chat_id=user_id,
            text=msg,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return True

    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        return False


def send_transaction_notification(data: dict):
    try:
        future = asyncio.run_coroutine_threadsafe(
            send_transaction_notification_async(data),
            _state["loop"]
        )
        return future.result(timeout=15)
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
        return False

# =========================================================
# 🌐 FLASK ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "service": "Xeo Wallet Bot",
        "bot_ready": bot_is_ready(),
        "event_set": _state["ready"].is_set(),
        "loop_set": _state["loop"] is not None,
        "app_set": _state["app"] is not None,
    })


@app.route("/notify_transaction", methods=["POST"])
def notify_transaction():
    if not wait_for_bot():
        return jsonify({"error": "Bot failed to start"}), 503

    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    required_fields = ["user_id", "type", "amount", "status"]
    missing = [f for f in required_fields if f not in data]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400

    Thread(
        target=send_transaction_notification,
        args=(data,),
        daemon=True
    ).start()

    return jsonify({"ok": True})


@app.route("/check_channels", methods=["POST"])
def check_channels():
    if not wait_for_bot():
        return jsonify({"error": "Bot failed to start"}), 503

    data = request.json
    user_id = data.get("user_id")
    channels = data.get("channels")

    if not user_id or not channels:
        return jsonify({"error": "Missing user_id or channels"}), 400

    try:
        future = asyncio.run_coroutine_threadsafe(
            verify_user_channels(user_id, channels),
            _state["loop"]
        )
        not_joined, bot_missing = future.result(timeout=15)

        if bot_missing:
            return jsonify({
                "ok": False,
                "bot_error": True,
                "bot_missing_channels": bot_missing,
                "message": "Bot is not admin in some channels"
            })

        return jsonify({
            "ok": True,
            "joined": len(not_joined) == 0,
            "missing_channels": not_joined
        })

    except Exception as e:
        logger.error(f"Channel verify error: {e}")
        return jsonify({"error": str(e)}), 500

# =========================================================
# 🚀 RUN TELEGRAM BOT
# =========================================================

async def run_bot_async():
    logger.info("[BOT] Building application...")
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_cmd))
    telegram_app.add_handler(CommandHandler("id", id_cmd))

    logger.info("[BOT] Initializing...")
    await telegram_app.initialize()

    logger.info("[BOT] Deleting webhook...")
    await telegram_app.bot.delete_webhook(drop_pending_updates=True)

    logger.info("[BOT] Starting...")
    await telegram_app.start()

    logger.info("[BOT] Setting state and signaling ready...")
    _state["app"] = telegram_app
    _state["loop"] = asyncio.get_running_loop()
    _state["ready"].set()

    logger.info(f"✅ [BOT] Ready! event={_state['ready'].is_set()} loop={_state['loop']} app={_state['app']}")

    try:
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info("[BOT] Polling started.")
        stop_event = asyncio.Event()
        await stop_event.wait()

    finally:
        logger.info("[BOT] Shutting down...")
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("[BOT] Shutdown complete.")


def run_telegram_bot():
    logger.info("[THREAD] Bot thread started.")
    asyncio.run(run_bot_async())
    logger.info("[THREAD] Bot thread ended.")

# =========================================================
# 🔴 GRACEFUL SHUTDOWN
# =========================================================

def shutdown_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    loop = _state.get("loop")
    if loop and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    os._exit(0)

atexit.register(lambda: logger.info("Process exiting."))

# =========================================================
# 🏁 START BOT THREAD
# =========================================================

logger.info("[MAIN] Module loaded, starting bot thread...")

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

bot_thread = Thread(target=run_telegram_bot, daemon=True)
bot_thread.start()

logger.info("[MAIN] Bot thread launched.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
