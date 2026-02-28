from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
import os
import logging
import signal
import threading
import atexit
import secrets
import time
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
ADMIN_TELEGRAM_ID = "6186511950"

# Supabase config for confirming telegram connections
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# =====================
# Flask App Setup
# =====================
app = Flask(__name__)
CORS(app, origins=["https://xeowallet.vercel.app", "http://localhost:5173", "http://localhost:3000"])

# =====================
# Bot State
# =====================
_state = {
    "loop": None,
    "app": None,
    "ready": threading.Event(),
}

# =====================
# Pending connect codes
# { code: { user_id: str, xid: str, created_at: float } }
# =====================
_pending_connects = {}

# Cleanup codes older than 10 minutes
def cleanup_old_codes():
    now = time.time()
    expired = [k for k, v in _pending_connects.items() if now - v["created_at"] > 600]
    for k in expired:
        del _pending_connects[k]

# =========================================================
# 🛡️ BOT READY HELPERS
# =========================================================
def bot_is_ready():
    return _state["ready"].is_set() and _state["loop"] is not None

def wait_for_bot(timeout=30):
    logger.info(f"[WAIT] Waiting for bot ready event (timeout={timeout}s)...")
    result = _state["ready"].wait(timeout=timeout)
    logger.info(f"[WAIT] Bot ready event result: {result}")
    return result

# =========================================================
# 🧠 CHANNEL CHECK LOGIC
# =========================================================
def resolve_channel_id(channel: str) -> str:
    ch = channel.strip()
    if ch.lstrip('-').isdigit():
        return ch
    if ch.startswith('@'):
        return ch
    if not ch.startswith('http') and not ch.startswith('t.me'):
        return f"@{ch}"
    if 't.me/' in ch and '/+' not in ch:
        username = ch.split('t.me/')[1].strip('/')
        return f"@{username}"
    return None

async def check_user_in_channel(user_id: int, channel: str):
    chat_id = resolve_channel_id(channel)
    if chat_id is None:
        logger.warning(f"Cannot check private invite link: {channel}. Use numeric chat ID instead.")
        return "bot_not_admin"
    try:
        member = await _state["app"].bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id
        )
        if member.status in ["member", "administrator", "creator"]:
            return "joined"
        return "not_joined"
    except Exception as e:
        err = str(e).lower()
        if (
            "chat not found" in err or
            "not enough rights" in err or
            "have no rights" in err or
            "forbidden" in err or
            "bot is not a member" in err or
            "user not found" in err
        ):
            logger.warning(f"Bot lacks access to {channel}: {e}")
            return "bot_not_admin"
        logger.warning(f"Channel check failed for {channel}: {e}")
        return "not_joined"

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

async def get_chat_id_async(invite_link: str):
    try:
        chat = await _state["app"].bot.get_chat(invite_link)
        return {"ok": True, "chat_id": chat.id, "title": chat.title}
    except Exception as e:
        logger.error(f"get_chat_id failed for {invite_link}: {e}")
        return {"ok": False, "error": str(e)}

# =========================================================
# 💾 SAVE TELEGRAM ID TO SUPABASE
# =========================================================
async def save_telegram_id_to_supabase(user_id: str, telegram_id: int):
    """Save telegram_id to profiles table via Supabase REST API."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("Supabase env vars not set, cannot save telegram_id")
        return False
    try:
        import urllib.request
        import json as _json
        url = f"{SUPABASE_URL}/rest/v1/profiles?user_id=eq.{user_id}"
        payload = _json.dumps({"telegram_id": str(telegram_id)}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            method="PATCH",
            headers={
                "Content-Type": "application/json",
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Prefer": "return=minimal"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Saved telegram_id {telegram_id} for user {user_id}, status: {resp.status}")
            return resp.status in [200, 204]
    except Exception as e:
        logger.error(f"Failed to save telegram_id: {e}")
        return False

# =========================================================
# 🤖 TELEGRAM COMMANDS
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args  # payload after /start

    # ── Handle connect flow ──────────────────────────────
    if args and len(args) > 0:
        code = args[0]
        cleanup_old_codes()

        if code in _pending_connects:
            pending = _pending_connects.pop(code)
            website_user_id = pending["user_id"]
            xid = pending.get("xid", "User")

            # Save telegram_id to Supabase
            saved = await save_telegram_id_to_supabase(website_user_id, user.id)

            if saved:
                await update.message.reply_text(
                    f"✅ Connected Successfully!\n\n"
                    f"👤 Account: {xid}\n"
                    f"🆔 Telegram ID: {user.id}\n\n"
                    f"You'll now receive alerts for:\n"
                    f"• 💰 Transactions\n"
                    f"• 📥 Fund request updates\n"
                    f"• 📤 Withdrawal updates\n"
                    f"• 🎉 Lifafa wins\n\n"
                    f"Welcome to Xeo Wallet! 🚀",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
                    ]])
                )
            else:
                await update.message.reply_text(
                    "⚠️ Connection failed. Please try again from the wallet.",
                    parse_mode="HTML"
                )
            return
        else:
            await update.message.reply_text(
                "⚠️ This connect link has expired or already been used.\n"
                "Please generate a new one from your wallet dashboard.",
                parse_mode="HTML"
            )
            return

    # ── Normal /start ────────────────────────────────────
    msg = (
        f"👋 Hello {user.first_name}!\n\n"
        "Welcome to Xeo Wallet Bot. 💼\n"
        "You will receive notifications for all your wallet transactions here.\n\n"
        "Use /help to see available commands."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
    ]])
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📝 Available Commands:\n\n"
        "• /start - Start the bot\n"
        "• /help - Show this help message\n"
        "• /id - Get your Telegram ID\n"
        "• /chatid - Get a channel's numeric chat ID (forward any message from the channel)\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 Channel: @Xeo_Wallet\n"
        "👨‍💻 Developer: @Gamenter\n"
        "🤖 Bot: @XeoWalletBot\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "💡 All wallet transactions will be notified automatically here."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
    ]])
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"{update.effective_user.id}",
        parse_mode="HTML"
    )

async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.forward_from_chat:
        chat = msg.forward_from_chat
        await msg.reply_text(
            f"📢 Channel: {chat.title}\n"
            f"🆔 Chat ID: `{chat.id}`\n\n"
            f"Use this numeric ID when adding a private channel in Xeo Wallet.",
            parse_mode="HTML"
        )
    else:
        await msg.reply_text(
            "Forward any message from your private channel to get its Chat ID.",
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
            elif t_type.lower() == "lifafa_win":
                type_emoji = "🎉"
            elif t_type.lower() == "lifafa_create":
                type_emoji = "🧧"
            elif t_type.lower() == "lifafa_refund":
                type_emoji = "↩️"
            else:
                type_emoji = "⭐"
            status_emoji = "✅"
        else:
            status_emoji = "❌"
            type_emoji = "⚠️"

        msg = (
            f"💰 Transaction Alert!\n\n"
            f"{type_emoji} Type: {t_type}\n"
            f"💵 Amount: ₹{amount}\n"
            f"{status_emoji} Status: {status}\n"
            f"👤 Sender: {sender}\n"
            f"💬 Comment: {comment}\n\n"
            f"💼 New Balance: ₹{balance}"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
        ]])

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
# 📣 ADMIN NOTIFICATION
# =========================================================
async def send_admin_message_async(message: str):
    try:
        await _state["app"].bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=message,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔧 Admin Panel", url=f"{MINIAPP_URL}/admin")
            ]])
        )
        return True
    except Exception as e:
        logger.error(f"Error sending admin message: {e}")
        return False

def send_admin_message(message: str):
    try:
        future = asyncio.run_coroutine_threadsafe(
            send_admin_message_async(message),
            _state["loop"]
        )
        return future.result(timeout=15)
    except Exception as e:
        logger.error(f"Failed to send admin message: {e}")
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

@app.route("/admin", methods=["POST"])
def admin_alert():
    """
    Send a message directly to the admin Telegram.
    Usage: POST { "message": "New add fund request!" }
    """
    if not wait_for_bot():
        return jsonify({"error": "Bot failed to start"}), 503

    data = request.json
    if not data or not data.get("message"):
        return jsonify({"error": "Missing message"}), 400

    message = data["message"]

    Thread(
        target=send_admin_message,
        args=(message,),
        daemon=True
    ).start()
    return jsonify({"ok": True})

@app.route("/check-id", methods=["POST"])
def check_id():
    """
    Generate a unique connect code and return a t.me link.
    Usage: POST { "user_id": "...", "xid": "..." }
    Returns: { "link": "https://t.me/XeoWalletBot?start=CODE", "code": "CODE" }
    """
    if not wait_for_bot():
        return jsonify({"error": "Bot failed to start"}), 503

    data = request.json
    if not data or not data.get("user_id"):
        return jsonify({"error": "Missing user_id"}), 400

    cleanup_old_codes()

    # Generate a unique code
    code = secrets.token_urlsafe(16)
    _pending_connects[code] = {
        "user_id": data["user_id"],
        "xid": data.get("xid", "User"),
        "created_at": time.time()
    }

    link = f"https://t.me/XeoWalletBot?start={code}"
    logger.info(f"Generated connect link for user {data['user_id']}: {link}")
    return jsonify({"ok": True, "link": link, "code": code})

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
            verify_user_channels(int(user_id), channels),
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

@app.route("/resolve_chat_id", methods=["POST"])
def resolve_chat_id():
    if not wait_for_bot():
        return jsonify({"error": "Bot failed to start"}), 503

    data = request.json
    invite_link = data.get("invite_link")
    if not invite_link:
        return jsonify({"error": "Missing invite_link"}), 400

    try:
        future = asyncio.run_coroutine_threadsafe(
            get_chat_id_async(invite_link),
            _state["loop"]
        )
        result = future.result(timeout=15)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================================================
# 🚀 RUN TELEGRAM BOT
# =========================================================
async def run_bot_async():
    logger.info("[BOT] Building application...")
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_cmd))
    telegram_app.add_handler(CommandHandler("id", id_cmd))
    telegram_app.add_handler(CommandHandler("chatid", chatid_cmd))

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
    logger.info(f"✅ [BOT] Ready!")

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
# 🎮 GAME ROUND MANAGER
# =========================================================
import random
import json as _json
import urllib.request
import urllib.error

_round_state = {
    "current_round_id": None,
    "running": False,
}

def supabase_request(method, path, data=None):
    """Make a request to Supabase REST API."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("Supabase env vars missing")
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    payload = _json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Content-Type": "application/json",
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Prefer": "return=representation",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return _json.loads(body) if body else []
    except Exception as e:
        logger.error(f"Supabase request failed ({method} {path}): {e}")
        return None

def supabase_rpc(func_name, params):
    """Call a Supabase RPC function."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/rpc/{func_name}"
    payload = _json.dumps(params).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return _json.loads(body) if body else None
    except Exception as e:
        logger.error(f"Supabase RPC {func_name} failed: {e}")
        return None

def create_new_round():
    """Create a new betting round in Supabase."""
    from datetime import datetime, timezone, timedelta
    ends_at = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    data = supabase_request("POST", "game_rounds", {
        "game": "big_small",
        "status": "betting",
        "ends_at": ends_at,
    })
    if data and len(data) > 0:
        round_id = data[0]["id"]
        logger.info(f"[GAME] New round created: {round_id} ends_at: {ends_at}")
        return round_id
    logger.error(f"[GAME] Failed to create round, response: {data}")
    return None

def resolve_round(round_id):
    """Pick a result and resolve all bets for this round."""
    result = random.choice(["big", "small"])
    logger.info(f"[GAME] Resolving round {round_id} -> {result}")
    data = supabase_rpc("resolve_round", {
        "p_round_id": round_id,
        "p_result": result,
    })
    if data and data.get("ok"):
        logger.info(f"[GAME] Round {round_id} resolved: {result}")
    else:
        logger.error(f"[GAME] Failed to resolve round {round_id}: {data}")
    return result

def game_round_loop():
    """
    Infinite loop that manages game rounds:
    - 10s betting window
    - 3s result display
    - repeat
    """
    logger.info("[GAME] Round manager starting, waiting for bot to be ready...")
    _state["ready"].wait(timeout=60)
    logger.info("[GAME] Round manager started!")
    _round_state["running"] = True

    while _round_state["running"]:
        try:
            # Create new round
            round_id = create_new_round()
            if not round_id:
                logger.error("[GAME] Failed to create round, retrying in 3s")
                time.sleep(3)
                continue

            _round_state["current_round_id"] = round_id
            logger.info(f"[GAME] Betting open for 10s (round {round_id})")

            # Betting window: 10 seconds
            time.sleep(10)

            # Resolve round immediately
            logger.info(f"[GAME] Resolving round {round_id}...")
            result = resolve_round(round_id)
            logger.info(f"[GAME] Round resolved: {result}")

            # Result display: 3 seconds then next round
            time.sleep(3)

        except Exception as e:
            logger.error(f"[GAME] Round loop error: {e}")
            time.sleep(3)

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

# Start game round manager
game_thread = Thread(target=game_round_loop, daemon=True)
game_thread.start()
logger.info("[MAIN] Game round manager launched.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
