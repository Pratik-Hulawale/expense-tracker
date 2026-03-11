import os
import json
import logging
import httpx
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Clients ──────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

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


# ── AI Parsing ────────────────────────────────────────────────────────────────
def parse_expense(text: str) -> dict | None:
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = f"""Extract ONE expense from this message and return ONLY a JSON object.

Message: "{text}"
Today: {today}

Return ONLY raw JSON like this example:
{{"amount": 250, "category": "Food", "description": "lunch", "date": "{today}"}}

category must be one of: Food, Transport, Shopping, Entertainment, Health, Bills, Other
If no expense found, return: {{"amount": null}}
No markdown, no backticks, no explanation. Just JSON."""

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        r = httpx.post(GEMINI_URL, json=payload, timeout=15)
        r.raise_for_status()
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        logger.info(f"Gemini raw: {raw}")

        raw = raw.replace("```json", "").replace("```", "").strip()
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        data = json.loads(raw[start:end])
        return data if data.get("amount") else None

    except httpx.HTTPStatusError as e:
        logger.error(f"Gemini HTTP error {e.response.status_code}: {e.response.text}")
        raise
    except Exception as e:
        logger.error(f"Gemini error: {type(e).__name__}: {e}")
        raise


# ── Google Sheets ─────────────────────────────────────────────────────────────
def append_expense(date: str, category: str, amount: float, description: str) -> int:
    all_rows = sheet.get_all_values()
    if not all_rows:
        sheet.append_row(["Date", "Category", "Amount", "Description", "Added At"])
    sheet.append_row([date, category, amount, description, datetime.now().strftime("%Y-%m-%d %H:%M")])
    return len(sheet.get_all_values())


def get_summary() -> str:
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        return "No expenses recorded yet."
    data = rows[1:]
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
    try:
        expense = parse_expense(text)
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ Error: `{type(e).__name__}: {str(e)[:200]}`",
            parse_mode="Markdown"
        )
        return
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