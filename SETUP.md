# 💸 Expense Tracker Bot — Setup Guide

Your personal expense tracker: send a WhatsApp-style message on Telegram → AI parses it → logs to Google Sheets automatically.

---

## What You'll Need (all free)
- Telegram account
- Google account
- GitHub account
- Railway account (railway.app)
- Anthropic API key (claude.ai/api)

---

## Step 1 — Create Your Telegram Bot

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. `My Expense Tracker`)
4. Choose a username (e.g. `myexpense_bot`) — must end in `bot`
5. **Copy the token** — looks like `7123456789:AAFxxxx...`

To get your personal Telegram User ID (to restrict the bot to only you):
- Message **@userinfobot** on Telegram
- It replies with your numeric ID (e.g. `987654321`)

---

## Step 2 — Set Up Google Sheets

### 2a. Create the spreadsheet
1. Go to [sheets.google.com](https://sheets.google.com)
2. Create a new sheet named **Expenses**
3. Copy the Sheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/**THIS_PART**/edit`

### 2b. Enable Google Sheets API
1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use existing)
3. Search **"Google Sheets API"** → Enable it
4. Go to **IAM & Admin → Service Accounts**
5. Click **Create Service Account**
   - Name: `expense-bot`
   - Click through all steps
6. Click the service account → **Keys tab** → **Add Key → JSON**
7. Download the JSON file — keep it safe!

### 2c. Share your sheet with the service account
1. Open your Google Sheet
2. Click **Share**
3. Enter the service account email (from the JSON: `client_email` field)
   Looks like: `expense-bot@your-project.iam.gserviceaccount.com`
4. Give it **Editor** access

### 2d. Convert credentials to one line
Open the downloaded JSON file in a text editor, select all, copy it.
You'll paste this as a single-line environment variable.

---

## Step 3 — Get Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an account / log in
3. Go to **API Keys** → **Create Key**
4. Copy it — starts with `sk-ant-...`

---

## Step 4 — Deploy to Railway

### 4a. Push code to GitHub
```bash
cd expense-tracker
git init
git add .
git commit -m "Initial commit"
# Create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/expense-tracker.git
git push -u origin main
```

### 4b. Deploy on Railway
1. Go to [railway.app](https://railway.app) → Sign up with GitHub
2. Click **New Project → Deploy from GitHub Repo**
3. Select your `expense-tracker` repo
4. Railway will auto-detect Python ✅

### 4c. Add environment variables
In Railway, go to your project → **Variables** tab → add each:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `ANTHROPIC_API_KEY` | From Anthropic Console |
| `GOOGLE_SHEET_ID` | From your Sheet URL |
| `GOOGLE_CREDENTIALS_JSON` | Paste entire JSON contents |
| `ALLOWED_TELEGRAM_USER_ID` | Your Telegram user ID (optional) |

Click **Deploy** — Railway will build and start your bot! 🎉

---

## Step 5 — Test It!

Open Telegram, find your bot, and try:

```
spent 250 on lunch today
paid 800 for groceries
uber ride 150
Netflix subscription 649
doctor visit 500
```

Then type `/summary` to see your totals!

---

## How It Works

```
You (Telegram) → Bot receives message
                      ↓
              Claude AI parses it
              (amount, category, date, description)
                      ↓
              Appended to Google Sheet
                      ↓
              Bot replies with confirmation ✅
```

---

## Google Sheet Structure

| Date | Category | Amount | Description | Added At |
|---|---|---|---|---|
| 2024-01-15 | Food | 250 | lunch | 2024-01-15 14:30 |
| 2024-01-15 | Transport | 150 | uber ride | 2024-01-15 18:00 |

---

## Troubleshooting

**Bot not responding?**
- Check Railway logs (Deployments → View Logs)
- Verify all env variables are set correctly

**Google Sheets not updating?**
- Make sure the service account email has Editor access to the sheet
- Verify `GOOGLE_SHEET_ID` is correct (just the ID, not the full URL)

**"Unauthorized" message?**
- If you set `ALLOWED_TELEGRAM_USER_ID`, make sure it matches your actual ID
- Message @userinfobot to confirm your ID

---

## Supported Categories
Food · Transport · Shopping · Entertainment · Health · Bills · Other

Claude AI automatically detects the right category from your message!
