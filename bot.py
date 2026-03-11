import os
import json
import logging
import httpx
import base64
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from dateutil.relativedelta import relativedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds  = Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"]), scopes=SCOPES)
gc = gspread.authorize(creds)
wb = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])

_raw_uid = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()
try:    ALLOWED_USER_ID = int(_raw_uid) if _raw_uid else 0
except ValueError: ALLOWED_USER_ID = 0

MONTHLY_BUDGET = float(os.environ.get("MONTHLY_BUDGET", "0"))

# ── Sheet state ───────────────────────────────────────────────────────────────
RESERVED = {"Recurring", "Meta"}
HEADER   = ["Date", "Category", "Amount", "Description", "Type", "Added At"]
_active_sheet: dict[int, str] = {}

def active_sheet_name(uid: int) -> str:
    return _active_sheet.get(uid, "Sheet1")

def set_active_sheet(uid: int, name: str):
    _active_sheet[uid] = name

# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_EXPENSE = {"red": 1.0,  "green": 0.85, "blue": 0.85}  # light red
COLOR_INCOME  = {"red": 0.85, "green": 1.0,  "blue": 0.85}  # light green
COLOR_HEADER  = {"red": 0.27, "green": 0.51, "blue": 0.71}  # blue header
COLOR_WHITE   = {"red": 1.0,  "green": 1.0,  "blue": 1.0}

def color_row(ws: gspread.Worksheet, row_idx: int, is_income: bool):
    """Color a data row red (expense) or green (income)."""
    try:
        color = COLOR_INCOME if is_income else COLOR_EXPENSE
        ws.format(f"A{row_idx}:F{row_idx}", {
            "backgroundColor": color,
            "textFormat": {"fontSize": 10}
        })
    except Exception as e:
        logger.warning(f"Color row failed: {e}")

def setup_sheet_formatting(ws: gspread.Worksheet):
    """Apply header formatting and freeze top row."""
    try:
        ws.format("A1:F1", {
            "backgroundColor": COLOR_HEADER,
            "textFormat": {"bold": True, "foregroundColor": COLOR_WHITE, "fontSize": 11},
            "horizontalAlignment": "CENTER"
        })
        wb.batch_update({"requests": [{
            "updateSheetProperties": {
                "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"
            }
        }]})
    except Exception as e:
        logger.warning(f"Sheet formatting failed: {e}")

# ── Sheet helpers ─────────────────────────────────────────────────────────────
def get_or_create_sheet(name: str) -> gspread.Worksheet:
    try:
        return wb.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title=name, rows=1000, cols=10)
        if name not in RESERVED:
            ws.append_row(HEADER)
            setup_sheet_formatting(ws)
        elif name == "Recurring":
            ws.append_row(["Amount", "Category", "Description", "Frequency", "Next Date", "Active"])
        logger.info(f"Created sheet: {name}")
        return ws

def list_expense_sheets() -> list[str]:
    return [ws.title for ws in wb.worksheets() if ws.title not in RESERVED]

def rename_sheet(old: str, new: str) -> bool:
    if old in RESERVED or new in RESERVED:
        return False
    try:
        wb.worksheet(old).update_title(new)
        return True
    except Exception as e:
        logger.error(f"Rename error: {e}"); return False

def delete_sheet(name: str) -> bool:
    if name in RESERVED or name == "Sheet1":
        return False
    try:
        wb.del_worksheet(wb.worksheet(name))
        return True
    except Exception as e:
        logger.error(f"Delete error: {e}"); return False

def ensure_header(sheet_name="Sheet1"):
    ws = get_or_create_sheet(sheet_name)
    cell = ws.cell(1, 1).value
    if not cell or cell.strip().lower() != "date":
        ws.insert_row(HEADER, 1)
        setup_sheet_formatting(ws)

def append_expense(date_str, category, amount, description, sheet_name="Sheet1", entry_type="expense"):
    ws = get_or_create_sheet(sheet_name)
    ws.append_row([
        date_str, category, float(amount), description,
        entry_type.capitalize(),
        datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    ])
    # Color the newly added row
    row_idx = len(ws.get_all_values())
    color_row(ws, row_idx, is_income=(entry_type == "income"))

def get_data_rows(sheet_name="Sheet1") -> list:
    ws = get_or_create_sheet(sheet_name)
    return [r for r in ws.get_all_values()
            if r and r[0].strip().lower() != "date" and r[0].strip()]

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
    # Only sum expenses (negative or Type==expense), not income
    total = 0.0
    for r in get_data_rows(sheet_name):
        if not r[0].startswith(prefix) or len(r) < 3:
            continue
        try:
            amt = float(r[2])
            entry_type = r[4].strip().lower() if len(r) > 4 else ("income" if amt > 0 else "expense")
            if entry_type == "expense":
                total += abs(amt)
        except: continue
    return total

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

# ── Recurring ─────────────────────────────────────────────────────────────────
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
                entry_type = "income" if amount > 0 else "expense"
                append_expense(today_str, category, abs(amount), f"[Auto] {description}", sheet_name, entry_type)
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
    try:
        result = json.loads(raw)
        if isinstance(result, list): return result
        if isinstance(result, dict): return [result]
    except: pass
    s, e = raw.find("["), raw.rfind("]") + 1
    if s != -1 and e > 0:
        try: return json.loads(raw[s:e])
        except: pass
    objects = re.findall(r'\{[^{}]+\}', raw, re.DOTALL)
    result = []
    for obj in objects:
        try: result.append(json.loads(obj))
        except: pass
    return result

def detect_sign_prefix(text: str):
    """
    Check if message starts with + or - to force income/expense.
    Returns (cleaned_text, forced_type) where forced_type is 'income', 'expense', or None.
    """
    t = text.strip()
    if t.startswith("+"):
        return t[1:].strip(), "income"
    if t.startswith("-"):
        return t[1:].strip(), "expense"
    return t, None

def parse_expenses(text: str) -> list[dict]:
    """Parse text, respecting +/- prefix for income/expense."""
    cleaned, forced_type = detect_sign_prefix(text)
    today = datetime.now(IST).strftime("%Y-%m-%d")

    # If no number at all in cleaned text, skip AI call
    if not re.search(r'\d', cleaned):
        return []

    raw = _groq([
        {"role": "system", "content": "Expense/income parser. Extract entries with explicit numeric amounts. Return ONLY a JSON array."},
        {"role": "user", "content": f"""Extract ALL entries from: "{cleaned}"
Today: {today}. Only if explicit number present, else return [].
Format: [{{"amount":250,"category":"Food","description":"lunch","date":"{today}","type":"expense"}}]
- type must be "income" or "expense"
- For payments TO someone (e.g. 'paid Person', 'gave money') → type: "expense"
- For received money (e.g. 'received', 'got paid') → type: "income"
Categories: Food,Transport,Shopping,Entertainment,Health,Bills,Transfer,Other
Return [] if nothing found."""}
    ])
    entries = [x for x in _extract_json_list(raw) if x.get("amount")]

    # Override type if user used +/- prefix
    if forced_type:
        for e in entries:
            e["type"] = forced_type

    return entries

def parse_receipt_image(image_bytes: bytes) -> list[dict]:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    b64   = base64.b64encode(image_bytes).decode()
    raw   = _groq([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": f"Receipt/bill. Extract ALL items. Today:{today}. Return ONLY JSON: [{{\"amount\":250,\"category\":\"Food\",\"description\":\"item\",\"date\":\"{today}\",\"type\":\"expense\"}}]. Return [] if not receipt."}
    ]}], model="llama-3.2-11b-vision-preview")
    return [x for x in _extract_json_list(raw) if x.get("amount")]

# ── Report builder ────────────────────────────────────────────────────────────
def build_report(label: str, rows: list, sheet_name="Sheet1") -> str:
    if not rows:
        return f"No entries in *{label}*."

    expenses_by_cat: dict[str, float] = {}
    income_by_cat:   dict[str, float] = {}
    total_expense = 0.0
    total_income  = 0.0

    for row in rows:
        try:
            amt  = abs(float(row[2]))
            cat  = row[1]
            etype = row[4].strip().lower() if len(row) > 4 else "expense"
            if etype == "income":
                income_by_cat[cat]   = income_by_cat.get(cat, 0) + amt
                total_income += amt
            else:
                expenses_by_cat[cat] = expenses_by_cat.get(cat, 0) + amt
                total_expense += amt
        except: continue

    lines = [f"📊 *{label}* — _{sheet_name}_ ({len(rows)} entries)\n"]

    if expenses_by_cat:
        lines.append("🔴 *Expenses:*")
        for cat, amt in sorted(expenses_by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"  {_emoji(cat)} {cat}: ₹{amt:,.2f}")
        lines.append(f"  💸 Total: ₹{total_expense:,.2f}\n")

    if income_by_cat:
        lines.append("🟢 *Income:*")
        for cat, amt in sorted(income_by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"  💰 {cat}: ₹{amt:,.2f}")
        lines.append(f"  📈 Total: ₹{total_income:,.2f}\n")

    net = total_income - total_expense
    net_emoji = "✅" if net >= 0 else "⚠️"
    lines.append(f"{net_emoji} *Net: ₹{net:,.2f}*")

    if MONTHLY_BUDGET > 0 and "Month" in label:
        pct = (total_expense / MONTHLY_BUDGET) * 100
        bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
        lines.append(f"📉 Budget: [{bar}] {pct:.0f}% of ₹{MONTHLY_BUDGET:,.2f}")

    return "\n".join(lines)

def _emoji(cat):
    return {"Food":"🍔","Transport":"🚗","Shopping":"🛍️","Entertainment":"🎬",
            "Health":"💊","Bills":"📄","Transfer":"💸"}.get(cat, "📌")

def _allowed(update: Update) -> bool:
    return ALLOWED_USER_ID == 0 or update.effective_user.id == ALLOWED_USER_ID

def _uid(update: Update) -> int:
    return update.effective_user.id

# ── /sheets menu ──────────────────────────────────────────────────────────────
async def sheets_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid = _uid(update)
    current = active_sheet_name(uid)
    all_sheets = list_expense_sheets()
    text = f"📂 *Sheet Manager*\n\nActive: *{current}*\nSheets: {', '.join(all_sheets)}\n"
    kb = [
        [InlineKeyboardButton("➕ New sheet",      callback_data="sheet:new"),
         InlineKeyboardButton("🔀 Switch sheet",   callback_data="sheet:switch")],
        [InlineKeyboardButton("✏️ Rename sheet",   callback_data="sheet:rename"),
         InlineKeyboardButton("🗑️ Delete sheet",   callback_data="sheet:delete")],
        [InlineKeyboardButton("📊 Compare sheets", callback_data="sheet:compare")],
    ]
    await update.message.reply_text(text, parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(kb))

# ── Callback handler ──────────────────────────────────────────────────────────
async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    data = q.data

    def main_kb():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ New sheet",      callback_data="sheet:new"),
             InlineKeyboardButton("🔀 Switch sheet",   callback_data="sheet:switch")],
            [InlineKeyboardButton("✏️ Rename sheet",   callback_data="sheet:rename"),
             InlineKeyboardButton("🗑️ Delete sheet",   callback_data="sheet:delete")],
            [InlineKeyboardButton("📊 Compare sheets", callback_data="sheet:compare")],
        ])

    if data == "sheet:new":
        ctx.user_data["awaiting"] = "new_sheet_name"
        await q.edit_message_text("📝 Send the name for your new sheet:\n_(e.g. Work, Travel, March2026)_",
                                  parse_mode="Markdown")

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
        await q.edit_message_text(f"✅ Switched to *{name}*", parse_mode="Markdown")

    elif data == "sheet:rename":
        sheets = [s for s in list_expense_sheets() if s != "Sheet1"]
        if not sheets:
            await q.edit_message_text("No sheets to rename (Sheet1 is protected)."); return
        kb = [[InlineKeyboardButton(s, callback_data=f"sheet:renamepick:{s}")] for s in sheets]
        kb.append([InlineKeyboardButton("« Back", callback_data="sheet:back")])
        await q.edit_message_text("✏️ *Which sheet to rename?*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("sheet:renamepick:"):
        old = data[len("sheet:renamepick:"):]
        ctx.user_data["awaiting"] = "rename_sheet"
        ctx.user_data["rename_target"] = old
        await q.edit_message_text(f"✏️ Send the new name for *{old}*:", parse_mode="Markdown")

    elif data == "sheet:delete":
        sheets = [s for s in list_expense_sheets() if s != "Sheet1"]
        if not sheets:
            await q.edit_message_text("No sheets to delete (Sheet1 is protected)."); return
        kb = [[InlineKeyboardButton(f"🗑️ {s}", callback_data=f"sheet:delconfirm:{s}")] for s in sheets]
        kb.append([InlineKeyboardButton("« Back", callback_data="sheet:back")])
        await q.edit_message_text("🗑️ *Which sheet to delete?*\n⚠️ Cannot be undone!",
                                  parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("sheet:delconfirm:"):
        name = data[len("sheet:delconfirm:"):]
        kb = [[InlineKeyboardButton("⚠️ Yes, delete", callback_data=f"sheet:dodelete:{name}"),
               InlineKeyboardButton("Cancel",         callback_data="sheet:back")]]
        await q.edit_message_text(f"⚠️ Delete *{name}* and ALL its data?",
                                  parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("sheet:dodelete:"):
        name = data[len("sheet:dodelete:"):]
        if delete_sheet(name):
            if active_sheet_name(uid) == name:
                set_active_sheet(uid, "Sheet1")
            await q.edit_message_text(f"🗑️ *{name}* deleted. Switched to Sheet1.", parse_mode="Markdown")
        else:
            await q.edit_message_text("❌ Cannot delete (Sheet1 is protected).")

    elif data == "sheet:compare":
        sheets = list_expense_sheets()
        lines  = ["📊 *All Sheets Comparison*\n"]
        for s in sheets:
            rows  = get_data_rows(s)
            exp   = sum(abs(float(r[2])) for r in rows if len(r) > 4 and r[4].lower() == "expense")
            inc   = sum(abs(float(r[2])) for r in rows if len(r) > 4 and r[4].lower() == "income")
            mark  = " ✅" if s == active_sheet_name(uid) else ""
            lines.append(f"  📂 *{s}*{mark}\n    🔴 ₹{exp:,.2f}  🟢 ₹{inc:,.2f}  ({len(rows)} entries)")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown")

    elif data == "sheet:back":
        current = active_sheet_name(uid)
        all_sheets = list_expense_sheets()
        text = f"📂 *Sheet Manager*\n\nActive: *{current}*\nSheets: {', '.join(all_sheets)}\n"
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=main_kb())

# ── Message handler ───────────────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ Unauthorized."); return

    uid  = _uid(update)
    text = update.message.text.strip()

    awaiting = ctx.user_data.get("awaiting")

    if awaiting == "new_sheet_name":
        ctx.user_data.pop("awaiting")
        name = text.strip()
        if name in RESERVED:
            await update.message.reply_text("❌ That name is reserved."); return
        ensure_header(name)
        set_active_sheet(uid, name)
        await update.message.reply_text(
            f"✅ Sheet *{name}* created and set as active!", parse_mode="Markdown")
        return

    if awaiting == "rename_sheet":
        ctx.user_data.pop("awaiting")
        old = ctx.user_data.pop("rename_target", None)
        new = text.strip()
        if not old:
            await update.message.reply_text("Something went wrong. Try /sheets again."); return
        if rename_sheet(old, new):
            if active_sheet_name(uid) == old:
                set_active_sheet(uid, new)
            await update.message.reply_text(f"✅ Renamed *{old}* → *{new}*", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Rename failed.")
        return

    sheet_name = active_sheet_name(uid)
    await update.message.reply_chat_action("typing")

    try:
        entries = parse_expenses(text)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: `{str(e)[:200]}`", parse_mode="Markdown")
        return

    if not entries:
        await update.message.reply_text(
            "🤔 No entry found. Include a number!\n"
            "• Expense: _Spent 300 on dinner_ or _-300 dinner_\n"
            "• Income: _Received 5000 salary_ or _+5000 salary_",
            parse_mode="Markdown")
        return

    lines = [f"✅ *Logged to {sheet_name}!*\n"]
    for e in entries:
        etype     = e.get("type", "expense").lower()
        amt       = abs(float(e["amount"]))
        icon      = "🟢" if etype == "income" else "🔴"
        type_label = "Income" if etype == "income" else "Expense"
        append_expense(e["date"], e["category"], amt, e["description"], sheet_name, etype)
        lines.append(f"{icon} {type_label} — {_emoji(e['category'])} {e['category']}: ₹{amt:,.2f} — {e['description']}")

    lines.append(f"\n📋 {len(entries)} entr{'y' if len(entries)==1 else 'ies'} added!")
    await update.message.reply_text("\n".join(lines) + budget_alert_msg(sheet_name), parse_mode="Markdown")

# ── Photo handler ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text("⛔ Unauthorized."); return
    uid        = _uid(update)
    sheet_name = active_sheet_name(uid)
    await update.message.reply_chat_action("upload_photo")
    try:
        photo     = update.message.photo[-1]
        file      = await ctx.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        entries   = parse_receipt_image(bytes(img_bytes))
    except Exception as e:
        await update.message.reply_text(f"⚠️ Receipt error: `{str(e)[:200]}`", parse_mode="Markdown")
        return
    if not entries:
        await update.message.reply_text("🤔 Couldn't read receipt. Try a clearer photo.")
        return
    lines = [f"🧾 *Receipt → {sheet_name}!*\n"]
    for e in entries:
        etype = e.get("type", "expense").lower()
        amt   = abs(float(e["amount"]))
        icon  = "🟢" if etype == "income" else "🔴"
        append_expense(e["date"], e["category"], amt, e["description"], sheet_name, etype)
        lines.append(f"{icon} {_emoji(e['category'])} {e['category']}: ₹{amt:,.2f} — {e['description']}")
    lines.append(f"\n📋 {len(entries)} item(s) logged!")
    await update.message.reply_text("\n".join(lines) + budget_alert_msg(sheet_name), parse_mode="Markdown")

# ── Commands ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = _uid(update)
    await update.message.reply_text(
        "👋 *Expense Tracker Bot*\n\n"
        "📝 *Log entries:*\n"
        "  _Spent 250 on lunch_ or _-250 lunch_\n"
        "  _Received 5000 salary_ or _+5000 salary_\n"
        "  _-1000 Person_ → 🔴 expense\n"
        "  _+2000 Person_ → 🟢 income\n"
        "  📸 Send a receipt photo!\n\n"
        "📂 *Sheets:* /sheets\n"
        f"  Active: *{active_sheet_name(uid)}*\n\n"
        "📊 *Reports:* /summary  /weekly  /monthly\n\n"
        "⚙️ *Other:*\n"
        "  /delete – Remove last entry\n"
        "  /recurring 649 Entertainment Netflix monthly\n"
        "  /listrecurring  /setbudget 20000",
        parse_mode="Markdown")

async def help_cmd(update, ctx): await start(update, ctx)

async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid = _uid(update); sn = active_sheet_name(uid)
    await update.message.reply_text(build_report("All Time", get_data_rows(sn), sn), parse_mode="Markdown")

async def weekly(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid = _uid(update); sn = active_sheet_name(uid)
    week_ago = (datetime.now(IST).date() - timedelta(days=7)).isoformat()
    rows = [r for r in get_data_rows(sn) if r[0] >= week_ago]
    await update.message.reply_text(build_report("Weekly", rows, sn), parse_mode="Markdown")

async def monthly(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid = _uid(update); sn = active_sheet_name(uid)
    now = datetime.now(IST); prefix = f"{now.year}-{now.month:02d}"
    rows = [r for r in get_data_rows(sn) if r[0].startswith(prefix)]
    await update.message.reply_text(build_report(now.strftime("%B %Y"), rows, sn), parse_mode="Markdown")

async def delete_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid = _uid(update); sn = active_sheet_name(uid)
    row = delete_last_row(sn)
    if not row:
        await update.message.reply_text(f"Nothing to delete in *{sn}*.", parse_mode="Markdown"); return
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
            f"✅ Budget: *₹{MONTHLY_BUDGET:,.2f}*\nThis month: ₹{get_month_total(active_sheet_name(uid)):,.2f}",
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
        if category not in ["Food","Transport","Shopping","Entertainment","Health","Bills","Transfer","Other"]:
            category = "Other"
        if frequency not in ["monthly","weekly","yearly"]:
            frequency = "monthly"
        add_recurring(amount, category, description, frequency)
        await update.message.reply_text(
            f"🔁 *Recurring added!*\n{_emoji(category)} {description}: ₹{amount:,.2f} ({frequency})",
            parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: `/recurring 649 Entertainment Netflix monthly`",
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
    if due: logger.info(f"Auto-logged recurring: {due}")

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
