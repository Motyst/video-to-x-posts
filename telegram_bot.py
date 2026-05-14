import hashlib
import json
import logging
import re
import tempfile
from datetime import time, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    DAILY_CHECK_HOUR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    YOUTUBE_CHANNEL_URL,
)
from database import (
    add_draft,
    add_good_post,
    count_good_posts,
    get_approved_drafts,
    get_draft_by_id,
    get_processed_video_ids,
    init_db,
    mark_draft_posted,
    set_draft_telegram_id,
    update_draft_status,
    upsert_video,
)
from youtube_monitor import fetch_single_video, get_new_videos, get_unprocessed_videos
from transcript import get_transcript, transcribe_local_file
from content_generator import generate_posts
from retrospective import run_retrospective

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# chat_id -> draft_id awaiting edited text
_pending_edits: dict[int, int] = {}
# chat_id -> (temp_file_path, suggested_title) awaiting title confirmation
_pending_file_titles: dict[int, tuple[str, str]] = {}


# ── formatting ────────────────────────────────────────────────────────────────

def _format_message(title: str, fmt: str, content, is_retro: bool = False) -> str:
    tag = "🔄 *[RETROSPECTIVE]*" if is_retro else "🆕"
    header = f"{tag} From: *{_esc(title)}*\n\n"

    if fmt == "tweet":
        return header + f"📝 *Tweet:*\n{_esc(content)}"
    else:
        tweets = content if isinstance(content, list) else json.loads(content)
        body = "\n\n".join(f"{i + 1}\\. {_esc(t)}" for i, t in enumerate(tweets))
        return header + f"🧵 *Thread \\({len(tweets)} tweets\\):*\n\n{body}"


def _esc(text: str) -> str:
    """Escape MarkdownV2 special chars."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)


def _keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{draft_id}"),
        InlineKeyboardButton("✏️ Edit",    callback_data=f"edit_{draft_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{draft_id}"),
    ]])


# ── send draft ────────────────────────────────────────────────────────────────

async def send_draft(
    app: Application,
    draft_id: int,
    video_title: str,
    fmt: str,
    content,
    is_retro: bool = False,
):
    text = _format_message(video_title, fmt, content, is_retro)
    try:
        msg = await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=_keyboard(draft_id),
        )
        set_draft_telegram_id(draft_id, msg.message_id)
    except Exception as e:
        logger.error(f"Failed to send draft {draft_id}: {e}")


# ── callback handler ──────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, draft_id_str = query.data.rsplit("_", 1)
    draft_id = int(draft_id_str)

    if action == "posted":
        mark_draft_posted(draft_id)
        await query.edit_message_text(
            query.message.text_markdown_v2 + _esc("\n\n🚀 Marked as posted"),
            parse_mode="MarkdownV2",
        )
        return

    draft = get_draft_by_id(draft_id)

    if not draft:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if action == "approve":
        update_draft_status(draft_id, "approved")
        add_good_post(draft_id, draft["format"], draft["content"])
        suffix = "\n\n✅ *Approved — added to style examples*"
        await query.edit_message_text(
            query.message.text_markdown_v2 + _esc(suffix),
            parse_mode="MarkdownV2",
        )

    elif action == "reject":
        update_draft_status(draft_id, "rejected")
        await query.edit_message_text(
            query.message.text_markdown_v2 + _esc("\n\n❌ Rejected"),
            parse_mode="MarkdownV2",
        )

    elif action == "edit":
        _pending_edits[query.message.chat_id] = draft_id
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "Send your edited version\\.\n\n"
                "• Single tweet → just type it\n"
                "• Thread → separate tweets with `---` on its own line"
            ),
            parse_mode="MarkdownV2",
            reply_to_message_id=query.message.message_id,
        )


# ── edit reply handler ────────────────────────────────────────────────────────

async def on_edit_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    # File title confirmation takes priority
    if chat_id in _pending_file_titles:
        tmp_path, suggested = _pending_file_titles.pop(chat_id)
        raw = update.message.text.strip()
        title = suggested if raw.lower() == "ok" else raw
        await update.message.reply_text(
            f"🎬 Processing: *{_esc(title)}*\nTranscribing \\(may take a while\\)\\.\\.\\.",
            parse_mode="MarkdownV2",
        )
        await _process_local_file(context.application, tmp_path, title, delete_after=True)
        return

    if chat_id not in _pending_edits:
        return

    draft_id = _pending_edits.pop(chat_id)
    draft = get_draft_by_id(draft_id)
    if not draft:
        return

    raw = update.message.text.strip()
    parts = [p.strip() for p in raw.split("\n---\n") if p.strip()]

    if len(parts) > 1:
        new_fmt = "thread"
        new_content = json.dumps(parts)
    else:
        new_fmt = "tweet"
        new_content = raw

    update_draft_status(draft_id, "approved", new_content)
    add_good_post(draft_id, new_fmt, new_content)
    await update.message.reply_text("✅ Saved edited version and added to style examples\\.", parse_mode="MarkdownV2")


# ── commands ──────────────────────────────────────────────────────────────────

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger the daily video check."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    await update.message.reply_text("🔍 Checking for new videos\\.\\.\\.", parse_mode="MarkdownV2")
    await _run_daily_check(context.application)


async def cmd_retrospective(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    await update.message.reply_text("🔄 Running retrospective analysis\\.\\.\\.", parse_mode="MarkdownV2")

    app = context.application

    async def _send(draft_id, title, fmt, content, is_retrospective=True):
        await send_draft(app, draft_id, title, fmt, content, is_retrospective)

    count = await run_retrospective(_send)
    await update.message.reply_text(
        f"Retrospective done\\. *{count}* new drafts created\\.",
        parse_mode="MarkdownV2",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    n_videos = len(get_processed_video_ids())
    n_posts = count_good_posts()
    n_queue = len(get_approved_drafts())
    await update.message.reply_text(
        f"📊 *Stats*\n"
        f"• Videos processed: *{n_videos}*\n"
        f"• Approved posts \\(style examples\\): *{n_posts}*\n"
        f"• Ready to post \\(queue\\): *{n_queue}*",
        parse_mode="MarkdownV2",
    )


async def cmd_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process a specific video URL: /process <url>"""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/process <youtube_url>`", parse_mode="MarkdownV2")
        return

    url = context.args[0]
    await update.message.reply_text(f"🔍 Fetching video info\\.\\.\\.", parse_mode="MarkdownV2")

    video = fetch_single_video(url)
    if not video:
        await update.message.reply_text("❌ Could not fetch video\\. Check the URL\\.", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(
        f"📹 Processing: *{_esc(video['title'])}*",
        parse_mode="MarkdownV2",
    )
    await _process_video(context.application, video)


async def cmd_processall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process all unprocessed videos on the configured channel."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    await update.message.reply_text("📋 Scanning channel for unprocessed videos\\.\\.\\.", parse_mode="MarkdownV2")

    videos = get_unprocessed_videos(YOUTUBE_CHANNEL_URL)
    if not videos:
        await update.message.reply_text("✅ All channel videos already processed\\.", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(
        f"Found *{len(videos)}* unprocessed videos\\. Starting\\.\\.\\.",
        parse_mode="MarkdownV2",
    )
    for video in videos:
        await _process_video(context.application, video)


async def cmd_processlocal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/processlocal <path> [title] — process a file already on the server."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/processlocal <file_path> [optional title]`\n"
            "Example: `/processlocal /videos/episode42\\.mp4 My Video Title`",
            parse_mode="MarkdownV2",
        )
        return

    file_path = context.args[0]
    title = " ".join(context.args[1:]) if len(context.args) > 1 else Path(file_path).stem

    if not Path(file_path).exists():
        await update.message.reply_text(
            f"❌ File not found: `{_esc(file_path)}`",
            parse_mode="MarkdownV2",
        )
        return

    await update.message.reply_text(
        f"🎬 Processing local file: *{_esc(title)}*\nTranscribing with Whisper \\(may take a while\\)\\.\\.\\.",
        parse_mode="MarkdownV2",
    )
    await _process_local_file(context.application, file_path, title)


async def on_file_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle video/audio files sent directly to the bot."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return

    msg = update.message
    tg_file = (
        msg.video
        or msg.audio
        or msg.voice
        or (msg.document if msg.document and _is_media_doc(msg.document) else None)
    )
    if not tg_file:
        return

    file_size = getattr(tg_file, "file_size", 0) or 0
    if file_size > 20 * 1024 * 1024:
        await msg.reply_text(
            f"⚠️ File is *{file_size // (1024*1024)} MB* — Telegram bot API limit is 20 MB\\.\n"
            "Copy the file to the server and use `/processlocal <path>` instead\\.",
            parse_mode="MarkdownV2",
        )
        return

    # Suggest title from caption or filename
    suggested = (
        msg.caption
        or getattr(tg_file, "file_name", None)
        or "Untitled"
    )
    suggested = Path(suggested).stem  # strip extension if filename

    await msg.reply_text(
        f"📥 Downloading\\.\\.\\. Reply with a title for this video, or send `ok` to use: *{_esc(suggested)}*",
        parse_mode="MarkdownV2",
    )

    tg_file_obj = await context.bot.get_file(tg_file.file_id)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    await tg_file_obj.download_to_drive(tmp.name)
    tmp.close()

    _pending_file_titles[msg.chat_id] = (tmp.name, suggested)


def _is_media_doc(doc) -> bool:
    mime = doc.mime_type or ""
    return mime.startswith("video/") or mime.startswith("audio/")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all approved posts ready to copy-paste."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return

    approved = get_approved_drafts()
    if not approved:
        await update.message.reply_text("Queue is empty — no approved posts yet\\.", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(
        f"📬 *{len(approved)} post(s) ready to publish:*",
        parse_mode="MarkdownV2",
    )
    for draft in approved:
        text = _format_queue_item(draft)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Mark as posted", callback_data=f"posted_{draft['id']}"),
        ]])
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
        )


def _format_queue_item(draft: dict) -> str:
    fmt = draft["format"]
    content = draft["content"]
    title = draft.get("title", "")
    header = f"📌 *{_esc(title)}*\n\n"

    if fmt == "tweet":
        return header + _esc(content)
    else:
        tweets = json.loads(content)
        body = "\n\n".join(f"{i + 1}\\. {_esc(t)}" for i, t in enumerate(tweets))
        return header + f"🧵 *Thread:*\n\n{body}"


# ── core processing ───────────────────────────────────────────────────────────

async def _process_video(app: Application, video: dict):
    """Transcribe one video, generate drafts, send to Telegram queue."""
    youtube_id = video["youtube_id"]
    title = video["title"]

    transcript = get_transcript(youtube_id, title)
    transcript_path = f"transcripts/{youtube_id}.txt" if transcript else None

    video_id = upsert_video(youtube_id, title, video["url"], transcript_path)

    if not transcript:
        logger.warning(f"No transcript for {youtube_id}")
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ No transcript for: *{_esc(title)}*",
            parse_mode="MarkdownV2",
        )
        return

    posts = generate_posts(youtube_id, title, transcript)
    if not posts:
        logger.warning(f"No posts generated for {youtube_id}")
        return

    for post in posts:
        content_str = (
            post["content"]
            if isinstance(post["content"], str)
            else json.dumps(post["content"])
        )
        draft_id = add_draft(video_id, post["format"], content_str)
        await send_draft(app, draft_id, title, post["format"], post["content"])


async def _process_local_file(
    app: Application,
    file_path: str,
    title: str,
    delete_after: bool = False,
):
    """Transcribe a local file with Whisper and generate drafts."""
    video_id = "local_" + hashlib.md5(Path(file_path).name.encode()).hexdigest()[:10]

    transcript = transcribe_local_file(file_path, video_id)

    if delete_after:
        Path(file_path).unlink(missing_ok=True)

    if not transcript:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"❌ Whisper failed to transcribe: *{_esc(title)}*",
            parse_mode="MarkdownV2",
        )
        return

    transcript_path = f"transcripts/{video_id}.txt"
    video_db_id = upsert_video(video_id, title, file_path, transcript_path)

    posts = generate_posts(video_id, title, transcript)
    if not posts:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ No posts generated for: *{_esc(title)}*",
            parse_mode="MarkdownV2",
        )
        return

    for post in posts:
        content_str = (
            post["content"]
            if isinstance(post["content"], str)
            else json.dumps(post["content"])
        )
        draft_id = add_draft(video_db_id, post["format"], content_str)
        await send_draft(app, draft_id, title, post["format"], post["content"])


# ── scheduled jobs ────────────────────────────────────────────────────────────

async def _run_daily_check(app: Application):
    logger.info("Daily check started")
    try:
        new_videos = get_new_videos(YOUTUBE_CHANNEL_URL)
        if not new_videos:
            logger.info("No new videos found")
            return
        for video in new_videos:
            await _process_video(app, video)
    except Exception as e:
        logger.error(f"Daily check failed: {e}", exc_info=True)
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ Daily check failed: {_esc(str(e))}",
            parse_mode="MarkdownV2",
        )


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    await _run_daily_check(context.application)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("check",         cmd_check))
    app.add_handler(CommandHandler("process",       cmd_process))
    app.add_handler(CommandHandler("processall",    cmd_processall))
    app.add_handler(CommandHandler("processlocal",  cmd_processlocal))
    app.add_handler(CommandHandler("queue",         cmd_queue))
    app.add_handler(CommandHandler("retrospective", cmd_retrospective))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL,
        on_file_received,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_edit_reply))

    app.job_queue.run_daily(
        daily_job,
        time=time(DAILY_CHECK_HOUR, 0, tzinfo=timezone.utc),
    )

    logger.info(f"Bot started. Daily check at {DAILY_CHECK_HOUR:02d}:00 UTC.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
