"""
WSGI entry point for production deployment with Gunicorn
"""
import os
import logging
from threading import Thread
from main import app, run_telegram_bot

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Start Telegram bot in background thread when app starts
def start_bot():
    """Start the Telegram bot"""
    logger.info("Starting Telegram bot thread...")
    bot_thread = Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()
    logger.info("Telegram bot thread started")

# Initialize bot when module loads
start_bot()

# Export app for Gunicorn
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
