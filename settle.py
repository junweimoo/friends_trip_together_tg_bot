from collections import defaultdict
from decimal import Decimal, InvalidOperation
from sqlalchemy import select
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_session, PayRecord, User, get_chat_users
from utils import get_chat_thread_user_id

SELECT_SETTLE_CURRENCY, ENTER_RATE = range(2)

async def start_settle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    currencies_1 = ["SGD", "MYR", "USD", "EUR"]
    currencies_2 = ["CNY", "THB", "VND", "HKD"]
    keyboard = [[InlineKeyboardButton(curr, callback_data=curr) for curr in currencies_1]]
    keyboard.append([InlineKeyboardButton(curr, callback_data=curr) for curr in currencies_2])
    reply_markup = InlineKeyboardMarkup(keyboard)

    chat_id, thread_id, user_id = get_chat_thread_user_id(update)
    context.user_data["chat_id"] = chat_id
    context.user_data["thread_id"] = thread_id
    context.user_data["user_id"] = user_id

    await update.message.reply_text(
        "Let's settle up! First, please select the **Target Currency** "
        "that everyone should pay/receive in:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

    return SELECT_SETTLE_CURRENCY

async def select_settle_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    target_currency = query.data
    context.user_data["target_currency"] = target_currency
    context.user_data["exchange_rates"] = {} # e.g. 'EUR_USD': 1.1
    
    async with get_session() as session: 
        # 1. Get all unique currencies
        stmt_records = select(PayRecord).where(
            PayRecord.chat_id == context.user_data["chat_id"],
            PayRecord.thread_id == context.user_data["thread_id"]
        )
        records_result = await session.execute(stmt_records)
        records = records_result.scalars().all()
        tx_currencies = set(r.currency for r in records)
    
        # 2. Determine which pairs need conversion
        needed_pairs = []
        for tx_curr in tx_currencies:
            if tx_curr != target_currency:
                needed_pairs.append((tx_curr, target_currency))
                
        context.user_data["needed_pairs_queue"] = needed_pairs
        
        await query.edit_message_text(f"Target currency set to: **{target_currency}**", parse_mode="Markdown")
        
        # 3. Check if we need to ask for rates or jump straight to calc
        if not needed_pairs:
            return await calculate_settlements(update, context)
        else:
            return await ask_next_rate(update, context)

async def ask_next_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    queue = context.user_data.get("needed_pairs_queue", [])
    
    if not queue: 
        return await calculate_settlements(update, context)
    
    source, target = queue[0]
    
    message_text = (
        f"I found transactions in **{source}**.\n"
        f"Please enter the exchange rate: 1 {source} = ? {target}"
    )
    
    if update.callback_query:
        await update.callback_query.message.reply_text(message_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(message_text, parse_mode="Markdown")
        
    return ENTER_RATE

async def store_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # parse rate to float
    text = update.message.text.strip()
    try:
        rate = Decimal(text)
        if rate <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid rate. Please enter a positive number (e.g., 1.05).")
        return ENTER_RATE

    # Get the current pair being processed
    queue = context.user_data["needed_pairs_queue"]
    current_source, current_target = queue.pop(0) # Remove from queue
    
    # Store the rate
    key = f"{current_source}_{current_target}"
    context.user_data["exchange_rates"][key] = rate
    
    await update.message.reply_text(f"Saved: 1 {current_source} = {rate} {current_target}")
    
    # Loop back to check if more rates are needed
    return await ask_next_rate(update, context)

async def calculate_settlements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Calculates the most efficient way to settle debts (minimize transactions).
    """
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id

    target_currency = context.user_data["target_currency"]
    rates = context.user_data["exchange_rates"]

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

        # 1a. Normalize all records to target currency
        for r in records:
            if r.currency != target_currency:
                rate = rates.get(f"{r.currency}_{target_currency}", 1.0)
                r.value = r.value * rate
                r.currency = target_currency

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
        
    return ConversationHandler.END