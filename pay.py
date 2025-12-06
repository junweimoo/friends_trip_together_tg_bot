import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import get_session, get_chat_users, create_full_transaction

# Correct state order: Payer -> Recipient -> Amount -> Currency -> Comment
SELECT_PAYER, SELECT_PAYEE, ENTER_AMOUNT, SELECT_CURRENCY, ENTER_COMMENT = range(5)

async def start_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Fetch users and ask who paid."""
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    async with get_session() as session:
        users = await get_chat_users(session, chat_id, thread_id)

    if len(users) < 2:
        await update.message.reply_text("Need at least 2 registered users. Use /register first.")
        return ConversationHandler.END

    # Produce prompt: payer
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
    """Step 2: Save payer and ask who received (including Split option)."""
    query = update.callback_query
    await query.answer()

    if query.data == "CANCEL":
        await query.edit_message_text("❌ Transaction cancelled.")
        return ConversationHandler.END

    payer_id = int(query.data)
    context.user_data['payer_id'] = payer_id

    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id

    async with get_session() as session:
        users = await get_chat_users(session, chat_id, thread_id)
        payer_name = next((u.name for u in users if u.user_id == payer_id), "Unknown")
        context.user_data['payer_name'] = payer_name

    # Produce prompt: payee
    keyboard = []
    for user in users:
        if user.user_id != payer_id:
            keyboard.append([InlineKeyboardButton(user.name, callback_data=str(user.user_id))])

    keyboard.append([InlineKeyboardButton("👨‍👩‍👧‍👦 Split Equally (All)", callback_data="SPLIT_ALL")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="CANCEL")])

    await query.edit_message_text(
        f"✅ **{payer_name}** paid.\n\nWho is this **FOR**?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

    return SELECT_PAYEE

async def select_payee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: Save payee (or SPLIT flag) and ask for total amount."""
    query = update.callback_query
    await query.answer()

    if query.data == "CANCEL":
        await query.edit_message_text("❌ Transaction cancelled.")
        return ConversationHandler.END

    payee_data = query.data
    context.user_data['payee_data'] = payee_data

    # Produce prompt: total amount
    text_display = "everyone (Split)" if payee_data == "SPLIT_ALL" else "a specific user"
    await query.edit_message_text(
        f"For **{text_display}**.\n\n💰 Enter the **TOTAL AMOUNT** (e.g., 60.00):",
        parse_mode='Markdown'
    )
    # Move to the state that captures the amount text input
    return ENTER_AMOUNT

async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 4: Validate amount and ask for currency."""
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

    # Produce prompt: currencies
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
    """Step 5: Save Currency and ask for Comment/Description."""
    query = update.callback_query
    await query.answer()

    if query.data == "CANCEL":
        await query.edit_message_text("❌ Transaction cancelled.")
        return ConversationHandler.END

    context.user_data['currency'] = query.data

    # Produce prompt: comment
    await query.edit_message_text(
        f"📝 What is this payment for? (Enter a description)",
        parse_mode='Markdown'
    )
    return ENTER_COMMENT

async def enter_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 6: Save everything to DB."""
    description = update.message.text.strip()
    data = context.user_data

    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id

    try:
        record_count = await create_full_transaction(
            chat_id=chat_id,
            thread_id=thread_id,
            payer_id=data['payer_id'],
            payee_id_or_split=data['payee_data'],
            currency=data['currency'],
            total_amount=data['amount'],
            description=description
        )

        payer_name = data['payer_name']

        logging.info(f"[PAYMENT] chat={chat_id} thread={thread_id} amount={data['amount']} {data['currency']} payer={data['payer_id']}")

        if data['payee_data'] == "SPLIT_ALL":
            msg = (f"✅ **Split Bill Recorded!**\n"
                   f"📌 Group: {description}\n"
                   f"👤 Payer: {payer_name}\n"
                   f"💵 Total: {data['amount']} {data['currency']}\n"
                   f"🔗 Split among {record_count + 1} people") # +1 includes the payer
        else:
            msg = (f"✅ **Payment Recorded!**\n"
                   f"📌 For: {description}\n"
                   f"👤 From: {payer_name}\n"
                   f"💵 Amount: {data['amount']} {data['currency']}")

        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        logging.error(f"DB Error: {e}")
        await update.message.reply_text("❌ Error saving transaction.")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Transaction cancelled.")
    return ConversationHandler.END