import os
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
app = Flask(__name__)

# -------------------------------
# Async Command Handlers
# -------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Welcome! This is your bot.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Available commands:\n/start\n/help")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Unknown command.")

# -------------------------------
# Build the Application
# -------------------------------
application = ApplicationBuilder().token(BOT_TOKEN).build()

# Add command handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler(None, unknown_command))  # fallback

# -------------------------------
# Webhook Route
# -------------------------------
@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Receive updates from Telegram and put them into the application queue"""
    data = request.get_json(force=True)
    update = Update.de_json(data, bot)
    application.update_queue.put_nowait(update)
    return "ok", 200

# -------------------------------
# Notify Transaction Endpoint
# -------------------------------
@app.route("/notify_transaction", methods=["POST"])
def notify_transaction():
    """Send a transaction notification to a user"""
    data = request.json
    user_id = data.get("user_id")
    amount = data.get("amount", 0)
    status = data.get("status", "Unknown")

    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    async def send_msg():
        await bot.send_message(user_id, f"💰 Transaction: ₹{amount}\nStatus: {status}")

    # Run async in the event loop
    application.create_task(send_msg())
    return jsonify({"ok": True})

# -------------------------------
# Keep Alive
# -------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
