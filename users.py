import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session, User, upsert_user, check_username_exists

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provided_args = " ".join(context.args)
    user_full_name = f"{update.effective_user.first_name or ''} {update.effective_user.last_name or ''}".strip()
    
    username = provided_args if context.args else user_full_name

    if not (1 <= len(username) <= 40):
        await update.message.reply_text("Error: Username must be between 1 and 40 characters.")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    if await check_username_exists(chat_id, thread_id, username): 
        await update.message.reply_text("This username already exists in this chat. Please choose another.")
        return

    try:
        await upsert_user(user_id, chat_id, thread_id, username)
        
        await update.message.reply_text(f"Success! Registered as: {username}")
        logging.info(f"User {user_id} registered as {username}")
        
    except Exception as e:
        logging.error(f"Registration error: {e}")
        await update.message.reply_text("Error registering.")