# Telegram Expense Tracker Bot

## Overview
A Telegram bot that uses AI (Groq/Llama models) to parse natural language messages and receipt photos, automatically logging expense/income data into a Google Spreadsheet.

## Tech Stack
- **Language:** Python 3.12
- **Bot Framework:** python-telegram-bot v21.5
- **AI/LLM:** Groq API (llama-3.1-8b-instant for text, llama-3.2-11b-vision-preview for images)
- **Storage:** Google Sheets (gspread + google-auth)
- **Entry Point:** `bot.py`

## Required Environment Variables
Set these in Replit Secrets before running:
- `TELEGRAM_BOT_TOKEN` - Your Telegram bot token (from @BotFather)
- `GROQ_API_KEY` - Your Groq API key
- `GOOGLE_SHEET_ID` - Google Spreadsheet ID
- `GOOGLE_CREDENTIALS_JSON` - Google service account credentials JSON string
- `ALLOWED_TELEGRAM_USER_ID` - (optional) Restrict bot to a specific Telegram user ID
- `MONTHLY_BUDGET` - (optional) Monthly budget amount for alerts

## Features
- Log expenses/income via natural language text messages
- Scan receipts by sending photos (AI vision parsing)
- Manage multiple sheets within a spreadsheet
- Track recurring expenses (weekly/monthly/yearly)
- Monthly budget tracking with alerts at 80% and 100%
- Generate summary, weekly, and monthly reports

## Project Structure
- `bot.py` - Main bot logic (handlers, AI parsing, Sheets integration)
- `requirements.txt` - Python dependencies
- `.env.example` - Template for required environment variables

## Running
The bot runs as a background console process (`python bot.py`). It does not serve a web UI. Configure all secrets before starting.
