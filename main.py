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
# Global Bot Application & Bot Instance
# =====================
telegram_app = None
bot_instance = Bot(token=BOT_TOKEN)

# =====================
# Telegram Commands
# =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command"""
    user = update.effective_user
    msg = (
        f"👋 <b>Hello {user.first_name}!</b>\n\n"
        "Welcome to <b>Xeo Wallet Bot</b>. 💼\n"
        "You will receive notifications for all your wallet transactions here.\n\n"
        "Use /help to see available commands."
    )
    
    # Create keyboard with Open Wallet button that opens mini app
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 Open Wallet", web_app={"url": "https://xeow.vercel.app/dashboard"})]
    ])
    
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
    logger.info(f"User {user.id} started the bot")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command"""
    msg = (
        "📝 <b>Available Commands:</b>\n\n"
        "• /start - Start the bot\n"
        "• /help - Show this help message\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 <b>Channel:</b> @Xeo_Wallet\n"
        "👨‍💻 <b>Developer:</b> @Gamenter\n"
        "🤖 <b>Bot:</b> @XeoWalletBot\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "💡 All wallet transactions will be notified automatically here."
    )
    
    # Create keyboard with Open Wallet button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 Open Wallet", web_app={"url": "https://xeow.vercel.app/dashboard"})]
    ])
    
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
    logger.info(f"User {update.effective_user.id} requested help")

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command"""
    msg = f"<b>{update.effective_user.id}</b>"
    
    await update.message.reply_text(msg, parse_mode="HTML")
    logger.info(f"User {update.effective_user.id} want to see his id!")
    
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
        # Determine emoji based on transaction type and status
        if status.lower() == "success":
            if t_type.lower() == "send_credit":
                status_emoji = "✅"
                type_emoji = "🏧"
            elif t_type.lower() == "api_debit":
                status_emoji = "✅"
                type_emoji = "🏧"
            elif t_type.lower() == "addfund":
                status_emoji = "✅"
                type_emoji = "📥"
            elif t_type.lower() == "withdraw":
                status_emoji = "✅"
                type_emoji = "📤"
            else:
                status_emoji = "✅"
                type_emoji = "⭐"
        else:
            status_emoji = "❌"
            type_emoji = "⚠️"
        
        # Formatted message with HTML
        msg = (
            f"💰 <b>Transaction Alert!</b>\n\n"
            f"{type_emoji} <b>Type:</b> {t_type}\n"
            f"💵 <b>Amount:</b> ₹{amount}\n"
            f"{status_emoji} <b>Status:</b> {status}\n"
            f"👤 <b>Sender:</b> {sender}\n"
            f"💬 <b>Comment:</b> {comment}\n\n"
            f"💼 <b>New Balance:</b> ₹{balance}\n"
        )
        
        # Inline button to open mini app
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💼 Open Wallet", web_app={"url": "https://xeow.vercel.app/dashboard"})]
        ])
        
        # Send the message using the shared bot instance
        await bot_instance.send_message(
            chat_id=user_id,
            text=msg,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        logger.info(f"Transaction notification sent to user {user_id}")
        return True
            
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
async def run_bot_async():
    """Run the bot with async/await"""
    global telegram_app
    
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add command handlers
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_cmd))
    telegram_app.add_handler(CommandHandler("id", id_cmd))
    
    logger.info("Starting Telegram bot polling...")
    
    # Initialize and start polling
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    # Keep the bot running
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping bot...")
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()

def run_telegram_bot():
    """Initialize and run the Telegram bot in a new event loop"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_bot_async())
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
