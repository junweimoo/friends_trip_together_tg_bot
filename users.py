import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session, User, upsert_user

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /register {username}")
        return

    username = " ".join(context.args)

    if not (2 <= len(username) <= 20):
        await update.message.reply_text("Error: Username must be between 2 and 20 characters.")
        return

    if not username.replace(" ", "").isalpha():
         await update.message.reply_text("Error: Username must contain letters and spaces only.")
         return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    try:
        await upsert_user(user_id, chat_id, thread_id, username)
        
        await update.message.reply_text(f"Success! Registered as: {username}")
        logging.info(f"User {user_id} registered as {username}")
        
    except Exception as e:
        logging.error(f"Registration error: {e}")
        await update.message.reply_text("Error registering.")