from telegram import Update

def get_chat_thread_user_id(update: Update) -> (str, str, str):
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    user_id = update.effective_user.id
    return chat_id, thread_id, user_id
