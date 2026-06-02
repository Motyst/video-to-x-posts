import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

YOUTUBE_CHANNEL_URL = os.getenv("YOUTUBE_CHANNEL_URL")
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "")

DB_PATH = os.getenv("DB_PATH", "david_bot.db")
TRANSCRIPTS_DIR = os.getenv("TRANSCRIPTS_DIR", "transcripts")
UPLOADS_DIR = os.getenv("UPLOADS_DIR", "uploads")

DAILY_CHECK_HOUR = int(os.getenv("DAILY_CHECK_HOUR", "9"))

WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

# Article generation
ARTICLE_TARGET_WORDS = os.getenv("ARTICLE_TARGET_WORDS", "")   # empty = Claude decides
ARTICLE_OUTPUT_FORMAT = os.getenv("ARTICLE_OUTPUT_FORMAT", "x_native")  # swap to change platform

# X / Twitter
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY", "")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET", "")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN", "")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET", "")
AUTO_POST = os.getenv("AUTO_POST", "false").lower() == "true"
