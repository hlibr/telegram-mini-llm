import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot.db"))
SYSTEM_PROMPT = os.getenv(
    "BOT_SYSTEM_PROMPT",
    "You are a helpful assistant inside a Telegram bot.",
)
DEFAULT_SESSION_NAME = "Session"
MAX_TITLE_LENGTH = 64
MAX_LISTED_SESSIONS = 20


@dataclass
class Session:
    id: int
    user_id: int
    name: str
    created_at: str
    updated_at: str


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    active_session_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(active_session_id) REFERENCES sessions(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id)"
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def ensure_user(self, user_id: int) -> None:
        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO users(user_id, active_session_id, created_at, updated_at)
                VALUES(?, NULL, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (user_id, now, now),
            )

    def create_session(self, user_id: int, name: str | None = None) -> Session:
        self.ensure_user(user_id)
        now = self._now()
        label = (name or DEFAULT_SESSION_NAME).strip()[:MAX_TITLE_LENGTH] or DEFAULT_SESSION_NAME

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO sessions(user_id, name, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                """,
                (user_id, label, now, now),
            )
            session_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE users SET active_session_id=?, updated_at=? WHERE user_id=?",
                (session_id, now, user_id),
            )
            conn.execute(
                """
                INSERT INTO messages(session_id, role, content, created_at)
                VALUES(?, 'system', ?, ?)
                """,
                (session_id, SYSTEM_PROMPT, now),
            )

        return self.get_session(user_id, session_id)

    def get_session(self, user_id: int, session_id: int) -> Session:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=? AND user_id=?",
                (session_id, user_id),
            ).fetchone()
        if row is None:
            raise ValueError("Session not found")
        return Session(**dict(row))

    def list_sessions(self, user_id: int, limit: int = MAX_LISTED_SESSIONS) -> list[Session]:
        self.ensure_user(user_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE user_id=?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [Session(**dict(row)) for row in rows]

    def get_active_session(self, user_id: int) -> Session:
        self.ensure_user(user_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT s.*
                FROM users u
                LEFT JOIN sessions s ON s.id = u.active_session_id AND s.user_id = u.user_id
                WHERE u.user_id=?
                """,
                (user_id,),
            ).fetchone()

        if row is None or row["id"] is None:
            return self.create_session(user_id)
        return Session(**dict(row))

    def set_active_session(self, user_id: int, session_id: int) -> Session:
        session = self.get_session(user_id, session_id)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE users SET active_session_id=?, updated_at=? WHERE user_id=?",
                (session.id, self._now(), user_id),
            )
        return session

    def rename_session(self, user_id: int, session_id: int, name: str) -> Session:
        clean_name = name.strip()[:MAX_TITLE_LENGTH]
        if not clean_name:
            raise ValueError("Session name cannot be empty")
        self.get_session(user_id, session_id)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE sessions SET name=?, updated_at=? WHERE id=? AND user_id=?",
                (clean_name, self._now(), session_id, user_id),
            )
        return self.get_session(user_id, session_id)

    def add_message(self, session_id: int, role: str, content: str) -> None:
        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO messages(session_id, role, content, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (session_id, role, content, now),
            )
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )

    def get_messages(self, session_id: int) -> list[dict[str, str]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT role, content
                FROM messages
                WHERE session_id=?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]


class LLMClient:
    def __init__(self) -> None:
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        if not model:
            raise RuntimeError("OPENAI_MODEL is not set")
        self.model = model
        base_url = os.getenv("OPENAI_BASE_URL") or None
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def reply(self, messages: Iterable[dict[str, str]]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=list(messages),
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("LLM returned an empty response")
        return content.strip()


storage = Storage(DB_PATH)
_llm_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


def _user_id(update: Update) -> int:
    if update.effective_user is None:
        raise RuntimeError("Could not determine user")
    return int(update.effective_user.id)


def _session_lines(active_session_id: int, sessions: list[Session]) -> str:
    if not sessions:
        return "No sessions yet. Use /new to create one."
    lines = []
    for session in sessions:
        marker = "*" if session.id == active_session_id else " "
        lines.append(f"{marker} {session.id} — {session.name}")
    return "\n".join(lines)


async def _send_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _user_id(update)
    session = storage.get_active_session(user_id)
    text = (
        "Hi! I’m your LLM chat bot.\n\n"
        f"Current session: {session.id} — {session.name}\n\n"
        "Commands:\n"
        "- /new [name] — create and switch to a new session\n"
        "- /sessions — list your sessions\n"
        "- /use <id> — switch to one of your sessions\n"
        "- /rename <id> <name> — rename a session\n"
        "- /current — show current session\n"
        "- /help — show help\n\n"
        "Just send a message to chat with the model in the active session."
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def current(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _user_id(update)
    session = storage.get_active_session(user_id)
    await update.message.reply_text(
        f"Current session: {session.id} — {session.name}"
    )


async def new_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _user_id(update)
    name = " ".join(context.args).strip() or None
    session = storage.create_session(user_id, name)
    await update.message.reply_text(
        f"Created and switched to session {session.id} — {session.name}"
    )


async def list_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _user_id(update)
    active = storage.get_active_session(user_id)
    sessions = storage.list_sessions(user_id)
    await update.message.reply_text("Your sessions:\n" + _session_lines(active.id, sessions))


async def use_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _user_id(update)
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /use <session_id>")
        return

    session_id = int(context.args[0])
    try:
        session = storage.set_active_session(user_id, session_id)
    except ValueError:
        await update.message.reply_text("That session does not exist in your account.")
        return

    await update.message.reply_text(
        f"Switched to session {session.id} — {session.name}"
    )


async def rename_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _user_id(update)
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /rename <session_id> <new name>")
        return

    session_id = int(context.args[0])
    name = " ".join(context.args[1:]).strip()
    try:
        session = storage.rename_session(user_id, session_id, name)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    await update.message.reply_text(
        f"Renamed session {session.id} to {session.name}"
    )


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text:
        return

    user_id = _user_id(update)
    session = storage.get_active_session(user_id)
    user_text = update.message.text.strip()
    if not user_text:
        return

    storage.add_message(session.id, "user", user_text)
    await _send_typing(context, update.effective_chat.id)

    try:
        reply_text = await asyncio.to_thread(get_llm().reply, storage.get_messages(session.id))
    except Exception as exc:
        logger.exception("Failed to get LLM response")
        await update.message.reply_text(f"LLM request failed: {exc}")
        return

    storage.add_message(session.id, "assistant", reply_text)
    await update.message.reply_text(reply_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("current", current))
    application.add_handler(CommandHandler("new", new_session))
    application.add_handler(CommandHandler("sessions", list_sessions))
    application.add_handler(CommandHandler("use", use_session))
    application.add_handler(CommandHandler("rename", rename_session))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    application.add_error_handler(error_handler)

    logger.info("Bot is starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
