import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters
)

# Import local modules
from database import init_db, upsert_user
# Import the new payment handlers and states
from pay import (
    start_pay, select_payer, select_payee, enter_amount, select_currency, enter_comment, cancel,
    SELECT_PAYER, SELECT_PAYEE, ENTER_AMOUNT, SELECT_CURRENCY, ENTER_COMMENT
)
from settle import list_settlements
from simplify import suggest_settlements

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
DB_URL = os.getenv('DATABASE_URL', 'postgresql+asyncpg://bot_admin:your_secure_password@localhost/settlement_bot')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


async def help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_first_name = update.effective_user.first_name

    reply_lines = [f"Hello, {user_first_name}!"]
    reply_lines.append("/register {your name} - Register in this group")
    reply_lines.append("/pay - Record a new payment (supports detailed splits!)")
    reply_lines.append("/list - Show transaction history and net balances")
    reply_lines.append("/settle - Show the most efficient way to pay everyone back")

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text='\n'.join(reply_lines)
    )

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registers the user into the database."""
    if not context.args:
        await update.message.reply_text("Usage: /register {username}")
        return

    username = " ".join(context.args)
    if not (2 <= len(username) <= 20) or not username.replace(" ", "").isalpha():
        await update.message.reply_text("Error: Username must be 2-20 letters/spaces.")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    try:
        await upsert_user(user_id, chat_id, thread_id, username)
        await update.message.reply_text(f"Success! Registered as: {username}")

    except Exception as e:
        logging.error(f"Registration error: {e}")
        await update.message.reply_text("Error registering.")

async def post_init(application):
    print("Initializing database...")
    await init_db(DB_URL)

if __name__ == '__main__':
    if not TOKEN:
        print("Error: BOT_TOKEN missing.")
        exit(1)

    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    pay_handler = ConversationHandler(
        entry_points=[CommandHandler('pay', start_pay)],
        states={
            # Corrected Flow: Payer -> Recipient -> Amount -> Currency -> Comment
            SELECT_PAYER: [CallbackQueryHandler(select_payer)],
            SELECT_PAYEE: [CallbackQueryHandler(select_payee)],
            ENTER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount)],
            SELECT_CURRENCY: [CallbackQueryHandler(select_currency)],
            ENTER_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_comment)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(pay_handler)
    application.add_handler(CommandHandler('list', list_settlements))
    application.add_handler(CommandHandler('settle', suggest_settlements))
    application.add_handler(CommandHandler('register', register))
    application.add_handler(CommandHandler('help', help))

    print("Bot is starting...")
    application.run_polling()