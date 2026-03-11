import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Clients ──────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"]).sheet1

ALLOWED_USER_ID = int(os.environ.get("ALLOWED_TELEGRAM_USER_ID", "0"))  # 0 = allow all

# ── AI Parsing ────────────────────────────────────────────────────────────────
def parse_expense(text: str) -> dict | None:
    """Use Claude to extract expense details from natural language."""
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""Extract expense details from this message and return ONLY valid JSON.

Message: "{text}"
Today's date: {today}

Return JSON with these exact keys:
{{
  "amount": <number or null>,
  "category": <string: Food, Transport, Shopping, Entertainment, Health, Bills, Other>,
  "description": <short string>,
  "date": <YYYY-MM-DD, use today if not mentioned>
}}

If this is NOT an expense message, return: {{"amount": null}}
Return ONLY the JSON object, no other text."""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    data = json.loads(raw)
    return data if data.get("amount") else None


# ── Google Sheets ─────────────────────────────────────────────────────────────
def append_expense(date: str, category: str, amount: float, description: str) -> int:
    """Append a row and return the new row number."""
    all_rows = sheet.get_all_values()
    # Add header if sheet is empty
    if not all_rows:
        sheet.append_row(["Date", "Category", "Amount", "Description", "Added At"])
    sheet.append_row([date, category, amount, description, datetime.now().strftime("%Y-%m-%d %H:%M")])
    return len(sheet.get_all_values())


def get_summary() -> str:
    """Return a text summary of all expenses."""
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        return "No expenses recorded yet."

    data = rows[1:]  # skip header
    total = 0.0
    by_category: dict[str, float] = {}
    for row in data:
        try:
            amt = float(row[2])
            cat = row[1]
            total += amt
            by_category[cat] = by_category.get(cat, 0) + amt
        except (IndexError, ValueError):
            continue

    lines = [f"📊 *Expense Summary* ({len(data)} entries)\n"]
    for cat, amt in sorted(by_category.items(), key=lambda x: -x[1]):
        lines.append(f"  {_cat_emoji(cat)} {cat}: ₹{amt:,.2f}")
    lines.append(f"\n💰 *Total: ₹{total:,.2f}*")
    return "\n".join(lines)


def _cat_emoji(cat: str) -> str:
    return {"Food": "🍔", "Transport": "🚗", "Shopping": "🛍️",
            "Entertainment": "🎬", "Health": "💊", "Bills": "📄"}.get(cat, "📌")


# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Expense Tracker Bot*\n\n"
        "Just tell me what you spent:\n"
        "• _Spent 250 on lunch_\n"
        "• _Paid 500 for uber_\n"
        "• _Groceries 1200_\n\n"
        "Commands:\n"
        "/summary – View spending summary\n"
        "/help – Show this message",
        parse_mode="Markdown"
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)


async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    await update.message.reply_text(get_summary(), parse_mode="Markdown")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    text = update.message.text.strip()
    await update.message.reply_chat_action("typing")

    expense = parse_expense(text)
    if not expense:
        await update.message.reply_text(
            "🤔 Couldn't find an expense in that message.\nTry: _Spent 300 on dinner_",
            parse_mode="Markdown"
        )
        return

    row = append_expense(expense["date"], expense["category"], expense["amount"], expense["description"])
    emoji = _cat_emoji(expense["category"])
    await update.message.reply_text(
        f"✅ *Expense logged!*\n\n"
        f"{emoji} {expense['category']}: ₹{expense['amount']:,.2f}\n"
        f"📝 {expense['description']}\n"
        f"📅 {expense['date']}\n"
        f"📋 Row #{row}",
        parse_mode="Markdown"
    )


def _allowed(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    return update.effective_user.id == ALLOWED_USER_ID


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
