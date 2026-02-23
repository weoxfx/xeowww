from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from flask import Flask, request, jsonify
import asyncio
import os
import logging
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
# Global Bot
# =====================
bot_instance = Bot(token=BOT_TOKEN)
telegram_app = None

# =========================================================
# 🧠 CHANNEL CHECK LOGIC (with bot admin detection)
# =========================================================

async def check_user_in_channel(user_id: int, channel: str):
    """
    Returns:
        "joined"
        "not_joined"
        "bot_not_admin"
        "error"
    """
    try:
        member = await bot_instance.get_chat_member(
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

        await bot_instance.send_message(
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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(
        send_transaction_notification_async(data)
    )
    loop.close()
    return result

# =========================================================
# 🌐 FLASK ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "service": "Xeo Wallet Bot"
    })


@app.route("/notify_transaction", methods=["POST"])
def notify_transaction():
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


# 🔥 FORCE-SUB CHECK (LIFAFA)

@app.route("/check_channels", methods=["POST"])
def check_channels():
    data = request.json
    user_id = data.get("user_id")
    channels = data.get("channels")

    if not user_id or not channels:
        return jsonify({"error": "Missing user_id or channels"}), 400

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        not_joined, bot_missing = loop.run_until_complete(
            verify_user_channels(user_id, channels)
        )
        loop.close()

        # 🚨 bot not admin case
        if bot_missing:
            return jsonify({
                "ok": False,
                "bot_error": True,
                "bot_missing_channels": bot_missing,
                "message": "Bot is not admin in some channels"
            })

        # ✅ normal result
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
    global telegram_app

    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_cmd))
    telegram_app.add_handler(CommandHandler("id", id_cmd))

    logger.info("Bot started")

    await telegram_app.initialize()
    await telegram_app.start()

    # ✅ IMPORTANT FIX
    await telegram_app.run_polling(
        stop_signals=None  # ← THIS FIXES YOUR ERROR
    )

def run_telegram_bot():
    asyncio.run(run_bot_async())

# =========================================================
# 🏁 MAIN
# =========================================================

if __name__ == "__main__":
    Thread(target=run_telegram_bot, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
