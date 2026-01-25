import math
import logging
from collections import defaultdict
from sqlalchemy import select
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_session, PayRecord, User, PaymentGroup, PaymentGroupLink, get_chat_users
from utils import split_lines

LIST_PAGE = range(1)

MAX_PAGES = 10000000
ITEMS_PER_PAGE = 20

async def generate_ledger_view(chat_id, thread_id, page_number):
    async with get_session() as session:
        # 1. Fetch all records in this chat
        stmt = select(
            PayRecord, 
            PaymentGroup.name, 
            PaymentGroup.group_id
        ).outerjoin(
            PaymentGroupLink, 
            PayRecord.pay_record_id == PaymentGroupLink.pay_record_id
        ).outerjoin(
            PaymentGroup, 
            PaymentGroupLink.group_id == PaymentGroup.group_id
        ).where(
            PayRecord.chat_id == chat_id,
            PayRecord.thread_id == thread_id
        ).order_by(PayRecord.gmt_created.asc())
        
        records_result = await session.execute(stmt)
        all_rows = records_result.all()

        if not all_rows:
            return "No transactions found in this chat.", None

        # 2. Fetch all users in this chat
        users = await get_chat_users(session, chat_id, thread_id)
        user_map = {u.user_id: u.name for u in users}

        # 3. Calculate global net balances
        balances = defaultdict(lambda: defaultdict(float))
        for record, _, _ in all_rows:
            balances[record.from_user_id][record.currency] += float(record.value)
            balances[record.to_user_id][record.currency] -= float(record.value)

        # 4. Format net balances
        summary_text_lines = ["📊 <b>Net Balances</b>\n"]
        has_balances = False
        
        for user_id, currencies in balances.items():
            user_name = user_map.get(user_id, "Unknown")
            user_lines = []
            
            for currency, amount in currencies.items():
                if abs(amount) < 0.01: 
                    continue
                if amount > 0:
                    user_lines.append(f"receives {amount:.2f} {currency}")
                else:
                    user_lines.append(f"owes {abs(amount):.2f} {currency}")
            
            if user_lines:
                has_balances = True
                summary_text_lines.append(f"• <b>{user_name}</b>: {', '.join(user_lines)}")

        if not has_balances:
            summary_text_lines.append("All settled up! ✅")

        summary_text_lines.append("\n" + "─" * 15 + "\n") # Separator

        # 5. Handle pagination
        total_records = len(all_rows)
        total_pages = math.ceil(total_records / ITEMS_PER_PAGE)

        if page_number < 1: page_number = 1
        if page_number > total_pages: page_number = total_pages

        start_index = (page_number - 1) * ITEMS_PER_PAGE
        end_index = start_index + ITEMS_PER_PAGE
        page_rows = all_rows[start_index:end_index]

        # 6. Format transaction history in this page
        history_text_lines = [f"📜 <b>History (Page {page_number}/{total_pages})</b>\n"]
        
        last_group_id = None
        
        if start_index > 0:
            _, _, last_group_id = all_rows[start_index - 1]

        for record, group_name, group_id in page_rows:
            payer = user_map.get(record.from_user_id, "Unknown")
            payee = user_map.get(record.to_user_id, "Unknown")

            if group_id and group_id != last_group_id:
                history_text_lines.append(f"\n📂 <b>{group_name}</b>")
            
            prefix = "  •" if group_id else "•"
            history_text_lines.append(f"{prefix} {payer} ➜ {payee}: {record.value:.2f} {record.currency}")
            
            last_group_id = group_id

        full_text = "\n".join(summary_text_lines + history_text_lines)

        keyboard = []
        nav_row = []
        
        if page_number > 1:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"list_page_{page_number - 1}"))
        
        if page_number < total_pages:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"list_page_{page_number + 1}"))
        
        nav_row.append(InlineKeyboardButton("Close", callback_data="CLOSE"))
            
        if nav_row:
            keyboard.append(nav_row)

        return full_text, InlineKeyboardMarkup(keyboard)


async def list_settlements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id

    if "ledger_messages" not in context.chat_data:
        context.chat_data["ledger_messages"] = {}
    
    text, reply_markup = await generate_ledger_view(chat_id, thread_id, page_number=MAX_PAGES)
    
    if text:
        thread_key = thread_id if thread_id else "general"

        last_msg_id = context.chat_data["ledger_messages"].get(thread_key)
        if last_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=last_msg_id)
            except Exception:
                pass

        message = await update.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
        context.chat_data["ledger_messages"][thread_key] = message.message_id
    return LIST_PAGE

async def list_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "CLOSE":
        await query.edit_message_text("List closed.")
        return ConversationHandler.END
    
    target_page = int(query.data.split("_")[-1])
    
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id
    
    text, reply_markup = await generate_ledger_view(chat_id, thread_id, page_number=target_page)
    
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
    except Exception as e:
        if "Message is not modified" not in str(e):
            logging.error(f"Error editing message: {e}")
    return LIST_PAGE

async def close_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END