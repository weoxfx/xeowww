import os
import asyncio
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# =====================
# Environment Variables
# =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")        # Your bot token
BOT_USERNAME = "XeoWalletBot"             # Bot username without @

app = Flask(__name__)

# =====================
# Telegram Handlers
# =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = (
        f"👋 Hello {user.first_name}!\n\n"
        "Welcome to XeoWallet Bot.\n"
        "You will receive notifications for all your wallet transactions here.\n\n"
        "Use /help to see available commands."
    )
    await update.message.reply_text(msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📝 *Available Commands & Contacts:*\n\n"
        "/start - Start the bot\n"
        "/help - Show this message\n\n"
        "*Channel:* XeoWallet\n"
        "*Developer:* @GAMENTER\n\n"
        "All wallet transactions will be notified automatically."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# =====================
# Telegram Application
# =====================
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_cmd))

# =====================
# Telegram Webhook Route
# =====================
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, application.bot)
    asyncio.run(application.process_update(update))
    return "ok", 200

# =====================
# Lovable Transaction Notifications
# =====================
@app.route("/notify_transaction", methods=["POST"])
def notify_transaction():
    data = request.json
    try:
        user_id = data.get("user_id")
        t_type = data.get("type", "N/A")
        amount = data.get("amount", 0)
        status = data.get("status", "Unknown")
        sender = data.get("sender", "N/A")
        comment = data.get("comment", "No comment")
        balance = data.get("balance", 0)

        if not user_id:
            return jsonify({"error": "user_id missing"}), 400

        # Format transaction message
        msg = (
            f"💰 *Transaction Alert!*\n\n"
            f"*Type:* {t_type}\n"
            f"*Amount:* ₹{amount}\n"
            f"*Status:* {status}\n"
            f"*Sender:* {sender}\n"
            f"*Comment:* {comment}\n"
            f"*New Balance:* ₹{balance}"
        )

        # Mini app button
        bot_url = f"tg://resolve?domain={BOT_USERNAME}&start=mini"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("💼 View Wallet", url=bot_url)]]
        )

        # Send notification
        asyncio.run(application.bot.send_message(
            chat_id=user_id,
            text=msg,
            parse_mode="Markdown",
            reply_markup=keyboard
        ))

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =====================
# Run Flask
# =====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
