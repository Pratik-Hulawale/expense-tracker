import os
import json
import logging
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Groq client ───────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ── Google Sheets ──────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"]).sheet1

_raw_uid = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()
try:
    ALLOWED_USER_ID = int(_raw_uid) if _raw_uid else 0
except ValueError:
    ALLOWED_USER_ID = 0


# ── AI Parsing — returns a LIST of expenses ───────────────────────────────────
def parse_expenses(text: str) -> list[dict]:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    system = "You are an expense parser. Extract ALL expenses and return ONLY a valid JSON array. No markdown, no backticks, no explanation."
    user = f"""Extract ALL transactions from this message (there may be multiple):
"{text}"

Today: {today}

IMPORTANT rules for amount sign:
- If message starts with + (e.g. "+200 salary") → amount is POSITIVE (income/received)
- If message starts with - (e.g. "-100 coffee") → amount is NEGATIVE (expense/spent)
- If no sign prefix → amount is NEGATIVE by default (it's an expense)

Return a JSON ARRAY. Example:
[
  {{"amount": -250, "category": "Food", "description": "lunch", "date": "{today}"}},
  {{"amount": 200, "category": "Other", "description": "received cash", "date": "{today}"}}
]

category must be one of: Food, Transport, Shopping, Entertainment, Health, Bills, Income, Other
If NO transactions found, return: []
Return ONLY the JSON array, nothing else."""

    try:
        r = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        logger.info(f"Groq raw: {raw}")

        raw = raw.replace("```json", "").replace("```", "").strip()
        start, end = raw.find("["), raw.rfind("]") + 1
        if start == -1 or end == 0:
            return []
        expenses = json.loads(raw[start:end])
        return [e for e in expenses if e.get("amount")]

    except httpx.HTTPStatusError as e:
        logger.error(f"Groq HTTP {e.response.status_code}: {e.response.text}")
        raise
    except Exception as e:
        logger.error(f"Groq error: {type(e).__name__}: {e}")
        raise


# ── Sheets helpers ────────────────────────────────────────────────────────────
def ensure_header():
    """Make sure header row exists."""
    try:
        cell = sheet.cell(1, 1).value
        if not cell or cell.strip().lower() != "date":
            sheet.insert_row(["Date", "Category", "Amount", "Description", "Added At"], 1)
            logger.info("Header row inserted.")
    except Exception as e:
        logger.error(f"Header check error: {e}")

def append_expense(date, category, amount, description) -> int:
    sheet.append_row([date, category, float(amount), description, datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")])
    return sheet.row_count

def get_summary() -> str:
    rows = sheet.get_all_values()
    logger.info(f"Sheet rows: {len(rows)} | First row: {rows[0] if rows else 'empty'}")

    # Skip header row if present
    data = [r for r in rows if r and r[0].strip().lower() != "date" and r[0].strip() != ""]
    if not data:
        return "No expenses recorded yet."

    total, by_cat = 0.0, {}
    for row in data:
        try:
            amt = float(row[2]); cat = row[1]
            total += amt; by_cat[cat] = by_cat.get(cat, 0) + amt
        except (IndexError, ValueError):
            continue

    total_expense = sum(v for v in by_cat.values() if v < 0)
    total_income = sum(v for v in by_cat.values() if v > 0)
    lines = [f"📊 *Summary* ({len(data)} entries)\n"]
    for cat, amt in sorted(by_cat.items(), key=lambda x: x[1]):
        prefix = "📈" if amt > 0 else _emoji(cat)
        lines.append(f"  {prefix} {cat}: ₹{abs(amt):,.2f} ({'income' if amt > 0 else 'expense'})")
    if total_income:
        lines.append(f"\n📈 *Income: ₹{total_income:,.2f}*")
    lines.append(f"💸 *Expenses: ₹{abs(total_expense):,.2f}*")
    lines.append(f"💰 *Net: ₹{total_income + total_expense:,.2f}*")
    return "\n".join(lines)

def _emoji(cat):
    return {"Food":"🍔","Transport":"🚗","Shopping":"🛍️","Entertainment":"🎬","Health":"💊","Bills":"📄"}.get(cat,"📌")


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Expense Tracker Bot*\n\nJust tell me what you spent:\n"
        "• _Spent 250 on lunch_\n• _Paid 500 for uber_\n• _Groceries 1200_\n\n"
        "You can send multiple expenses in one message!\n\n"
        "Commands:\n/summary – View spending summary\n/help – Show this message",
        parse_mode="Markdown")

async def help_cmd(update, ctx): await start(update, ctx)

async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    result = get_summary()
    logger.info(f"Summary result: {result}")
    await update.message.reply_text(result, parse_mode="Markdown")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ Unauthorized."); return
    text = update.message.text.strip()
    await update.message.reply_chat_action("typing")
    try:
        expenses = parse_expenses(text)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: `{type(e).__name__}: {str(e)[:200]}`", parse_mode="Markdown")
        return

    if not expenses:
        await update.message.reply_text("🤔 Couldn't find any expenses.\nTry: _Spent 300 on dinner_", parse_mode="Markdown")
        return

    lines = ["✅ *Expenses logged!*\n"]
    for e in expenses:
        append_expense(e["date"], e["category"], e["amount"], e["description"])
        amt = float(e['amount'])
        sign = "📈 Income" if amt > 0 else _emoji(e['category'])
        lines.append(f"{sign} {e['category']}: ₹{abs(amt):,.2f} — {e['description']}")

    lines.append(f"\n📋 {len(expenses)} expense(s) added to your sheet!")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

def _allowed(update):
    return ALLOWED_USER_ID == 0 or update.effective_user.id == ALLOWED_USER_ID

def main():
    ensure_header()
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
