import os
import json
import logging
import httpx
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Groq client (free, no billing needed) ─────────────────────────────────────
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


# ── AI Parsing ────────────────────────────────────────────────────────────────
def parse_expense(text: str) -> dict | None:
    today = datetime.now().strftime("%Y-%m-%d")
    system = "You are an expense parser. Extract expense details and return ONLY valid JSON. No markdown, no backticks, no explanation."
    user = f"""Extract ONE expense from this message:
"{text}"

Today: {today}

Return ONLY this JSON format:
{{"amount": 250, "category": "Food", "description": "lunch", "date": "{today}"}}

category must be one of: Food, Transport, Shopping, Entertainment, Health, Bills, Other
If no expense found: {{"amount": null}}"""

    try:
        r = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.1,
                "max_tokens": 150,
            },
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        logger.info(f"Groq raw: {raw}")

        raw = raw.replace("```json", "").replace("```", "").strip()
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        data = json.loads(raw[start:end])
        return data if data.get("amount") else None

    except httpx.HTTPStatusError as e:
        logger.error(f"Groq HTTP {e.response.status_code}: {e.response.text}")
        raise
    except Exception as e:
        logger.error(f"Groq error: {type(e).__name__}: {e}")
        raise


# ── Sheets helpers ────────────────────────────────────────────────────────────
def append_expense(date, category, amount, description) -> int:
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
    total, by_cat = 0.0, {}
    for row in data:
        try:
            amt = float(row[2]); cat = row[1]
            total += amt; by_cat[cat] = by_cat.get(cat, 0) + amt
        except (IndexError, ValueError):
            continue
    lines = [f"📊 *Expense Summary* ({len(data)} entries)\n"]
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"  {_emoji(cat)} {cat}: ₹{amt:,.2f}")
    lines.append(f"\n💰 *Total: ₹{total:,.2f}*")
    return "\n".join(lines)

def _emoji(cat):
    return {"Food":"🍔","Transport":"🚗","Shopping":"🛍️","Entertainment":"🎬","Health":"💊","Bills":"📄"}.get(cat,"📌")


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Expense Tracker Bot*\n\nJust tell me what you spent:\n"
        "• _Spent 250 on lunch_\n• _Paid 500 for uber_\n• _Groceries 1200_\n\n"
        "Commands:\n/summary – View spending summary\n/help – Show this message",
        parse_mode="Markdown")

async def help_cmd(update, ctx): await start(update, ctx)

async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    await update.message.reply_text(get_summary(), parse_mode="Markdown")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ Unauthorized."); return
    text = update.message.text.strip()
    await update.message.reply_chat_action("typing")
    try:
        expense = parse_expense(text)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: `{type(e).__name__}: {str(e)[:200]}`", parse_mode="Markdown")
        return
    if not expense:
        await update.message.reply_text("🤔 Couldn't find an expense.\nTry: _Spent 300 on dinner_", parse_mode="Markdown")
        return
    row = append_expense(expense["date"], expense["category"], expense["amount"], expense["description"])
    await update.message.reply_text(
        f"✅ *Expense logged!*\n\n{_emoji(expense['category'])} {expense['category']}: ₹{expense['amount']:,.2f}\n"
        f"📝 {expense['description']}\n📅 {expense['date']}\n📋 Row #{row}", parse_mode="Markdown")

def _allowed(update):
    return ALLOWED_USER_ID == 0 or update.effective_user.id == ALLOWED_USER_ID

def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()