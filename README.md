Lightweight Python Telegram bot with per-user session management
and per-session chat history.

 • bot.py
 • requirements.txt
 • .env.example

What it supports:

 • LLM chat over Telegram
 • Separate session lists per Telegram user
 • Independent message history per session
 • Session switching
 • Session creation
 • Session renaming
 • Current session lookup

Commands:

 • /start
 • /help
 • /new [name]
 • /sessions
 • /use <session_id>
 • /rename <session_id> <new name>
 • /current

Implementation notes:

 • Uses sqlite3 for lightweight persistence
 • Stores:
    • users
    • sessions
    • messages
 • Uses an OpenAI-compatible backend via env vars:
    • TELEGRAM_BOT_TOKEN
    • OPENAI_API_KEY
    • OPENAI_MODEL
    • optional OPENAI_BASE_URL

Run steps:

 1 Install deps: pip install -r requirements.txt
 2 Copy env template and fill values: cp .env.example .env
 3 Start bot: python3 bot.py
