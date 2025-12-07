from collections import defaultdict
from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session, PayRecord, User, get_chat_users

async def suggest_settlements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Calculates the most efficient way to settle debts (minimize transactions).
    """
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id

    async with get_session() as session:
        # 1. Fetch all records
        stmt_records = select(PayRecord).where(
            PayRecord.chat_id == chat_id,
            PayRecord.thread_id == thread_id
        )
        records_result = await session.execute(stmt_records)
        records = records_result.scalars().all()

        if not records:
            await update.message.reply_text("No transactions found to settle.")
            return

        # 2. Fetch Users for Name Mapping
        users = await get_chat_users(session, chat_id, thread_id)
        user_map = {u.user_id: u.name for u in users}

        # 3. Calculate Net Balances per Currency
        # Structure: {'USD': {user_id: 10.0}, 'EUR': {user_id: -5.0}}
        balances = defaultdict(lambda: defaultdict(float))
        
        for r in records:
            balances[r.currency][r.from_user_id] += float(r.value)
            balances[r.currency][r.to_user_id] -= float(r.value)

        # 4. Simplification Algorithm
        settlement_plan = [] # (payer_name, payee_name, amount, currency)

        for currency, user_balances in balances.items():
            debtors = []
            creditors = []

            # Separate into two lists
            for uid, amount in user_balances.items():
                if abs(amount) < 0.01: continue # Skip settled users
                
                if amount < 0:
                    debtors.append({'id': uid, 'val': amount})
                else:
                    creditors.append({'id': uid, 'val': amount})

            # Sort to optimize (Greedy approach: match largest debt with largest credit)
            debtors.sort(key=lambda x: x['val'])       # Ascending (most negative first)
            creditors.sort(key=lambda x: x['val'], reverse=True) # Descending (most positive first)

            d_idx = 0
            c_idx = 0

            while d_idx < len(debtors) and c_idx < len(creditors):
                debtor = debtors[d_idx]
                creditor = creditors[c_idx]

                # The amount to settle is the minimum of the absolute debt or the available credit
                amount = min(abs(debtor['val']), creditor['val'])

                # Record the transaction
                payer_name = user_map.get(debtor['id'], "Unknown")
                payee_name = user_map.get(creditor['id'], "Unknown")
                
                if amount > 0.00:
                    settlement_plan.append((payer_name, payee_name, amount, currency))

                # Update balances
                debtor['val'] += amount
                creditor['val'] -= amount

                # Move pointers if settled (approximate for float precision)
                if abs(debtor['val']) < 0.01:
                    d_idx += 1
                if creditor['val'] < 0.01:
                    c_idx += 1

        # 5. Output Results
        if not settlement_plan:
            await update.message.reply_text("Everyone is all settled up! 🎉")
            return

        msg = "🤝 To settle all owed amounts efficiently:\n"
        for payer, payee, amount, curr in settlement_plan:
            msg += f"• **{payer}** pays **{payee}** {amount:.2f} {curr}\n"
        
        await update.message.reply_text(msg, parse_mode='Markdown')