from collections import defaultdict
from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session, PayRecord, User, PaymentGroup, PaymentGroupLink, get_chat_users
from utils import split_lines

async def list_settlements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lists all transactions (grouped by payment group) and calculates net balances.
    """
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id

    async with get_session() as session:
        # 1. Fetch records
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
        rows = records_result.all()

        if not rows:
            await update.message.reply_text("No transactions found in this chat.")
            return

        # 2. Fetch Users
        users = await get_chat_users(session, chat_id, thread_id)
        user_map = {u.user_id: u.name for u in users}

        # 3. Process Data
        balances = defaultdict(lambda: defaultdict(float))
        history_text_lines = ["📜 <b>Transaction History</b>\n"]
        
        last_group_id = None

        for record, group_name, group_id in rows:
            # --- Balance Calculation ---
            balances[record.from_user_id][record.currency] += float(record.value)
            balances[record.to_user_id][record.currency] -= float(record.value)

            # --- Formatting History ---
            payer = user_map.get(record.from_user_id, "Unknown")
            payee = user_map.get(record.to_user_id, "Unknown")

            # Check if this record belongs to a new group context
            if group_id and group_id != last_group_id:
                history_text_lines.append(f"\n📂 <b>{group_name}</b>\n")
            
            # Indent if inside a group, otherwise standard bullet
            prefix = "  •" if group_id else "•"
            
            history_text_lines.append(f"{prefix} {payer} ➜ {payee}: {record.value:.2f} {record.currency}\n")
            
            last_group_id = group_id

        # 4. Format Net Balances
        summary_text_lines = ["📊 <b>Net Balances</b>\n"]
        has_balances = False
        
        for user_id, currencies in balances.items():
            user_name = user_map.get(user_id, "Unknown")
            user_lines = []
            
            for currency, amount in currencies.items():
                if abs(amount) < 0.01: 
                    continue
                
                if amount > 0:
                    user_lines.append(f"is owed {amount:.2f} {currency}")
                else:
                    user_lines.append(f"owes {abs(amount):.2f} {currency}")
            
            if user_lines:
                has_balances = True
                summary_text_lines.append(f"\n• <b>{user_name}</b>: {', '.join(user_lines)}\n")

        if not has_balances:
            summary_text_lines.append("\nAll settled up! ✅")

        summary_text_lines.append('\n')

        split_msgs = split_lines(summary_text_lines + history_text_lines)
        for msg in split_msgs:
            await update.message.reply_text(msg, parse_mode='HTML')

