import os
import json
import logging
import httpx
import base64
import re
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple, Any
from functools import wraps, lru_cache
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import gspread
from gspread.utils import rowcol_to_a1
from gspread.exceptions import APIError, WorksheetNotFound
from google.oauth2.service_account import Credentials
from dateutil.relativedelta import relativedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
IST = ZoneInfo("Asia/Kolkata")
MAX_RETRIES = 3
RETRY_DELAY = 1.0
TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_LOGS_LIMIT = 30
API_TIMEOUT = 30
GROQ_MODEL_DEFAULT = "llama-3.1-8b-instant"
GROQ_MODEL_VISION = "llama-3.2-11b-vision-preview"
VALID_CATEGORIES = frozenset([
    "Food", "Transport", "Shopping", "Entertainment",
    "Health", "Bills", "Transfer", "Other"
])
VALID_FREQUENCIES = frozenset(["monthly", "weekly", "yearly"])

# ── Config ────────────────────────────────────────────────────────────────────
def get_env_variable(name: str, default: str = "", required: bool = True) -> str:
    """Safely get environment variable with proper error handling."""
    value = os.environ.get(name, default).strip()
    if required and not value:
        raise ValueError(f"Required environment variable '{name}' is not set")
    return value

GROQ_API_KEY = get_env_variable("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

try:
    creds_json = json.loads(get_env_variable("GOOGLE_CREDENTIALS_JSON"))
    creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    gc = gspread.authorize(creds)
    wb = gc.open_by_key(get_env_variable("GOOGLE_SHEET_ID"))
except (json.JSONDecodeError, ValueError) as e:
    logger.error(f"Error initializing Google Sheets: {e}")
    raise

_raw_uid = get_env_variable("ALLOWED_TELEGRAM_USER_ID", default="0", required=False)
try:
    ALLOWED_USER_ID = int(_raw_uid) if _raw_uid else 0
except ValueError:
    logger.warning(f"Invalid ALLOWED_TELEGRAM_USER_ID: {_raw_uid}, defaulting to 0")
    ALLOWED_USER_ID = 0

try:
    MONTHLY_BUDGET = float(get_env_variable("MONTHLY_BUDGET", default="0", required=False))
except ValueError:
    logger.warning("Invalid MONTHLY_BUDGET, defaulting to 0")
    MONTHLY_BUDGET = 0.0

# ── Sheet state ───────────────────────────────────────────────────────────────
RESERVED = frozenset({"Recurring", "Meta"})
HEADER = ["Date", "Category", "Amount", "Description", "Type", "Added At"]
RECURRING_HEADER = ["Amount", "Category", "Description", "Frequency", "Next Date", "Active"]
_active_sheet: Dict[int, str] = {}
_worksheet_cache: Dict[str, Tuple[gspread.Worksheet, float]] = {}
CACHE_TTL = 300  # 5 minutes

def active_sheet_name(uid: int) -> str:
    """Get the active sheet name for a user."""
    return _active_sheet.get(uid, "Sheet1")

def set_active_sheet(uid: int, name: str) -> None:
    """Set the active sheet for a user."""
    if not name or not isinstance(name, str):
        raise ValueError("Sheet name must be a non-empty string")
    _active_sheet[uid] = name

def retry_on_error(max_attempts: int = MAX_RETRIES, delay: float = RETRY_DELAY):
    """Decorator to retry functions on API errors."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (APIError, httpx.HTTPError, ConnectionError) as e:
                    if attempt == max_attempts - 1:
                        logger.error(f"{func.__name__} failed after {max_attempts} attempts: {e}")
                        raise
                    logger.warning(f"{func.__name__} attempt {attempt + 1} failed: {e}. Retrying...")
                    time.sleep(delay * (attempt + 1))
            return None
        return wrapper
    return decorator

def get_cached_worksheet(name: str) -> Optional[gspread.Worksheet]:
    """Get a worksheet from cache if available and not expired."""
    if name in _worksheet_cache:
        ws, timestamp = _worksheet_cache[name]
        if time.time() - timestamp < CACHE_TTL:
            return ws
    return None

def cache_worksheet(name: str, ws: gspread.Worksheet) -> None:
    """Cache a worksheet with current timestamp."""
    _worksheet_cache[name] = (ws, time.time())

def clear_worksheet_cache(name: Optional[str] = None) -> None:
    """Clear worksheet cache for a specific sheet or all sheets."""
    if name:
        _worksheet_cache.pop(name, None)
    else:
        _worksheet_cache.clear()

# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_EXPENSE = {"red": 1.0,  "green": 0.85, "blue": 0.85}  # light red
COLOR_INCOME  = {"red": 0.85, "green": 1.0,  "blue": 0.85}  # light green
COLOR_HEADER  = {"red": 0.27, "green": 0.51, "blue": 0.71}  # blue header
COLOR_WHITE   = {"red": 1.0,  "green": 1.0,  "blue": 1.0}

@retry_on_error(max_attempts=2, delay=0.5)
def color_row(ws: gspread.Worksheet, row_idx: int, is_income: bool) -> None:
    """Color a data row red (expense) or green (income).
    
    Args:
        ws: The worksheet to format
        row_idx: The row index to color (1-based)
        is_income: True for income (green), False for expense (red)
    """
    try:
        color = COLOR_INCOME if is_income else COLOR_EXPENSE
        ws.format(f"A{row_idx}:F{row_idx}", {
            "backgroundColor": color,
            "textFormat": {"fontSize": 10}
        })
    except Exception as e:
        logger.warning(f"Failed to color row {row_idx}: {e}")

@retry_on_error(max_attempts=2, delay=0.5)
def setup_sheet_formatting(ws: gspread.Worksheet) -> None:
    """Apply header formatting and freeze top row.
    
    Args:
        ws: The worksheet to format
    """
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
        logger.warning(f"Sheet formatting failed for '{ws.title}': {e}")

# ── Sheet helpers ─────────────────────────────────────────────────────────────
@retry_on_error()
def get_or_create_sheet(name: str) -> gspread.Worksheet:
    """Get an existing worksheet or create a new one with proper headers.
    
    Args:
        name: The name of the worksheet
        
    Returns:
        The worksheet object
        
    Raises:
        ValueError: If name is empty or invalid
    """
    if not name or not isinstance(name, str):
        raise ValueError("Sheet name must be a non-empty string")
    
    # Check cache first
    cached = get_cached_worksheet(name)
    if cached:
        return cached
    
    try:
        ws = wb.worksheet(name)
        cache_worksheet(name, ws)
        return ws
    except WorksheetNotFound:
        logger.info(f"Creating new sheet: {name}")
        ws = wb.add_worksheet(title=name, rows=1000, cols=10)
        
        if name not in RESERVED:
            ws.append_row(HEADER)
            setup_sheet_formatting(ws)
        elif name == "Recurring":
            ws.append_row(RECURRING_HEADER)
            
        cache_worksheet(name, ws)
        logger.info(f"Successfully created sheet: {name}")
        return ws

@retry_on_error()
def list_expense_sheets() -> List[str]:
    """Get all non-reserved worksheet names.
    
    Returns:
        List of sheet names excluding reserved sheets
    """
    try:
        return [ws.title for ws in wb.worksheets() if ws.title not in RESERVED]
    except Exception as e:
        logger.error(f"Failed to list sheets: {e}")
        return ["Sheet1"]

@retry_on_error()
def rename_sheet(old: str, new: str) -> bool:
    """Rename a worksheet.
    
    Args:
        old: Current sheet name
        new: New sheet name
        
    Returns:
        True if successful, False otherwise
    """
    if not old or not new:
        logger.warning("Sheet names cannot be empty")
        return False
    if old in RESERVED or new in RESERVED:
        logger.warning(f"Cannot rename reserved sheets: {old} -> {new}")
        return False
    if old == new:
        return True
        
    try:
        wb.worksheet(old).update_title(new)
        clear_worksheet_cache(old)
        logger.info(f"Renamed sheet: {old} -> {new}")
        return True
    except Exception as e:
        logger.error(f"Rename error ({old} -> {new}): {e}")
        return False

@retry_on_error()
def delete_sheet(name: str) -> bool:
    """Delete a worksheet if not protected.
    
    Args:
        name: Sheet name to delete
        
    Returns:
        True if successful, False otherwise
    """
    if not name:
        return False
    if name in RESERVED or name == "Sheet1":
        logger.warning(f"Cannot delete protected sheet: {name}")
        return False
        
    try:
        wb.del_worksheet(wb.worksheet(name))
        clear_worksheet_cache(name)
        logger.info(f"Deleted sheet: {name}")
        return True
    except Exception as e:
        logger.error(f"Delete error for {name}: {e}")
        return False

@retry_on_error()
def ensure_header(sheet_name: str = "Sheet1") -> None:
    """Ensure a sheet has the proper header row.
    
    Args:
        sheet_name: Name of the sheet to check
    """
    try:
        ws = get_or_create_sheet(sheet_name)
        cell = ws.cell(1, 1).value
        if not cell or cell.strip().lower() != "date":
            ws.insert_row(HEADER, 1)
            setup_sheet_formatting(ws)
            logger.info(f"Added header to sheet: {sheet_name}")
    except Exception as e:
        logger.error(f"Failed to ensure header for {sheet_name}: {e}")

@retry_on_error()
def append_expense(
    date_str: str,
    category: str,
    amount: float,
    description: str,
    sheet_name: str = "Sheet1",
    entry_type: str = "expense"
) -> None:
    """Append an expense or income entry to a sheet.
    
    Args:
        date_str: Date in YYYY-MM-DD format
        category: Category name
        amount: Transaction amount (positive)
        description: Transaction description
        sheet_name: Target sheet name
        entry_type: 'expense' or 'income'
    """
    # Validate inputs
    if not date_str or not category or amount < 0:
        raise ValueError("Invalid expense data")
    
    if entry_type not in ("expense", "income"):
        logger.warning(f"Invalid entry_type: {entry_type}, defaulting to expense")
        entry_type = "expense"
    
    ws = get_or_create_sheet(sheet_name)
    # Store positive for income, negative for expense
    signed = abs(float(amount)) if entry_type == "income" else -abs(float(amount))
    
    row_data = [
        date_str,
        category,
        signed,
        description,
        entry_type.capitalize(),
        datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    ]
    
    ws.append_row(row_data)
    
    # Color the newly added row
    row_idx = len(ws.get_all_values())
    color_row(ws, row_idx, is_income=(entry_type == "income"))
    
    logger.info(f"Added {entry_type}: {category} ₹{amount} to {sheet_name}")

@retry_on_error()
def get_data_rows(sheet_name: str = "Sheet1") -> List[List[str]]:
    """Get all data rows from a sheet (excluding header).
    
    Args:
        sheet_name: Name of the sheet
        
    Returns:
        List of data rows
    """
    try:
        ws = get_or_create_sheet(sheet_name)
        all_rows = ws.get_all_values()
        return [
            r for r in all_rows
            if r and r[0].strip().lower() != "date" and r[0].strip()
        ]
    except Exception as e:
        logger.error(f"Failed to get data rows from {sheet_name}: {e}")
        return []

@retry_on_error()
def delete_last_row(sheet_name: str = "Sheet1") -> Optional[List[str]]:
    """Delete the last data row from a sheet.
    
    Args:
        sheet_name: Name of the sheet
        
    Returns:
        The deleted row data, or None if no rows to delete
    """
    try:
        ws = get_or_create_sheet(sheet_name)
        rows = ws.get_all_values()
        data = [
            (i+1, r) for i, r in enumerate(rows)
            if r and r[0].strip().lower() != "date" and r[0].strip()
        ]
        if not data:
            return None
        row_idx, row = data[-1]
        ws.delete_rows(row_idx)
        logger.info(f"Deleted row {row_idx} from {sheet_name}")
        return row
    except Exception as e:
        logger.error(f"Failed to delete last row from {sheet_name}: {e}")
        return None

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
def sanitize_input(text: str, max_length: int = 1000) -> str:
    """Sanitize user input for AI processing.
    
    Args:
        text: Input text to sanitize
        max_length: Maximum allowed length
        
    Returns:
        Sanitized text
    """
    if not text:
        return ""
    # Truncate if too long
    text = text[:max_length]
    # Remove potential injection patterns
    text = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F]', '', text)
    return text.strip()

@retry_on_error(max_attempts=2, delay=1.0)
def _groq(
    messages: List[Dict[str, Any]],
    max_tokens: int = 500,
    model: str = GROQ_MODEL_DEFAULT
) -> str:
    """Make a request to Groq API with retry logic.
    
    Args:
        messages: List of message dictionaries
        max_tokens: Maximum tokens in response
        model: Model name to use
        
    Returns:
        Response text from the API
        
    Raises:
        httpx.HTTPError: If the request fails after retries
    """
    try:
        response = httpx.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": max_tokens
            },
            timeout=API_TIMEOUT
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError, IndexError) as e:
        logger.error(f"Groq API error: {e}")
        raise

def _extract_json_list(raw: str) -> List[Dict[str, Any]]:
    """Extract JSON objects from a string that may contain markdown or extra text.
    
    Args:
        raw: Raw string potentially containing JSON
        
    Returns:
        List of extracted JSON objects
    """
    raw = raw.replace("```json", "").replace("```", "").strip()
    
    # Try parsing the whole string as JSON first
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    except json.JSONDecodeError:
        pass
    
    # Try finding JSON array
    s, e = raw.find("["), raw.rfind("]") + 1
    if s != -1 and e > 0:
        try:
            return json.loads(raw[s:e])
        except json.JSONDecodeError:
            pass
    
    # Extract individual JSON objects
    objects = re.findall(r'\{[^{}]+\}', raw, re.DOTALL)
    result = []
    for obj in objects:
        try:
            result.append(json.loads(obj))
        except json.JSONDecodeError:
            continue
    
    return result

def detect_sign_prefix(text: str) -> Tuple[str, Optional[str]]:
    """Check if message starts with + or - to force income/expense.
    
    Args:
        text: Input text to check
        
    Returns:
        Tuple of (cleaned_text, forced_type) where forced_type is 'income', 'expense', or None
    """
    t = text.strip()
    if t.startswith("+"):
        return t[1:].strip(), "income"
    if t.startswith("-"):
        return t[1:].strip(), "expense"
    return t, None

def parse_expenses(text: str) -> List[Dict[str, Any]]:
    """Parse text to extract expense/income entries using AI.
    
    Args:
        text: User input text
        
    Returns:
        List of expense/income entry dictionaries
    """
    # Sanitize input
    text = sanitize_input(text)
    if not text:
        return []
    
    cleaned, forced_type = detect_sign_prefix(text)
    today = datetime.now(IST).strftime("%Y-%m-%d")

    # If no number at all in cleaned text, skip AI call
    if not re.search(r'\d', cleaned):
        return []

    try:
        raw = _groq([
            {"role": "system", "content": "Expense/income parser. Extract entries with explicit numeric amounts. Return ONLY a JSON array."},
            {"role": "user", "content": f"""Extract ALL entries from: "{cleaned}"
Today: {today}. Only if explicit number present, else return [].
Format: [{{"amount":250,"category":"Food","description":"lunch","date":"{today}","type":"expense"}}]
- type must be "income" or "expense"
- For payments TO someone (e.g. 'paid Person', 'gave money') → type: "expense"
- For received money (e.g. 'received', 'got paid') → type: "income"
- description = ONLY the person/item name. NEVER include the number in description.
  Example: "-1000 Person" → amount:1000, description:"Person" (NOT "1000 Person")
Categories: {', '.join(VALID_CATEGORIES)}
Return [] if nothing found."""}
        ])
        entries = [x for x in _extract_json_list(raw) if x.get("amount")]

        # Override type if user used +/- prefix
        if forced_type:
            for e in entries:
                e["type"] = forced_type

        return entries
    except Exception as e:
        logger.error(f"Failed to parse expenses from text: {e}")
        return []

def parse_receipt_image(image_bytes: bytes) -> List[Dict[str, Any]]:
    """Parse a receipt image to extract expense entries using AI vision.
    
    Args:
        image_bytes: Image data as bytes
        
    Returns:
        List of expense entry dictionaries
    """
    if not image_bytes:
        return []
    
    try:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        b64 = base64.b64encode(image_bytes).decode()
        raw = _groq(
            [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": f"Receipt/bill. Extract ALL items. Today:{today}. Return ONLY JSON: [{{\"amount\":250,\"category\":\"Food\",\"description\":\"item\",\"date\":\"{today}\",\"type\":\"expense\"}}]. Return [] if not receipt."}
            ]}],
            model=GROQ_MODEL_VISION
        )
        return [x for x in _extract_json_list(raw) if x.get("amount")]
    except Exception as e:
        logger.error(f"Failed to parse receipt image: {e}")
        return []

# ── Report builder ────────────────────────────────────────────────────────────
def build_report(label: str, rows: List[List[str]], sheet_name: str = "Sheet1") -> str:
    """Build a formatted expense/income report.
    
    Args:
        label: Report label (e.g., "Weekly", "Monthly")
        rows: Data rows to process
        sheet_name: Name of the sheet
        
    Returns:
        Formatted report string
    """
    if not rows:
        return f"No entries in *{label}*."

    expenses_by_cat: Dict[str, float] = {}
    income_by_cat: Dict[str, float] = {}
    total_expense = 0.0
    total_income = 0.0

    for row in rows:
        try:
            amt = abs(float(row[2]))
            cat = row[1]
            etype = row[4].strip().lower() if len(row) > 4 else "expense"
            if etype == "income":
                income_by_cat[cat] = income_by_cat.get(cat, 0) + amt
                total_income += amt
            else:
                expenses_by_cat[cat] = expenses_by_cat.get(cat, 0) + amt
                total_expense += amt
        except (ValueError, IndexError) as e:
            logger.warning(f"Skipping invalid row in report: {e}")
            continue

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

@lru_cache(maxsize=10)
def _emoji(cat: str) -> str:
    """Get emoji for a category.
    
    Args:
        cat: Category name
        
    Returns:
        Emoji string
    """
    emoji_map = {
        "Food": "🍔",
        "Transport": "🚗",
        "Shopping": "🛍️",
        "Entertainment": "🎬",
        "Health": "💊",
        "Bills": "📄",
        "Transfer": "💸"
    }
    return emoji_map.get(cat, "📌")

def _allowed(update: Update) -> bool:
    """Check if user is authorized to use the bot.
    
    Args:
        update: Telegram update object
        
    Returns:
        True if authorized, False otherwise
    """
    return ALLOWED_USER_ID == 0 or update.effective_user.id == ALLOWED_USER_ID

def _uid(update: Update) -> int:
    """Get user ID from update.
    
    Args:
        update: Telegram update object
        
    Returns:
        User ID
    """
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


async def logs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update): return
    uid = _uid(update); sn = active_sheet_name(uid)
    rows = get_data_rows(sn)
    if not rows:
        await update.message.reply_text(
            f"📭 No transactions in *{sn}*.", parse_mode="Markdown")
        return

    # Optional: filter by n rows — /logs 20 shows last 20
    limit = 30  # default
    if ctx.args:
        try: limit = int(ctx.args[0])
        except ValueError: pass

    recent = rows[-limit:]  # take last N entries
    lines  = [f"📋 *Transactions — {sn}* (last {len(recent)} of {len(rows)} total)\n"]

    for r in recent:
        try:
            date_str = r[0]
            cat      = r[1]
            amt      = abs(float(r[2]))
            desc     = r[3] if len(r) > 3 else ""
            etype    = r[4].strip().lower() if len(r) > 4 else "expense"
            icon     = "🟢" if etype == "income" else "🔴"
            label    = "+" if etype == "income" else "-"
            lines.append(
                f"{icon} `{date_str}` {_emoji(cat)} *{cat}* {label}₹{amt:,.2f}"
                + (f" — _{desc}_" if desc else "")
            )
        except:
            continue

    # Telegram message limit is 4096 chars — split if needed
    full_text = "\n".join(lines)
    if len(full_text) <= 4096:
        await update.message.reply_text(full_text, parse_mode="Markdown")
    else:
        # Send in chunks
        chunk, chunks = [], []
        chunk.append(lines[0])  # header
        for line in lines[1:]:
            chunk.append(line)
            if sum(len(l) for l in chunk) > 3800:
                chunks.append("\n".join(chunk))
                chunk = []
        if chunk:
            chunks.append("\n".join(chunk))
        for part in chunks:
            await update.message.reply_text(part, parse_mode="Markdown")

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
    app.add_handler(CommandHandler("logs", logs_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
