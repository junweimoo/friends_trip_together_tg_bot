import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_session, get_chat_users, create_full_transaction

SELECT_PAYER, ENTER_COMMENT, ENTER_AMOUNT, SELECT_CURRENCY, SELECT_PAYEE, \
    SELECT_CONSUMER_FOR_SPLIT, ENTER_CONSUMER_AMOUNT = range(7)

async def start_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Fetch users and ask who paid."""
    context.user_data.clear()
    context.user_data['split_allocations'] = {}

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    user_id = update.effective_user.id

    context.user_data['initiator_id'] = user_id

    async with get_session() as session:
        users = await get_chat_users(session, chat_id, thread_id)
        context.user_data['user_map'] = {u.user_id: u.name for u in users}

    if len(users) < 2:
        await update.message.reply_text("Need at least 2 registered users. Use /register first.")
        return ConversationHandler.END

    keyboard = []
    for user in users:
        keyboard.append([InlineKeyboardButton(user.name, callback_data=str(user.user_id))])

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL")])

    await update.message.reply_text(
        "💸 New Payment Record\n\nWho **PAID** the money?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return SELECT_PAYER

async def select_payer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Save payer and ask for comment."""
    query = update.callback_query
    await query.answer()

    if query.data == "CANCEL":
        await query.edit_message_text("❌ Transaction cancelled.")
        return ConversationHandler.END

    payer_id = int(query.data)
    context.user_data['payer_id'] = payer_id
    context.user_data['payer_name'] = context.user_data['user_map'].get(payer_id, "Unknown")

    await query.edit_message_text(
        f"📝 What is this payment for? (Enter a description)",
        parse_mode='Markdown'
    )
    return ENTER_COMMENT

async def enter_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: Save comment and ask for total amount."""
    if not is_message_sender_initiator(update, context):
        await update.message.reply_text("Only the user who started this transaction can answer.")
        return ENTER_COMMENT

    description = update.message.text.strip()
    context.user_data['description'] = description

    await update.message.reply_text(
        f"💰 Enter the **TOTAL AMOUNT** (e.g., 60.00):",
        parse_mode='Markdown'
    )
    return ENTER_AMOUNT

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 4: Validate amount and ask for currency."""
    if not is_message_sender_initiator(update, context):
        await update.message.reply_text("Only the user who started this transaction can answer.")
        return ENTER_AMOUNT

    text = update.message.text.strip()

    try:
        if "." in text:
            decimal_part = text.split(".")[1]
            if len(decimal_part) > 2:
                await update.message.reply_text("Invalid amount. Please limit to 2 decimal places (e.g., 10.50).")
                return ENTER_AMOUNT
        amount = float(text)
        if amount <= 0:
            raise ValueError
        context.user_data['amount'] = amount
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a positive number.")
        return ENTER_AMOUNT

    currencies_1 = ["SGD", "MYR", "USD", "EUR"]
    currencies_2 = ["CNY", "THB", "VND", "HKD"]
    keyboard = [[InlineKeyboardButton(curr, callback_data=curr) for curr in currencies_1]]
    keyboard.append([InlineKeyboardButton(curr, callback_data=curr) for curr in currencies_2])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL")])

    await update.message.reply_text(
        f"💵 Amount: {amount}\nSelect **CURRENCY**:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return SELECT_CURRENCY

async def select_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 5: Save Currency and prompt for Payee type."""
    query = update.callback_query
    await query.answer()

    if query.data == "CANCEL":
        await query.edit_message_text("❌ Transaction cancelled.")
        return ConversationHandler.END

    context.user_data['currency'] = query.data

    payer_id = context.user_data['payer_id']
    payer_name = context.user_data['payer_name']

    keyboard = []
    for user_id, name in context.user_data['user_map'].items():
        if user_id != payer_id:
            keyboard.append([InlineKeyboardButton(name, callback_data=str(user_id))])

    keyboard.append([InlineKeyboardButton("👨‍👩‍👧‍👦 Split Equally (All)", callback_data="SPLIT_ALL")])
    keyboard.append([InlineKeyboardButton("📝 Split by amounts", callback_data="SPLIT_AMOUNTS")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL")])

    await query.edit_message_text(
        f"✅ **{payer_name}** paid.\n\nWho is this **FOR**?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return SELECT_PAYEE

async def select_payee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 6: Branch logic: Detailed Split vs Simple Save."""
    query = update.callback_query
    await query.answer()

    if query.data == "CANCEL":
        await query.edit_message_text("❌ Transaction cancelled.")
        return ConversationHandler.END

    payee_data = query.data
    context.user_data['payee_data'] = payee_data

    if payee_data == "SPLIT_AMOUNTS":
        await query.edit_message_text("Starting manual allocation...", parse_mode='Markdown')
        return await prompt_consumer_selection(update, context)
    else:
        return await finalize_split(update, context)

# --- Detailed Split Handlers ---

async def prompt_consumer_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Helper to show available users to allocate amounts to."""
    total_amount = context.user_data['amount']
    allocations = context.user_data['split_allocations']
    current_spent = sum(allocations.values())
    remaining = total_amount - current_spent

    payer_id = context.user_data['payer_id']
    payer_name = context.user_data['payer_name']

    keyboard = []
    for user_id, name in context.user_data['user_map'].items():
        if user_id != payer_id:
            label = name
            if user_id in allocations:
                label = f"{name} ({allocations[user_id]:.2f})"
            keyboard.append([InlineKeyboardButton(label, callback_data=str(user_id))])

    payer_label = f"🧑‍💻 {payer_name} (Payer)"
    if payer_id in allocations:
        payer_label = f"🧑‍💻 {payer_name} ({allocations[payer_id]:.2f})"
    keyboard.append([InlineKeyboardButton(payer_label, callback_data=str(payer_id))])

    if current_spent > 0:
        finish_lbl = f"✅ FINISH ({remaining:.2f} left)"
        keyboard.append([InlineKeyboardButton(finish_lbl, callback_data="FINISH_SPLIT")])

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL")])

    msg = (f"**Total:** {total_amount:.2f}\n"
           f"**Allocated:** {current_spent:.2f}\n"
           f"**Remaining:** {remaining:.2f}\n\n"
           f"Select a person to add or modify:")

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    elif update.message:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    return SELECT_CONSUMER_FOR_SPLIT

async def select_consumer_for_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 7: Handle selection of a specific consumer in detailed split."""
    query = update.callback_query
    await query.answer()

    if query.data == "CANCEL":
        await query.edit_message_text("❌ Transaction cancelled.")
        return ConversationHandler.END

    if query.data == "FINISH_SPLIT":
        return await finalize_split(update, context, detailed=True)

    consumer_id = int(query.data)
    context.user_data['current_consumer_id'] = consumer_id

    consumer_name = context.user_data['user_map'].get(consumer_id, "Unknown")

    current_val = context.user_data['split_allocations'].get(consumer_id)

    total_amount = context.user_data['amount']
    current_spent = sum(context.user_data['split_allocations'].values())
    remaining = total_amount - current_spent

    prompt_text = f"👤 Selected: **{consumer_name}**\n"
    prompt_text += f"💸 Remaining to allocate: {remaining:.2f}\n"

    if current_val is not None:
        prompt_text += f"✏️ **Current allocation:** {current_val:.2f}\n\n"
    else:
        prompt_text += "\n"

    prompt_text += f"Enter the **AMOUNT** for {consumer_name}:"

    await query.edit_message_text(prompt_text, parse_mode='Markdown')
    return ENTER_CONSUMER_AMOUNT

async def enter_consumer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 8: Save amount for consumer and loop back."""
    if not is_message_sender_initiator(update, context):
        await update.message.reply_text("Only the user who started this transaction can answer.")
        return ENTER_CONSUMER_AMOUNT

    text = update.message.text.strip()
    consumer_id = context.user_data.get('current_consumer_id')

    try:
        if "." in text:
            if len(text.split(".")[1]) > 2:
                await update.message.reply_text("Limit to 2 decimal places.")
                return ENTER_CONSUMER_AMOUNT
        val = float(text)
        if val < 0: raise ValueError

        context.user_data['split_allocations'][consumer_id] = val
        del context.user_data['current_consumer_id']

        return await prompt_consumer_selection(update, context)

    except ValueError:
        await update.message.reply_text("Invalid amount. Enter a positive number.")
        return ENTER_CONSUMER_AMOUNT

async def finalize_split(update, context, detailed=False):
    """Saves the transaction to DB. Fixed to accept 'update' for chat ID access."""
    data = context.user_data
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id

    payer_name = data.get('payer_name', 'Unknown')
    total_amount = data['amount']
    payee_arg = data.get('payee_data')

    if detailed:
        allocated_sum = sum(data['split_allocations'].values())
        if allocated_sum > total_amount + 0.05: 
            error_msg = "❌ Total allocated exceeds original amount. Please retry."
            if update.callback_query:
                await update.callback_query.edit_message_text(error_msg)
            else:
                await update.message.reply_text(error_msg)
            return await prompt_consumer_selection(update, context)

        remaining = total_amount - allocated_sum
        if remaining > 0.01:
            payer_id = data['payer_id']
            data['split_allocations'][payer_id] = data['split_allocations'].get(payer_id, 0) + remaining

        payee_arg = {
            'type': 'DETAILED_SPLIT',
            'allocations': data['split_allocations']
        }

    try:
        record_count = await create_full_transaction(
            chat_id=chat_id,
            thread_id=thread_id,
            payer_id=data['payer_id'],
            payee_id_or_split=payee_arg,
            currency=data['currency'],
            total_amount=total_amount,
            description=data['description']
        )

        if detailed:
            msg = (f"✅ **Manual Split Recorded!**\n"
                   f"📌 {data['description']}\n"
                   f"👤 Payer: {payer_name}\n"
                   f"💵 Total: {total_amount} {data['currency']}")
        elif payee_arg == "SPLIT_ALL":
            msg = (f"✅ **Equal Split Recorded!**\n"
                   f"📌 {data['description']}\n"
                   f"👤 Payer: {payer_name}\n"
                   f"💵 Total: {total_amount} {data['currency']}\n"
                   f"🔗 Split among {record_count + 1} people")
        else:
            msg = (f"✅ **Payment Recorded!**\n"
                   f"📌 {data['description']}\n"
                   f"👤 From: {payer_name}\n"
                   f"💵 Amount: {total_amount} {data['currency']}")

        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
        else:
            await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        logging.error(f"DB Error: {e}")
        error_msg = "❌ Error saving transaction."
        if update.callback_query:
            await update.callback_query.edit_message_text(error_msg)
        else:
            await update.message.reply_text(error_msg)

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Transaction cancelled.")
    return ConversationHandler.END

def is_message_sender_initiator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    initiator_id = context.user_data['initiator_id']
    sender_id = update.effective_user.id
    return sender_id == initiator_id