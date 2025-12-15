from telegram import Update

MAX_MSG_LEN = 100
MAX_LINES = 50

def get_chat_thread_user_id(update: Update) -> (str, str, str):
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    user_id = update.effective_user.id
    return chat_id, thread_id, user_id

def split_message(msg_text: str) -> [str]:
    if len(msg_text) <= MAX_MSG_LEN:
        return [msg_text]
    i = 0
    split_msgs = []
    while i < len(msg_text):
        j = min(i + MAX_MSG_LEN, len(msg_text))
        split_msgs.append(msg_text[i:j])
        i = j
    return split_msgs

def split_lines(msg_lines: [str]) -> [str]:
    if len(msg_lines) <= MAX_LINES:
        return [''.join(msg_lines)]
    i = 0
    split_msgs = []
    while i < len(msg_lines):
        j = min(i + MAX_LINES, len(msg_lines))
        split_msgs.append(''.join(msg_lines[i:j]))
        i = j
    return split_msgs