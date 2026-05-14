import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

YOUTUBE_CHANNEL_URL = os.getenv("YOUTUBE_CHANNEL_URL")

DB_PATH = os.getenv("DB_PATH", "david_bot.db")
TRANSCRIPTS_DIR = os.getenv("TRANSCRIPTS_DIR", "transcripts")

DAILY_CHECK_HOUR = int(os.getenv("DAILY_CHECK_HOUR", "9"))

WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
