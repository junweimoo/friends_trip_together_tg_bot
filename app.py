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

from database import init_db, upsert_user
from pay import (
    start_pay, select_payer, enter_comment, enter_amount, select_currency, select_payee,
    select_consumer_for_split, enter_consumer_amount, cancel, undo_pay,
    SELECT_PAYER, ENTER_COMMENT, ENTER_AMOUNT, SELECT_CURRENCY, SELECT_PAYEE,
    SELECT_CONSUMER_FOR_SPLIT, ENTER_CONSUMER_AMOUNT
)
from settle import (
    start_settle, select_settle_currency, store_rate,
    SELECT_SETTLE_CURRENCY, ENTER_RATE
)
from users import register
from list import list_settlements

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
    reply_lines.append("/register - Register in this group")
    reply_lines.append("/pay - Record a new payment (supports detailed splits!)")
    reply_lines.append("/list - Show transaction history and net balances")
    reply_lines.append("/settle - Show the most efficient way to pay everyone back")
    reply_lines.append("/undo - Remove the last transaction recorded in this chat")
    reply_lines.append("/cancel - Cancel an ongoing transaction")

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text='\n'.join(reply_lines)
    )

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
            # Core Flow: Payer -> Comment -> Amount -> Currency -> Payee Type
            SELECT_PAYER: [CallbackQueryHandler(select_payer)],
            ENTER_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_comment)],
            ENTER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount)],
            SELECT_CURRENCY: [CallbackQueryHandler(select_currency)],

            # Branching Step: Payee Selection (triggers simple save or detailed split)
            SELECT_PAYEE: [CallbackQueryHandler(select_payee)],

            # Detailed Split Loop:
            SELECT_CONSUMER_FOR_SPLIT: [CallbackQueryHandler(select_consumer_for_split)],
            ENTER_CONSUMER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_consumer_amount)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(pay_handler)

    settle_handler = ConversationHandler(
        entry_points=[CommandHandler('settle', start_settle)],
        states={
            SELECT_SETTLE_CURRENCY: [CallbackQueryHandler(select_settle_currency)],
            ENTER_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, store_rate)],
        },
        fallbacks =[CommandHandler('cancel', cancel)]
    )
    application.add_handler(settle_handler)

    application.add_handler(CommandHandler('undo', undo_pay))
    application.add_handler(CommandHandler('list', list_settlements))
    application.add_handler(CommandHandler('register', register))
    application.add_handler(CommandHandler('help', help))

    print("Bot is starting...")
    application.run_polling()