from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
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
import imaplib
import email
import re
from email.header import decode_header
from threading import Thread
from datetime import datetime, timezone, timedelta

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
ADMIN_GROUP_ID = "-1002437040999"

# Gmail config
GMAIL_USER = "circuitsaga@gmail.com"
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "xrleigmqdnfibmsb")
FAMAPP_SENDER = "no-reply@famapp.in"

# Supabase config
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
# =====================
_pending_connects = {}

# =====================
# Active deposit sessions
# =====================
_deposit_sessions = {}

# =====================
# Pending transfers
# =====================
_pending_transfers = {}

def cleanup_old_codes():
    now = time.time()
    expired = [k for k, v in _pending_connects.items() if now - v["created_at"] > 600]
    for k in expired:
        del _pending_connects[k]

def bot_is_ready():
    return _state["ready"].is_set() and _state["loop"] is not None

def wait_for_bot(timeout=30):
    return _state["ready"].wait(timeout=timeout)

# =========================================================
# 💾 SUPABASE HELPERS
# =========================================================
import json as _json
import urllib.request
import urllib.error

def supabase_request(method, path, data=None, params=None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.error("Supabase env vars missing")
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
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

async def save_telegram_id_to_supabase(user_id: str, telegram_id: int):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return False
    try:
        url = f"{SUPABASE_URL}/rest/v1/profiles?user_id=eq.{user_id}"
        payload = _json.dumps({"telegram_id": str(telegram_id)}).encode()
        req = urllib.request.Request(
            url, data=payload, method="PATCH",
            headers={
                "Content-Type": "application/json",
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Prefer": "return=minimal"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in [200, 204]
    except Exception as e:
        logger.error(f"Failed to save telegram_id: {e}")
        return False

# =========================================================
# 📧 GMAIL WATCHER
# =========================================================
def fetch_recent_famapp_emails(since_minutes=6):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")
        since_date = (datetime.now() - timedelta(minutes=since_minutes)).strftime("%d-%b-%Y")
        _, message_ids = mail.search(None, f'(FROM "{FAMAPP_SENDER}" SINCE "{since_date}" UNSEEN)')
        emails = []
        for msg_id in message_ids[0].split():
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = ""
            raw_subject = msg.get("Subject", "")
            decoded = decode_header(raw_subject)
            for part, enc in decoded:
                if isinstance(part, bytes):
                    subject += part.decode(enc or "utf-8", errors="ignore")
                else:
                    subject += part
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    elif part.get_content_type() == "text/html":
                        body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            emails.append({"id": msg_id, "subject": subject, "body": body})
        mail.logout()
        return emails
    except Exception as e:
        logger.error(f"Gmail fetch error: {e}")
        return []

def parse_payment_email(subject, body):
    amount = None
    sender_name = None
    amt_match = re.search(r'₹([\d.]+)', subject)
    if not amt_match:
        amt_match = re.search(r'Rs\.?\s*([\d.]+)', subject, re.IGNORECASE)
    if amt_match:
        amount = float(amt_match.group(1))
    sender_match = re.search(r'from\s+([A-Z][A-Z\s]+)', body)
    if sender_match:
        sender_name = sender_match.group(1).strip()
    return amount, sender_name

def check_emails_for_sessions():
    if not _deposit_sessions:
        return
    emails = fetch_recent_famapp_emails(since_minutes=6)
    if not emails:
        return
    for email_data in emails:
        amount, sender_name = parse_payment_email(email_data["subject"], email_data["body"])
        if not amount:
            continue
        logger.info(f"[EMAIL] Found payment: ₹{amount} from {sender_name}")
        for request_id, session in list(_deposit_sessions.items()):
            if session.get("matched"):
                continue
            if abs(float(session["amount"]) - amount) < 0.5:
                session["matched"] = True
                session["sender_name"] = sender_name or "Unknown"
                logger.info(f"[EMAIL] Matched session {request_id} for ₹{amount}")
                future = asyncio.run_coroutine_threadsafe(
                    send_admin_approval_request(request_id, session, amount, sender_name),
                    _state["loop"]
                )
                try:
                    future.result(timeout=15)
                except Exception as e:
                    logger.error(f"Failed to send admin approval: {e}")
                break

async def send_admin_approval_request(request_id, session, amount, sender_name):
    bonus = round(amount * 0.01, 2)
    total = amount + bonus
    msg = (
        f"💰 Payment Detected!\n\n"
        f"👤 User: {session['xid']}\n"
        f"💵 Amount: ₹{amount}\n"
        f"👨 Sender: {sender_name or 'Unknown'}\n"
        f"🎁 Bonus: +₹{bonus}\n"
        f"✅ Total to credit: ₹{total}\n\n"
        f"Approve this deposit?"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{request_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"decline_{request_id}"),
    ]])
    await _state["app"].bot.send_message(
        chat_id=ADMIN_GROUP_ID, text=msg, parse_mode="HTML", reply_markup=keyboard
    )

# =========================================================
# 🔘 CALLBACK HANDLER
# =========================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data:
        return
    if data.startswith("approve_"):
        await handle_approve(query, data.replace("approve_", ""))
    elif data.startswith("decline_"):
        await handle_decline(query, data.replace("decline_", ""))
    elif data.startswith("transfer_confirm_"):
        await handle_transfer_confirm(query, data.replace("transfer_confirm_", ""))
    elif data.startswith("transfer_cancel_"):
        await handle_transfer_cancel(query, data.replace("transfer_cancel_", ""))

async def handle_approve(query, request_id):
    session = _deposit_sessions.get(request_id)
    if not session:
        await query.edit_message_text("⚠️ Session expired or not found.")
        return
    amount = float(session["amount"])
    bonus = round(amount * 0.01, 2)
    total = amount + bonus
    user_id = session["user_id"]
    xid = session["xid"]
    telegram_id = session.get("telegram_id")
    try:
        profiles = supabase_request("GET", "profiles", params={"user_id": f"eq.{user_id}", "select": "balance"})
        if not profiles:
            await query.edit_message_text("⚠️ User profile not found.")
            return
        current_balance = float(profiles[0]["balance"])
        new_balance = current_balance + total
        supabase_request("PATCH", f"profiles?user_id=eq.{user_id}", {"balance": new_balance})
        supabase_request("PATCH", f"add_fund_requests?id=eq.{request_id}", {"status": "approved"})
        supabase_request("POST", "transactions", {
            "user_id": user_id,
            "type": "addfund",
            "amount": total,
            "status": "completed",
            "description": f"Add fund approved ₹{amount} + ₹{bonus} bonus",
        })
        if telegram_id:
            try:
                await _state["app"].bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        f"✅ Deposit Approved!\n\n"
                        f"💰 Amount: ₹{amount}\n"
                        f"🎁 Bonus: +₹{bonus}\n"
                        f"✅ Total credited: ₹{total}\n"
                        f"💼 New Balance: ₹{new_balance:.2f}\n\n"
                        f"Thank you for using Xeo Wallet! 🚀"
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
                    ]])
                )
            except Exception as e:
                logger.error(f"Failed to notify user: {e}")
        await query.edit_message_text(
            f"✅ Approved!\n\n"
            f"👤 {xid}\n"
            f"💵 ₹{total} credited (₹{amount} + ₹{bonus} bonus)\n"
            f"💼 New Balance: ₹{new_balance:.2f}"
        )
        _deposit_sessions.pop(request_id, None)
    except Exception as e:
        logger.error(f"Approve error: {e}")
        await query.edit_message_text(f"⚠️ Error approving: {e}")

async def handle_decline(query, request_id):
    session = _deposit_sessions.get(request_id)
    if not session:
        await query.edit_message_text("⚠️ Session expired or not found.")
        return
    xid = session["xid"]
    amount = session["amount"]
    telegram_id = session.get("telegram_id")
    supabase_request("PATCH", f"add_fund_requests?id=eq.{request_id}", {"status": "declined"})
    if telegram_id:
        try:
            await _state["app"].bot.send_message(
                chat_id=telegram_id,
                text=(
                    f"❌ Deposit Declined\n\n"
                    f"💵 Amount: ₹{amount}\n\n"
                    f"Your deposit request was declined. "
                    f"Please contact support if you believe this is an error."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
                ]])
            )
        except Exception as e:
            logger.error(f"Failed to notify user of decline: {e}")
    await query.edit_message_text(
        f"❌ Declined\n\n"
        f"👤 {xid}\n"
        f"💵 ₹{amount} — request declined"
    )
    _deposit_sessions.pop(request_id, None)

# =========================================================
# 💸 /send COMMAND
# =========================================================
async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if not args or len(args) < 2:
        await update.message.reply_text(
            "❌ Usage: /send <XID/Mobile/Username> <Amount>\n\n"
            "Examples:\n"
            "• /send john@x 50\n"
            "• /send 9876543210 100\n"
            "• /send johndoe 25"
        )
        return

    target = args[0].strip()
    try:
        amount = float(args[1].strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return

    if amount < 1:
        await update.message.reply_text("❌ Minimum send amount is ₹1.")
        return

    # Check sender is linked
    sender_profiles = supabase_request("GET", "profiles", params={
        "telegram_id": f"eq.{user.id}",
        "select": "user_id,username,xid,balance,is_frozen,is_banned",
    })

    if not sender_profiles:
        await update.message.reply_text(
            "❌ Your Telegram is not linked to any XeoWallet account.\n\n"
            "Go to your wallet and link your Telegram first.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
            ]])
        )
        return

    sender = sender_profiles[0]

    if sender.get("is_frozen") or sender.get("is_banned"):
        await update.message.reply_text("❌ Your account is restricted.")
        return

    if float(sender["balance"]) < amount:
        await update.message.reply_text(
            f"❌ Insufficient balance.\n\n"
            f"💼 Your balance: ₹{float(sender['balance']):.2f}\n"
            f"💸 Amount: ₹{amount:.2f}"
        )
        return

    # Find recipient by xid, mobile, or username
    recipient_profiles = supabase_request("GET", "profiles", params={
        "or": f"(xid.eq.{target},mobile.eq.{target},username.eq.{target})",
        "select": "user_id,username,xid,balance,telegram_id,is_frozen,is_banned",
    })

    if not recipient_profiles:
        await update.message.reply_text(f"❌ User '{target}' not found.")
        return

    recipient = recipient_profiles[0]

    if recipient["user_id"] == sender["user_id"]:
        await update.message.reply_text("❌ You cannot send money to yourself.")
        return

    if recipient.get("is_frozen") or recipient.get("is_banned"):
        await update.message.reply_text("❌ Recipient account is restricted.")
        return

    # Store pending transfer
    transfer_code = secrets.token_urlsafe(8)
    _pending_transfers[transfer_code] = {
        "sender_user_id": sender["user_id"],
        "sender_telegram_id": str(user.id),
        "sender_xid": sender["xid"],
        "sender_username": sender["username"],
        "recipient_user_id": recipient["user_id"],
        "recipient_xid": recipient["xid"],
        "recipient_username": recipient["username"],
        "recipient_telegram_id": recipient.get("telegram_id"),
        "amount": amount,
        "created_at": time.time(),
    }

    await update.message.reply_text(
        f"💸 Confirm Transfer\n\n"
        f"👤 To: {recipient['username']} ({recipient['xid']})\n"
        f"💵 Amount: ₹{amount:.2f}\n"
        f"💼 Your balance after: ₹{float(sender['balance']) - amount:.2f}\n\n"
        f"Are you sure?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"transfer_confirm_{transfer_code}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"transfer_cancel_{transfer_code}"),
        ]])
    )

async def handle_transfer_confirm(query, transfer_code):
    transfer = _pending_transfers.get(transfer_code)
    if not transfer:
        await query.edit_message_text("⚠️ Transfer expired. Please try again.")
        return

    if str(query.from_user.id) != transfer["sender_telegram_id"]:
        await query.answer("❌ This is not your transfer.", show_alert=True)
        return

    amount = transfer["amount"]

    try:
        # Get fresh sender balance
        sender_data = supabase_request("GET", "profiles", params={
            "user_id": f"eq.{transfer['sender_user_id']}",
            "select": "balance",
        })
        if not sender_data or float(sender_data[0]["balance"]) < amount:
            await query.edit_message_text("❌ Insufficient balance.")
            _pending_transfers.pop(transfer_code, None)
            return

        sender_balance = float(sender_data[0]["balance"])

        # Get fresh recipient balance
        recipient_data = supabase_request("GET", "profiles", params={
            "user_id": f"eq.{transfer['recipient_user_id']}",
            "select": "balance",
        })
        recipient_balance = float(recipient_data[0]["balance"])

        # Deduct sender
        supabase_request("PATCH", f"profiles?user_id=eq.{transfer['sender_user_id']}",
            {"balance": sender_balance - amount})

        # Credit recipient
        supabase_request("PATCH", f"profiles?user_id=eq.{transfer['recipient_user_id']}",
            {"balance": recipient_balance + amount})

        # Record sender transaction
        supabase_request("POST", "transactions", {
            "user_id": transfer["sender_user_id"],
            "type": "send",
            "amount": amount,
            "status": "completed",
            "description": f"Sent ₹{amount} to {transfer['recipient_xid']} via Telegram",
        })

        # Record recipient transaction
        supabase_request("POST", "transactions", {
            "user_id": transfer["recipient_user_id"],
            "type": "receive",
            "amount": amount,
            "status": "completed",
            "description": f"Received ₹{amount} from {transfer['sender_xid']} via Telegram",
        })

        # Confirm to sender
        await query.edit_message_text(
            f"✅ Transfer Successful!\n\n"
            f"💸 Sent ₹{amount:.2f} to {transfer['recipient_username']} ({transfer['recipient_xid']})\n"
            f"💼 New Balance: ₹{sender_balance - amount:.2f}"
        )

        # Notify recipient if connected
        if transfer.get("recipient_telegram_id"):
            try:
                await _state["app"].bot.send_message(
                    chat_id=transfer["recipient_telegram_id"],
                    text=(
                        f"💰 Money Received!\n\n"
                        f"👤 From: {transfer['sender_username']} ({transfer['sender_xid']})\n"
                        f"💵 Amount: ₹{amount:.2f}\n"
                        f"💼 New Balance: ₹{recipient_balance + amount:.2f}"
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
                    ]])
                )
            except Exception as e:
                logger.error(f"Failed to notify recipient: {e}")

        _pending_transfers.pop(transfer_code, None)

    except Exception as e:
        logger.error(f"Transfer error: {e}")
        await query.edit_message_text(f"❌ Transfer failed: {e}")

async def handle_transfer_cancel(query, transfer_code):
    transfer = _pending_transfers.get(transfer_code)
    if not transfer:
        await query.edit_message_text("⚠️ Transfer already expired.")
        return
    if str(query.from_user.id) != transfer["sender_telegram_id"]:
        await query.answer("❌ This is not your transfer.", show_alert=True)
        return
    _pending_transfers.pop(transfer_code, None)
    await query.edit_message_text("❌ Transfer cancelled.")

# =========================================================
# ⏱️ EMAIL WATCHER LOOP
# =========================================================
def email_watcher_loop():
    logger.info("[EMAIL] Watcher starting...")
    _state["ready"].wait(timeout=60)
    logger.info("[EMAIL] Watcher started!")

    while True:
        try:
            now = time.time()
            expired = [k for k, v in list(_deposit_sessions.items())
                      if now > v.get("expires_at", 0) and not v.get("matched")]
            for k in expired:
                logger.info(f"[EMAIL] Session {k} expired without match")
                _deposit_sessions.pop(k, None)

            # Clean expired transfers (older than 5 minutes)
            expired_transfers = [k for k, v in list(_pending_transfers.items())
                                if time.time() - v["created_at"] > 300]
            for k in expired_transfers:
                _pending_transfers.pop(k, None)

            if _deposit_sessions:
                check_emails_for_sessions()

        except Exception as e:
            logger.error(f"[EMAIL] Watcher error: {e}")

        time.sleep(30)

# =========================================================
# 💤 INACTIVITY FEE MANAGER
# =========================================================
async def send_inactivity_warning_async(telegram_id: str, username: str, balance: float, days_inactive: int):
    try:
        fee = round(balance * 0.30, 2) if balance > 0 else 10
        await _state["app"].bot.send_message(
            chat_id=telegram_id,
            text=(
                f"⚠️ Inactivity Warning!\n\n"
                f"👤 Hi {username},\n"
                f"You've been inactive for {days_inactive} days.\n\n"
                f"💸 In {30 - days_inactive} days, a maintenance fee of "
                f"₹{fee} will be deducted from your balance.\n\n"
                f"💼 Current Balance: ₹{balance:.2f}\n\n"
                f"Stay active to avoid the fee — just make any transaction! 🚀"
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
            ]])
        )
    except Exception as e:
        logger.error(f"Failed to send inactivity warning to {telegram_id}: {e}")

def inactivity_fee_loop():
    logger.info("[INACTIVITY] Manager starting...")
    _state["ready"].wait(timeout=60)
    logger.info("[INACTIVITY] Manager started!")

    while True:
        try:
            logger.info("[INACTIVITY] Running daily check...")
            data = supabase_request("GET", "profiles", params={
                "select": "user_id,username,balance,telegram_id,last_active_at",
                "is_frozen": "eq.false",
                "is_banned": "eq.false",
                "telegram_id": "not.is.null",
            })

            if data:
                now = datetime.now(timezone.utc)
                for user in data:
                    if not user.get("last_active_at"):
                        continue
                    last_active = datetime.fromisoformat(
                        user["last_active_at"].replace("Z", "+00:00")
                    )
                    days_inactive = (now - last_active).days

                    if days_inactive == 25 and user.get("telegram_id"):
                        future = asyncio.run_coroutine_threadsafe(
                            send_inactivity_warning_async(
                                user["telegram_id"],
                                user["username"],
                                float(user["balance"] or 0),
                                days_inactive
                            ),
                            _state["loop"]
                        )
                        try:
                            future.result(timeout=15)
                            logger.info(f"[INACTIVITY] Warned {user['username']}")
                        except Exception as e:
                            logger.error(f"Warning failed: {e}")

            result = supabase_rpc("charge_inactivity_fees", {})
            if result:
                logger.info(f"[INACTIVITY] Charged {result.get('charged_users', 0)} users, total ₹{result.get('total_charged', 0)}")
                if result.get("charged_users", 0) > 0:
                    send_admin_message(
                        f"💤 Inactivity Fees Collected\n\n"
                        f"👥 Users charged: {result['charged_users']}\n"
                        f"💰 Total collected: ₹{result['total_charged']}"
                    )

        except Exception as e:
            logger.error(f"[INACTIVITY] Error: {e}")

        time.sleep(86400)

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
        return "bot_not_admin"
    try:
        member = await _state["app"].bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            return "joined"
        return "not_joined"
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ["chat not found", "not enough rights", "have no rights", "forbidden", "bot is not a member", "user not found"]):
            return "bot_not_admin"
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
        return {"ok": False, "error": str(e)}

# =========================================================
# 🤖 TELEGRAM COMMANDS
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if args and len(args) > 0:
        code = args[0]
        cleanup_old_codes()
        if code in _pending_connects:
            pending = _pending_connects.pop(code)
            website_user_id = pending["user_id"]
            xid = pending.get("xid", "User")
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
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
                    ]])
                )
            else:
                await update.message.reply_text("⚠️ Connection failed. Please try again from the wallet.")
            return
        else:
            await update.message.reply_text("⚠️ This connect link has expired or already been used.")
            return

    msg = (
        f"👋 Hello {user.first_name}!\n\n"
        "Welcome to Xeo Wallet Bot. 💼\n"
        "You will receive notifications for all your wallet transactions here.\n\n"
        "Use /help to see available commands."
    )
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
    ]]))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📝 Available Commands:\n\n"
        "• /start - Start the bot\n"
        "• /help - Show this help message\n"
        "• /id - Get your Telegram ID\n"
        "• /chatid - Get a channel's numeric chat ID\n"
        "• /send <XID/Mobile/Username> <Amount> - Send money\n"
        "• /balance - Check your wallet balance\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 Channel: @Xeo_Wallet\n"
        "👨‍💻 Developer: @Gamenter\n"
        "🤖 Bot: @XeoWalletBot\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "💡 All wallet transactions will be notified automatically here."
    )
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
    ]]))

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"{update.effective_user.id}")

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
        await msg.reply_text("Forward any message from your private channel to get its Chat ID.")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    profiles = supabase_request("GET", "profiles", params={
        "telegram_id": f"eq.{user.id}",
        "select": "username,xid,balance",
    })
    if not profiles:
        await update.message.reply_text(
            "❌ Your Telegram is not linked to any XeoWallet account.\n\n"
            "Go to your wallet and link your Telegram first.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
            ]])
        )
        return
    profile = profiles[0]
    await update.message.reply_text(
        f"💼 Wallet Balance\n\n"
        f"👤 {profile['username']} ({profile['xid']})\n"
        f"💰 Balance: ₹{float(profile['balance']):.2f}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
        ]])
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

        await _state["app"].bot.send_message(
            chat_id=user_id, text=msg, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💼 Open Wallet", web_app={"url": MINIAPP_URL})
            ]])
        )
        return True
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        return False

def send_transaction_notification(data: dict):
    try:
        future = asyncio.run_coroutine_threadsafe(
            send_transaction_notification_async(data), _state["loop"]
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
            chat_id=ADMIN_TELEGRAM_ID, text=message, parse_mode="HTML",
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
            send_admin_message_async(message), _state["loop"]
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
        "active_sessions": len(_deposit_sessions),
        "pending_transfers": len(_pending_transfers),
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
    Thread(target=send_transaction_notification, args=(data,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/test_email", methods=["GET"])
def test_email():
    try:
        emails = fetch_recent_famapp_emails(since_minutes=60)
        results = []
        for e in emails:
            amount, sender = parse_payment_email(e["subject"], e["body"])
            results.append({
                "subject": e["subject"],
                "parsed_amount": amount,
                "parsed_sender": sender,
            })
        return jsonify({
            "ok": True,
            "emails_found": len(emails),
            "active_sessions": list(_deposit_sessions.values()),
            "parsed": results,
        })
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)})

@app.route("/admin", methods=["POST"])
def admin_alert():
    if not wait_for_bot():
        return jsonify({"error": "Bot failed to start"}), 503
    data = request.json
    if not data or not data.get("message"):
        return jsonify({"error": "Missing message"}), 400
    Thread(target=send_admin_message, args=(data["message"],), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/start_deposit_session", methods=["POST"])
def start_deposit_session():
    if not wait_for_bot():
        return jsonify({"error": "Bot failed to start"}), 503
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
    required = ["request_id", "user_id", "xid", "amount"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400
    request_id = data["request_id"]
    _deposit_sessions[request_id] = {
        "user_id": data["user_id"],
        "xid": data["xid"],
        "amount": float(data["amount"]),
        "telegram_id": data.get("telegram_id"),
        "expires_at": time.time() + 300,
        "matched": False,
    }
    logger.info(f"[SESSION] Started deposit session {request_id} for ₹{data['amount']}")
    return jsonify({"ok": True, "expires_in": 300})

@app.route("/check-id", methods=["POST"])
def check_id():
    if not wait_for_bot():
        return jsonify({"error": "Bot failed to start"}), 503
    data = request.json
    if not data or not data.get("user_id"):
        return jsonify({"error": "Missing user_id"}), 400
    cleanup_old_codes()
    code = secrets.token_urlsafe(16)
    _pending_connects[code] = {
        "user_id": data["user_id"],
        "xid": data.get("xid", "User"),
        "created_at": time.time()
    }
    link = f"https://t.me/XeoWalletBot?start={code}"
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
            verify_user_channels(int(user_id), channels), _state["loop"]
        )
        not_joined, bot_missing = future.result(timeout=15)
        if bot_missing:
            return jsonify({
                "ok": False, "bot_error": True,
                "bot_missing_channels": bot_missing,
                "message": "Bot is not admin in some channels"
            })
        return jsonify({"ok": True, "joined": len(not_joined) == 0, "missing_channels": not_joined})
    except Exception as e:
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
            get_chat_id_async(invite_link), _state["loop"]
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
    telegram_app.add_handler(CommandHandler("send", send_cmd))
    telegram_app.add_handler(CommandHandler("balance", balance_cmd))
    telegram_app.add_handler(CallbackQueryHandler(handle_callback))

    await telegram_app.initialize()
    await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    await telegram_app.start()

    _state["app"] = telegram_app
    _state["loop"] = asyncio.get_running_loop()
    _state["ready"].set()
    logger.info("✅ [BOT] Ready!")

    try:
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        stop_event = asyncio.Event()
        await stop_event.wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

def run_telegram_bot():
    asyncio.run(run_bot_async())

# =========================================================
# 🎮 GAME ROUND MANAGER
# =========================================================
import random

_round_state = {
    "current_round_id": None,
    "running": False,
}

def supabase_request_game(method, path, data=None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    payload = _json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url, data=payload, method=method,
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
        logger.error(f"Supabase game request failed ({method} {path}): {e}")
        return None

def create_new_round():
    ends_at = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    data = supabase_request_game("POST", "game_rounds", {
        "game": "big_small",
        "status": "betting",
        "ends_at": ends_at,
    })
    if data and len(data) > 0:
        return data[0]["id"]
    return None

def resolve_round(round_id):
    result = random.choice(["big", "small"])
    supabase_rpc("resolve_round", {"p_round_id": round_id, "p_result": result})
    return result

def game_round_loop():
    logger.info("[GAME] Round manager starting...")
    _state["ready"].wait(timeout=60)
    logger.info("[GAME] Round manager started!")
    _round_state["running"] = True

    while _round_state["running"]:
        try:
            round_id = create_new_round()
            if not round_id:
                time.sleep(3)
                continue
            _round_state["current_round_id"] = round_id
            time.sleep(10)
            resolve_round(round_id)
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
# 🏁 START THREADS
# =========================================================
logger.info("[MAIN] Module loaded, starting threads...")
signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

bot_thread = Thread(target=run_telegram_bot, daemon=True)
bot_thread.start()
logger.info("[MAIN] Bot thread launched.")

game_thread = Thread(target=game_round_loop, daemon=True)
game_thread.start()
logger.info("[MAIN] Game round manager launched.")

inactivity_thread = Thread(target=inactivity_fee_loop, daemon=True)
inactivity_thread.start()
logger.info("[MAIN] Inactivity fee manager launched.")

email_thread = Thread(target=email_watcher_loop, daemon=True)
email_thread.start()
logger.info("[MAIN] Email watcher launched.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
