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

# =====================
# Flask App Setup
# =====================
app = Flask(__name__)

# =====================
# Global Bot Application
# =====================
telegram_app = None

# =====================
# Telegram Commands
# =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command"""
    user = update.effective_user
    msg = (
        f"👋 Hello {user.first_name}!\n\n"
        "Welcome to Xeo Wallet Bot.\n"
        "You will receive notifications for all your wallet transactions here.\n\n"
        "Use /help to see available commands."
    )
    await update.message.reply_text(msg)
    logger.info(f"User {user.id} started the bot")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command"""
    msg = (
        "📝 *Available Commands:*\n"
        "/start - Start the bot\n"
        "/help - Show this help message\n\n"
        "📡 Channel: [Xeo\\_Wallet](https://t.me/Xeo_Wallet)\n"
        "👨‍💻 Developer: @Gamenter\n"
        "🤖 Bot: @XeoWalletBot\n\n"
        "All wallet transactions will be notified automatically here."
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")
    logger.info(f"User {update.effective_user.id} requested help")

# =====================
# Transaction Notification Function
# =====================
async def send_transaction_notification_async(data: dict):
    """Send transaction notification asynchronously"""
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
        # Escape special characters for MarkdownV2
        def escape_md(text):
            special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
            for char in special_chars:
                text = str(text).replace(char, f'\\{char}')
            return text
        
        # Message formatting
        msg = (
            f"💰 *Transaction Alert\\!*\n\n"
            f"*Type:* {escape_md(t_type)}\n"
            f"*Amount:* ₹{escape_md(amount)}\n"
            f"*Status:* {escape_md(status)}\n"
            f"*Sender:* {escape_md(sender)}\n"
            f"*Comment:* {escape_md(comment)}\n"
            f"*New Balance:* ₹{escape_md(balance)}"
        )
        
        # Inline button to view wallet
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💼 View Wallet", url=f"https://t.me/XeoWalletBot?start=wallet_{user_id}")]
        ])
        
        # Send the message using the global bot instance
        if telegram_app:
            await telegram_app.bot.send_message(
                chat_id=user_id,
                text=msg,
                parse_mode="MarkdownV2",
                reply_markup=keyboard
            )
            logger.info(f"Transaction notification sent to user {user_id}")
            return True
        else:
            logger.error("Telegram app not initialized")
            return False
            
    except Exception as e:
        logger.error(f"Error sending notification to user {user_id}: {str(e)}")
        return False

def send_transaction_notification(data: dict):
    """Wrapper to run async notification in event loop"""
    try:
        # Create new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(send_transaction_notification_async(data))
        loop.close()
        return result
    except Exception as e:
        logger.error(f"Error in notification wrapper: {str(e)}")
        return False

# =====================
# Flask Routes
# =====================
@app.route("/", methods=["GET"])
def home():
    """Health check endpoint"""
    return jsonify({
        "status": "online",
        "service": "Xeo Wallet Bot",
        "version": "1.0"
    })

@app.route("/notify_transaction", methods=["POST"])
def notify_transaction():
    """Endpoint to receive transaction notifications from Lovable website"""
    data = request.json
    
    if not data:
        return jsonify({"error": "No data provided"}), 400
    
    # Validate required fields
    required_fields = ["user_id", "type", "amount", "status"]
    missing_fields = [field for field in required_fields if field not in data]
    
    if missing_fields:
        return jsonify({
            "error": f"Missing required fields: {', '.join(missing_fields)}"
        }), 400
    
    try:
        # Send notification in a separate thread to avoid blocking
        Thread(target=send_transaction_notification, args=(data,), daemon=True).start()
        logger.info(f"Transaction notification queued for user {data.get('user_id')}")
        return jsonify({"ok": True, "message": "Notification queued"})
    except Exception as e:
        logger.error(f"Error queueing notification: {str(e)}")
        return jsonify({"error": str(e)}), 500

# =====================
# Run Telegram Bot
# =====================
def run_telegram_bot():
    """Initialize and run the Telegram bot"""
    global telegram_app
    
    try:
        telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
        
        # Add command handlers
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CommandHandler("help", help_cmd))
        
        logger.info("Starting Telegram bot polling...")
        telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Error running Telegram bot: {str(e)}")
        raise

# =====================
# Main Entry Point
# =====================
if __name__ == "__main__":
    # Start Telegram bot in a separate daemon thread
    bot_thread = Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()
    logger.info("Telegram bot thread started")
    
    # Run Flask app
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
