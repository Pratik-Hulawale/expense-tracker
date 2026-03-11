import os
import json
import logging
import httpx
import base64
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import gspread
from google.oauth2.service_account import Credentials
from dateutil.relativedelta import relativedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds  = Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"]), scopes=SCOPES)
gc = gspread.authorize(creds)
wb = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])

_raw_uid = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()
try:    ALLOWED_USER_ID = int(_raw_uid) if _raw_uid else 0
except ValueError: ALLOWED_USER_ID = 0

MONTHLY_BUDGET = float(os.environ.get("MONTHLY_BUDGET", "0"))

# ── Active sheet state (per session) ─────────────────────────────────────────
RESERVED = {"Recurring", "Meta"}  # sheets not treated as expense sheets
HEADER   = ["Date", "Category", "Amount", "Description", "Added At"]

# Store active sheet name in memory (resets on restart to default)
_active_sheet: dict[int, str] = {}  # user_id -> sheet name

def active_sheet_name(user_id: int) -> str:
    return _active_sheet.get(user_id, "Sheet1")

def set_active_sheet(user_id: int, name: str):
    _active_sheet[user_id] = name

# ── Sheet helpers ─────────────────────────────────────────────────────────────
def get_or_create_sheet(name: str) -> gspread.Worksheet:
    try:
        return wb.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title=name, rows=1000, cols=10)
        if name not in RESERVED:
            ws.append_row(HEADER)
        elif name == "Recurring":
            ws.append_row(["Amount", "Category", "Description", "Frequency", "Next Date", "Active"])
        logger.info(f"Created sheet: {name}")
        return ws

def list_expense_sheets() -> list[str]:
    """Return all sheets except reserved ones."""
    return [ws.title for ws in wb.worksheets() if ws.title not in RESERVED]

def rename_sheet(old: str, new: str) -> bool:
    if old in RESERVED or new in RESERVED:
        return False
    try:
        ws = wb.worksheet(old)
        ws.update_title(new)
        return True
    except Exception as e:
        logger.error(f"Rename error: {e}")
        return False

def delete_sheet(name: str) -> bool:
    if name in RESERVED or name == "Sheet1":
        return False
    try:
        ws = wb.worksheet(name)
        wb.del_worksheet(ws)
        return True
    except Exception as e:
        logger.error(f"Delete sheet error: {e}")
        return False

def ensure_header(sheet_name="Sheet1"):
    ws = get_or_create_sheet(sheet_name)
    cell = ws.cell(1, 1).value
    if not cell or cell.strip().lower() != "date":
        ws.insert_row(HEADER, 1)

def append_expense(date_str, category, amount, description, sheet_name="Sheet1"):
    ws = get_or_create_sheet(sheet_name)
    ws.append_row([date_str, category, float(amount), description,
                   datetime.now(IST).strftime("%Y-%m-%d %H:%M")])

def get_data_rows(sheet_name="Sheet1") -> list:
    ws = get_or_create_sheet(sheet_name)
    rows = ws.get_all_values()
    return [r for r in rows if r and r[0].strip().lower() != "date" and r[0].strip()]

def delete_last_row(sheet_name="Sheet1"):
    ws = get_or_create_sheet(sheet_name)
    rows = ws.get_all_values()
    data = [(i+1, r) for i, r in enumerate(rows)
            if r and r[0].strip().lower() != "date" and r[0].strip()]
    if not data:
        return None
    row_idx, row = data[-1]
    ws.delete_rows(row_idx)
    return row

# ── Budget helpers ────────────────────────────────────────────────────────────
def get_month_total(sheet_name="Sheet1"):
    now    = datetime.now(IST)
    prefix = f"{now.year}-{now.month:02d}"
    return sum(float(r[2]) for r in get_data_rows(sheet_name)
               if r[0].startswith(prefix) and len(r) > 2)

def budget_alert_msg(sheet_name="Sheet1") -> str:
    if MONTHLY_BUDGET <= 0:
        return ""
    spent = get_month_total(sheet_name)
    pct = (spent / MONTHLY_BUDGET) * 100
    if pct >= 100:
        return f"\n\n🚨 *Budget exceeded!* ₹{spent:,.2f} / ₹{MONTHLY_BUDGET:,.2f} ({pct:.0f}%)"
    elif pct >= 80:
        return f"\n\n⚠️ *Budget warning!* ₹{spent:,.2f} / ₹{MONTHLY_BUDGET:,.2f} ({pct:.0f}%)"
    return ""

# ── Recurring expenses ────────────────────────────────────────────────────────
def get_recurring() -> list:
    ws = get_or_create_sheet("Recurring")
    return [r for r in ws.get_all_values()
            if r and r[0].strip().lower() not in ("amount","") and len(r) >= 6 and r[5].strip().lower() == "yes"]

def add_recurring(amount, category, description, frequency):
    ws = get_or_create_sheet("Recurring")
    ws.append_row([float(amount), category, description, frequency,
                   datetime.now(IST).strftime("%Y-%m-%d"), "Yes"])

def process_due_recurring(sheet_name="Sheet1") -> list[str]:
    ws = get_or_create_sheet("Recurring")
    all_rows  = ws.get_all_values()
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    today_dt  = datetime.now(IST).date()
    logged    = []
    for i, row in enumerate(all_rows):
        if not row or row[0].strip().lower() in ("amount","") or len(row) < 6:
            continue
        if row[5].strip().lower() != "yes":
            continue
        try:
            amount, category, description, frequency, next_date_str = (
                float(row[0]), row[1], row[2], row[3], row[4])
            next_dt = date.fromisoformat(next_date_str)
            if next_dt <= today_dt:
                append_expense(today_str, category, amount, f"[Auto] {description}", sheet_name)
                freq_map = {"monthly": relativedelta(months=1),
                            "weekly":  timedelta(weeks=1),
                            "yearly":  relativedelta(years=1)}
                new_next = next_dt + freq_map.get(frequency, relativedelta(months=1))
                ws.update_cell(i + 1, 5, new_next.isoformat())
                logged.append(f"{_emoji(category)} {description}: ₹{amount:,.2f}")
        except Exception as e:
            logger.error(f"Recurring row {i} error: {e}")
    return logged

# ── AI helpers ────────────────────────────────────────────────────────────────
def _groq(messages, max_tokens=500, model="llama-3.1-8b-instant") -> str:
    r = httpx.post(GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.1, "max_tokens": max_tokens},
        timeout=20)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def _extract_json_list(raw: str) -> list:
    raw = raw.replace("```json","").replace("```","").strip()
    s, e = raw.find("["), raw.rfind("]") + 1
    if s == -1 or e == 0:
        return []
    return json.loads(raw[s:e])

def parse_expenses(text: str) -> list[dict]:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    raw = _groq([
        {"role": "system", "content": "Strict expense parser. Only extract expenses with explicit numeric amounts. Return ONLY a JSON array, no markdown."},
        {"role": "user",   "content": f"""Extract ALL expenses from: "{text}"
Today: {today}. Only if explicit number present, else return [].
Format: [{{"amount":250,"category":"Food","description":"lunch","date":"{today}"}}]
Categories: Food,Transport,Shopping,Entertainment,Health,Bills,Other. Return [] if nothing."""}
    ])
    return [x for x in _extract_json_list(raw) if x.get("amount")]

def parse_receipt_image(image_bytes: bytes) -> list[dict]:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    b64   = base64.b64encode(image_bytes).decode()
    raw   = _groq([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": f"Receipt/bill image. Extract ALL items. Today:{today}. Return ONLY JSON array: [{{\"amount\":250,\"category\":\"Food\",\"description\":\"item\",\"date\":\"{today}\"}}]. Return [] if not a receipt."}
    ]}], model="llama-3.2-11b-vision-preview")
    return [x for x in _extract_json_list(raw) if x.get("amount")]

# ── Report builder ────────────────────────────────────────────────────────────
def build_report(label: str, rows: list, sheet_name="Sheet1") -> str:
    if not rows:
        return f"No expenses in *{label}*."
    total, by_cat = 0.0, {}
    for row in rows:
        try:
            amt = float(row[2]); cat = row[1]
            total += amt; by_cat[cat] = by_cat.get(cat, 0) + amt
        except: continue
    lines = [f"📊 *{label}* — _{sheet_name}_ ({len(rows)} entries)\n"]
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"  {_emoji(cat)} {cat}: ₹{amt:,.2f}")
    lines.append(f"\n💰 *Total: ₹{total:,.2f}*")
    if MONTHLY_BUDGET > 0 and "Month" in label:
        pct = (total / MONTHLY_BUDGET) * 100
        bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
        lines.append(f"📉 Budget: [{bar}] {pct:.0f}% of ₹{MONTHLY_BUDGET:,.2f}")
    return "\n".join(lines)

def _emoji(cat):
    return {"Food":"🍔","Transport":"🚗","Shopping":"🛍️",
            "Entertainment":"🎬","Health":"💊","Bills":"📄"}.get(cat, "📌")

def _allowed(update: Update) -> bool:
    return ALLOWED_USER_ID == 0 or update.effective_user.id == ALLOWED_USER_ID

def _uid(update: Update) -> int:
    return update.effective_user.id

# ── /sheets — main sheet management menu ─────────────────────────────────────
async def sheets_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid   = _uid(update)
    current = active_sheet_name(uid)
    all_sheets = list_expense_sheets()

    text = f"📂 *Sheet Manager*\n\nActive sheet: *{current}*\nAll sheets: {', '.join(all_sheets)}\n"
    kb = [
        [InlineKeyboardButton("➕ New sheet",     callback_data="sheet:new"),
         InlineKeyboardButton("🔀 Switch sheet",  callback_data="sheet:switch")],
        [InlineKeyboardButton("✏️ Rename sheet",  callback_data="sheet:rename"),
         InlineKeyboardButton("🗑️ Delete sheet",  callback_data="sheet:delete")],
        [InlineKeyboardButton("📊 Compare sheets", callback_data="sheet:compare")],
    ]
    await update.message.reply_text(text, parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(kb))

# ── Callback router ───────────────────────────────────────────────────────────
async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    data = q.data

    # ── sheet:new ──────────────────────────────────────────────────────────────
    if data == "sheet:new":
        ctx.user_data["awaiting"] = "new_sheet_name"
        await q.edit_message_text("📝 Send the name for your new sheet:\n_(e.g. Work, Travel, March2026)_",
                                  parse_mode="Markdown")

    # ── sheet:switch ───────────────────────────────────────────────────────────
    elif data == "sheet:switch":
        sheets = list_expense_sheets()
        kb = [[InlineKeyboardButton(f"{'✅ ' if s == active_sheet_name(uid) else ''}{s}",
               callback_data=f"sheet:use:{s}")] for s in sheets]
        kb.append([InlineKeyboardButton("« Back", callback_data="sheet:back")])
        await q.edit_message_text("🔀 *Switch to sheet:*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("sheet:use:"):
        name = data[len("sheet:use:"):]
        set_active_sheet(uid, name)
        ensure_header(name)
        await q.edit_message_text(f"✅ Switched to *{name}*\nAll new expenses will go here.",
                                  parse_mode="Markdown")

    # ── sheet:rename ───────────────────────────────────────────────────────────
    elif data == "sheet:rename":
        sheets = [s for s in list_expense_sheets() if s != "Sheet1"]
        if not sheets:
            await q.edit_message_text("No sheets available to rename (Sheet1 is protected).")
            return
        kb = [[InlineKeyboardButton(s, callback_data=f"sheet:renamepick:{s}")] for s in sheets]
        kb.append([InlineKeyboardButton("« Back", callback_data="sheet:back")])
        await q.edit_message_text("✏️ *Which sheet to rename?*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("sheet:renamepick:"):
        old = data[len("sheet:renamepick:"):]
        ctx.user_data["awaiting"]     = "rename_sheet"
        ctx.user_data["rename_target"] = old
        await q.edit_message_text(f"✏️ Send the new name for *{old}*:", parse_mode="Markdown")

    # ── sheet:delete ───────────────────────────────────────────────────────────
    elif data == "sheet:delete":
        sheets = [s for s in list_expense_sheets() if s != "Sheet1"]
        if not sheets:
            await q.edit_message_text("No sheets to delete (Sheet1 is protected).")
            return
        kb = [[InlineKeyboardButton(f"🗑️ {s}", callback_data=f"sheet:delconfirm:{s}")] for s in sheets]
        kb.append([InlineKeyboardButton("« Back", callback_data="sheet:back")])
        await q.edit_message_text("🗑️ *Which sheet to delete?*\n⚠️ This cannot be undone!",
                                  parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("sheet:delconfirm:"):
        name = data[len("sheet:delconfirm:"):]
        kb = [
            [InlineKeyboardButton("⚠️ Yes, delete it", callback_data=f"sheet:dodelete:{name}"),
             InlineKeyboardButton("Cancel",             callback_data="sheet:back")]
        ]
        await q.edit_message_text(f"⚠️ Delete *{name}* and ALL its data?",
                                  parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("sheet:dodelete:"):
        name = data[len("sheet:dodelete:"):]
        ok = delete_sheet(name)
        if ok:
            if active_sheet_name(uid) == name:
                set_active_sheet(uid, "Sheet1")
            await q.edit_message_text(f"🗑️ Sheet *{name}* deleted. Switched to Sheet1.",
                                      parse_mode="Markdown")
        else:
            await q.edit_message_text("❌ Could not delete (Sheet1 is protected).")

    # ── sheet:compare ──────────────────────────────────────────────────────────
    elif data == "sheet:compare":
        sheets = list_expense_sheets()
        lines  = ["📊 *All Sheets Comparison*\n"]
        for s in sheets:
            rows  = get_data_rows(s)
            total = sum(float(r[2]) for r in rows if len(r) > 2)
            mark  = " ✅" if s == active_sheet_name(uid) else ""
            lines.append(f"  📂 *{s}*{mark} — {len(rows)} entries, ₹{total:,.2f}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown")

    elif data == "sheet:back":
        uid2 = q.from_user.id
        current = active_sheet_name(uid2)
        all_sheets = list_expense_sheets()
        text = f"📂 *Sheet Manager*\n\nActive sheet: *{current}*\nAll sheets: {', '.join(all_sheets)}\n"
        kb = [
            [InlineKeyboardButton("➕ New sheet",     callback_data="sheet:new"),
             InlineKeyboardButton("🔀 Switch sheet",  callback_data="sheet:switch")],
            [InlineKeyboardButton("✏️ Rename sheet",  callback_data="sheet:rename"),
             InlineKeyboardButton("🗑️ Delete sheet",  callback_data="sheet:delete")],
            [InlineKeyboardButton("📊 Compare sheets", callback_data="sheet:compare")],
        ]
        await q.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(kb))

# ── Awaiting-input middleware ─────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ Unauthorized."); return

    uid  = _uid(update)
    text = update.message.text.strip()

    # ── Handle sheet management text inputs ───────────────────────────────────
    awaiting = ctx.user_data.get("awaiting")

    if awaiting == "new_sheet_name":
        ctx.user_data.pop("awaiting")
        name = text.strip()
        if name in RESERVED:
            await update.message.reply_text("❌ That name is reserved. Choose another.")
            return
        ensure_header(name)
        set_active_sheet(uid, name)
        await update.message.reply_text(
            f"✅ Sheet *{name}* created and set as active!\nAll new expenses will log here.",
            parse_mode="Markdown")
        return

    if awaiting == "rename_sheet":
        ctx.user_data.pop("awaiting")
        old = ctx.user_data.pop("rename_target", None)
        new = text.strip()
        if not old:
            await update.message.reply_text("Something went wrong. Try /sheets again.")
            return
        if rename_sheet(old, new):
            if active_sheet_name(uid) == old:
                set_active_sheet(uid, new)
            await update.message.reply_text(f"✅ Renamed *{old}* → *{new}*", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Rename failed. Name may be reserved.")
        return

    # ── Normal expense parsing ─────────────────────────────────────────────────
    sheet_name = active_sheet_name(uid)
    await update.message.reply_chat_action("typing")
    try:
        expenses = parse_expenses(text)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: `{str(e)[:200]}`", parse_mode="Markdown")
        return
    if not expenses:
        await update.message.reply_text(
            "🤔 No expense found. Include a number!\nExample: _Spent 300 on dinner_",
            parse_mode="Markdown")
        return
    lines = [f"✅ *Expenses logged to {sheet_name}!*\n"]
    for e in expenses:
        append_expense(e["date"], e["category"], e["amount"], e["description"], sheet_name)
        lines.append(f"{_emoji(e['category'])} {e['category']}: ₹{float(e['amount']):,.2f} — {e['description']}")
    lines.append(f"\n📋 {len(expenses)} expense(s) added!")
    await update.message.reply_text("\n".join(lines) + budget_alert_msg(sheet_name), parse_mode="Markdown")

# ── Photo handler ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ Unauthorized."); return
    uid = _uid(update)
    sheet_name = active_sheet_name(uid)
    await update.message.reply_chat_action("upload_photo")
    try:
        photo     = update.message.photo[-1]
        file      = await ctx.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        expenses  = parse_receipt_image(bytes(img_bytes))
    except Exception as e:
        await update.message.reply_text(f"⚠️ Receipt error: `{str(e)[:200]}`", parse_mode="Markdown")
        return
    if not expenses:
        await update.message.reply_text("🤔 Couldn't read receipt. Try a clearer photo.")
        return
    lines = [f"🧾 *Receipt → {sheet_name}!*\n"]
    for e in expenses:
        append_expense(e["date"], e["category"], e["amount"], e["description"], sheet_name)
        lines.append(f"{_emoji(e['category'])} {e['category']}: ₹{float(e['amount']):,.2f} — {e['description']}")
    lines.append(f"\n📋 {len(expenses)} item(s) logged!")
    await update.message.reply_text("\n".join(lines) + budget_alert_msg(sheet_name), parse_mode="Markdown")

# ── Standard command handlers ─────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = _uid(update)
    await update.message.reply_text(
        "👋 *Expense Tracker Bot*\n\n"
        "📝 *Log expenses:*\n"
        "  Type: _Spent 250 on lunch_\n"
        "  Multiple: _Lunch 250, uber 150_\n"
        "  📸 Send a photo of a receipt!\n\n"
        "📂 *Sheet Management:*\n"
        "  /sheets – Create, switch, rename, delete sheets\n"
        f"  Active sheet: *{active_sheet_name(uid)}*\n\n"
        "📊 *Reports:*\n"
        "  /summary – All time\n"
        "  /weekly – This week\n"
        "  /monthly – This month\n\n"
        "⚙️ *Manage:*\n"
        "  /delete – Remove last entry\n"
        "  /recurring 649 Entertainment Netflix monthly\n"
        "  /listrecurring – View recurring\n"
        "  /setbudget 20000 – Set monthly budget\n"
        "  /help – Show this message",
        parse_mode="Markdown")

async def help_cmd(update, ctx): await start(update, ctx)

async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid = _uid(update)
    sn  = active_sheet_name(uid)
    await update.message.reply_text(build_report("All Time", get_data_rows(sn), sn), parse_mode="Markdown")

async def weekly(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid     = _uid(update)
    sn      = active_sheet_name(uid)
    week_ago = (datetime.now(IST).date() - timedelta(days=7)).isoformat()
    rows    = [r for r in get_data_rows(sn) if r[0] >= week_ago]
    await update.message.reply_text(build_report("Weekly", rows, sn), parse_mode="Markdown")

async def monthly(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid    = _uid(update)
    sn     = active_sheet_name(uid)
    now    = datetime.now(IST)
    prefix = f"{now.year}-{now.month:02d}"
    rows   = [r for r in get_data_rows(sn) if r[0].startswith(prefix)]
    await update.message.reply_text(build_report(now.strftime("%B %Y"), rows, sn), parse_mode="Markdown")

async def delete_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid = _uid(update)
    sn  = active_sheet_name(uid)
    row = delete_last_row(sn)
    if not row:
        await update.message.reply_text(f"Nothing to delete in *{sn}*.", parse_mode="Markdown")
        return
    await update.message.reply_text(
        f"🗑️ *Deleted from {sn}:*\n  {row[1]} — ₹{row[2]} — {row[3]} ({row[0]})",
        parse_mode="Markdown")

async def set_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    global MONTHLY_BUDGET
    try:
        MONTHLY_BUDGET = float(ctx.args[0])
        uid = _uid(update)
        await update.message.reply_text(
            f"✅ Monthly budget: *₹{MONTHLY_BUDGET:,.2f}*\n"
            f"This month spent: ₹{get_month_total(active_sheet_name(uid)):,.2f}",
            parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /setbudget 20000")

async def recurring_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    try:
        amount      = float(ctx.args[0])
        category    = ctx.args[1].capitalize()
        frequency   = ctx.args[-1].lower()
        description = " ".join(ctx.args[2:-1])
        if category not in ["Food","Transport","Shopping","Entertainment","Health","Bills","Other"]:
            category = "Other"
        if frequency not in ["monthly","weekly","yearly"]:
            frequency = "monthly"
        add_recurring(amount, category, description, frequency)
        await update.message.reply_text(
            f"🔁 *Recurring added!*\n{_emoji(category)} {description}: ₹{amount:,.2f} ({frequency})",
            parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/recurring 649 Entertainment Netflix monthly`\nFrequencies: monthly, weekly, yearly",
            parse_mode="Markdown")

async def list_recurring(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    items = get_recurring()
    if not items:
        await update.message.reply_text("No recurring expenses.\nAdd: /recurring 649 Entertainment Netflix monthly")
        return
    lines = ["🔁 *Recurring Expenses:*\n"]
    for r in items:
        lines.append(f"  {_emoji(r[1])} {r[2]} — ₹{float(r[0]):,.2f} ({r[3]}), next: {r[4]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ensure_header("Sheet1")
    get_or_create_sheet("Recurring")
    due = process_due_recurring("Sheet1")
    if due:
        logger.info(f"Auto-logged recurring: {due}")

    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start",         start))
    app.add_handler(CommandHandler("help",          help_cmd))
    app.add_handler(CommandHandler("sheets",        sheets_menu))
    app.add_handler(CommandHandler("summary",       summary))
    app.add_handler(CommandHandler("weekly",        weekly))
    app.add_handler(CommandHandler("monthly",       monthly))
    app.add_handler(CommandHandler("delete",        delete_last))
    app.add_handler(CommandHandler("setbudget",     set_budget))
    app.add_handler(CommandHandler("recurring",     recurring_cmd))
    app.add_handler(CommandHandler("listrecurring", list_recurring))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
