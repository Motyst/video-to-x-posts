import hashlib
import json
import logging
import re
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from config import (
    AUTO_POST,
    DAILY_CHECK_HOUR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    UPLOADS_DIR,
    YOUTUBE_CHANNEL_URL,
)
from database import (
    add_draft,
    add_draft_pair,
    add_good_post,
    count_good_posts,
    get_all_scheduled_drafts,
    get_approved_drafts,
    get_draft_by_id,
    get_draft_partner,
    get_processed_video_ids,
    get_recent_video_summaries,
    get_scheduled_drafts,
    init_db,
    log_video_job,
    get_recent_promo_drafts,
    mark_draft_posted,
    set_draft_scheduled,
    unschedule_draft,
    set_draft_telegram_id,
    update_draft_status,
    upsert_video,
)
from twitter_poster import post_draft, twitter_configured
from youtube_monitor import fetch_single_video, get_new_videos, get_unprocessed_videos
from transcript import get_transcript, transcribe_local_file
from content_generator import generate_posts, generate_promo, generate_article, format_article_for_output, generate_video_post_captions
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
# chat_id -> draft_id awaiting custom schedule time (text fallback)
_pending_schedule_times: dict[int, int] = {}
# chat_id → {draft_id, date, hour} for step-by-step schedule builder
_sched_builder: dict[int, dict] = {}
# chat_id -> True when awaiting manual example text
_pending_examples: dict[int, bool] = {}
# chat_id -> video file path, awaiting custom caption text
_pending_video_captions: dict[int, str] = {}
# chat_id -> {path, caption_a, caption_b} — generated options waiting for pick
_video_caption_options: dict[int, dict] = {}
# chat_ids where next file received = video post (not transcription)
_video_upload_mode: set[int] = set()
# chat_id → ordered list of video file paths from last /uploads listing
_uploads_listing: dict[int, list[str]] = {}

_VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.flv', '.mp3', '.m4a', '.wav'}

# Runtime toggle — can be flipped via /autopost without restart
_auto_post_enabled: bool = AUTO_POST


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


def _pair_keyboard(id_a: int, id_b: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Original",    callback_data=f"pair_a_{id_a}"),
            InlineKeyboardButton("✅ Trend angle", callback_data=f"pair_b_{id_b}"),
        ],
        [
            InlineKeyboardButton("❌ Reject both", callback_data=f"pair_x_{id_a}"),
        ],
    ])


def _pair_version_a_keyboard(id_a: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve Version A (Original)",    callback_data=f"pair_a_{id_a}")],
        [InlineKeyboardButton("❌ Reject both",                     callback_data=f"pair_x_{id_a}")],
    ])


def _pair_version_b_keyboard(id_b: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve Version B (Trend angle)", callback_data=f"pair_b_{id_b}")],
        [InlineKeyboardButton("❌ Reject both",                     callback_data=f"pair_x_{id_b}")],
    ])


def _queue_keyboard(draft_id: int, fmt: str = None) -> InlineKeyboardMarkup:
    """Keyboard shown on each item in /queue. Promo = manual only, no auto-post buttons."""
    manual_only = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Mark as posted", callback_data=f"posted_{draft_id}"),
    ]])
    if fmt == "promo" or not twitter_configured():
        return manual_only
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Post Now",  callback_data=f"sched_now_{draft_id}"),
            InlineKeyboardButton("⏰ Schedule",   callback_data=f"queue_sched_{draft_id}"),
        ],
        [
            InlineKeyboardButton("✅ Mark as posted", callback_data=f"posted_{draft_id}"),
        ],
    ])


def _day_picker_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    today = date.today()
    rows = []
    i = 0
    while i < 14:
        d = today + timedelta(days=i)
        if i == 0:
            rows.append([InlineKeyboardButton(
                f"Today — {d.strftime('%a %d %b')}",
                callback_data=f"sbday_{draft_id}_{i}",
            )])
            i += 1
        elif i == 1:
            rows.append([InlineKeyboardButton(
                f"Tomorrow — {d.strftime('%a %d %b')}",
                callback_data=f"sbday_{draft_id}_{i}",
            )])
            i += 1
        else:
            btn1 = InlineKeyboardButton(d.strftime("%a %d %b"), callback_data=f"sbday_{draft_id}_{i}")
            if i + 1 < 14:
                d2 = today + timedelta(days=i + 1)
                btn2 = InlineKeyboardButton(d2.strftime("%a %d %b"), callback_data=f"sbday_{draft_id}_{i+1}")
                rows.append([btn1, btn2])
                i += 2
            else:
                rows.append([btn1])
                i += 1
    return InlineKeyboardMarkup(rows)


def _hour_picker_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    rows = []
    for h in range(0, 24, 4):
        rows.append([
            InlineKeyboardButton(f"{hr:02d}:__", callback_data=f"sbhr_{draft_id}_{hr}")
            for hr in range(h, min(h + 4, 24))
        ])
    return InlineKeyboardMarkup(rows)


def _minute_picker_keyboard(draft_id: int, selected_date: date, hour: int) -> InlineKeyboardMarkup:
    label = f"{selected_date.strftime('%d %b')} {hour:02d}:"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{label}00", callback_data=f"sbmin_{draft_id}_0"),
        InlineKeyboardButton(f"{label}15", callback_data=f"sbmin_{draft_id}_15"),
        InlineKeyboardButton(f"{label}30", callback_data=f"sbmin_{draft_id}_30"),
        InlineKeyboardButton(f"{label}45", callback_data=f"sbmin_{draft_id}_45"),
    ]])


def _schedule_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Now",  callback_data=f"sched_now_{draft_id}"),
            InlineKeyboardButton("+1h",     callback_data=f"sched_1h_{draft_id}"),
            InlineKeyboardButton("+2h",     callback_data=f"sched_2h_{draft_id}"),
            InlineKeyboardButton("+4h",     callback_data=f"sched_4h_{draft_id}"),
        ],
        [
            InlineKeyboardButton("+8h",          callback_data=f"sched_8h_{draft_id}"),
            InlineKeyboardButton("+24h",         callback_data=f"sched_24h_{draft_id}"),
            InlineKeyboardButton("⏰ Custom",     callback_data=f"sched_custom_{draft_id}"),
        ],
        [
            InlineKeyboardButton("📋 Keep manual", callback_data=f"sched_manual_{draft_id}"),
        ],
    ])


# ── send draft / pair ─────────────────────────────────────────────────────────

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


async def send_idea_pair(
    app: Application,
    id_a: int,
    id_b: int,
    video_title: str,
    idea: dict,
    is_retro: bool = False,
):
    """Send both versions of an idea. Tweets: one message. Threads: two messages."""
    tag = "🔄 [RETRO] " if is_retro else ""
    fmt = idea["format"]
    reason = idea.get("trend_reason", "")

    if fmt == "tweet":
        text = (
            f"{tag}From: {video_title}\n\n"
            f"━━ VERSION A: Original ━━\n{idea['original']}\n\n"
            f"━━ VERSION B: Trend angle ━━\n{idea['trend']}\n\n"
            f"💡 {reason}"
        )
        try:
            msg = await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text[:4096],
                reply_markup=_pair_keyboard(id_a, id_b),
            )
            set_draft_telegram_id(id_a, msg.message_id)
            set_draft_telegram_id(id_b, msg.message_id)
        except Exception as e:
            logger.error(f"Failed to send tweet pair ({id_a},{id_b}): {e}")
    else:
        def _render_thread(content) -> str:
            tweets = content if isinstance(content, list) else json.loads(content)
            return "\n\n".join(
                f"{i+1}/ {_strip_tweet_number(t)}" for i, t in enumerate(tweets)
            )

        text_a = (
            f"{tag}From: {video_title}\n\n"
            f"━━ VERSION A: Original ━━\n\n"
            f"{_render_thread(idea['original'])}"
        )
        text_b = (
            f"{tag}From: {video_title}\n\n"
            f"━━ VERSION B: Trend angle ━━\n\n"
            f"{_render_thread(idea['trend'])}\n\n"
            f"💡 {reason}"
        )
        try:
            msg_a = await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text_a[:4096],
                reply_markup=_pair_version_a_keyboard(id_a),
            )
            set_draft_telegram_id(id_a, msg_a.message_id)
        except Exception as e:
            logger.error(f"Failed to send thread version A ({id_a}): {e}")
        try:
            msg_b = await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text_b[:4096],
                reply_markup=_pair_version_b_keyboard(id_b),
            )
            set_draft_telegram_id(id_b, msg_b.message_id)
        except Exception as e:
            logger.error(f"Failed to send thread version B ({id_b}): {e}")


async def send_promo_pair(
    app: Application,
    id_a: int,
    id_b: int,
    video_title: str,
    promo: dict,
    is_retro: bool = False,
):
    """Send promotional content (title/hook/caption) as two version messages."""
    tag = "🔄 [RETRO] " if is_retro else ""
    reason = promo.get("trend_reason", "")

    text_a = (
        f"{tag}From: {video_title}\n\n"
        f"━━ VERSION A: Original ━━\n\n"
        f"📌 Title:\n{promo['title_a']}\n\n"
        f"🪝 Hook:\n{promo['hook_a']}\n\n"
        f"📝 Caption:\n{promo['caption_a']}"
    )
    text_b = (
        f"{tag}From: {video_title}\n\n"
        f"━━ VERSION B: Trend angle ━━\n\n"
        f"📌 Title:\n{promo['title_b']}\n\n"
        f"🪝 Hook:\n{promo['hook_b']}\n\n"
        f"📝 Caption:\n{promo['caption_b']}\n\n"
        f"💡 {reason}"
    )
    try:
        msg_a = await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text_a[:4096],
            reply_markup=_pair_version_a_keyboard(id_a),
        )
        set_draft_telegram_id(id_a, msg_a.message_id)
    except Exception as e:
        logger.error(f"Failed to send promo version A ({id_a}): {e}")
    try:
        msg_b = await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text_b[:4096],
            reply_markup=_pair_version_b_keyboard(id_b),
        )
        set_draft_telegram_id(id_b, msg_b.message_id)
    except Exception as e:
        logger.error(f"Failed to send promo version B ({id_b}): {e}")


# ── callback handler ──────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Video caption selection — no integer ID suffix
    if query.data.startswith("vcap_"):
        chat_id = query.message.chat_id
        opts = _video_caption_options.pop(chat_id, None)
        _pending_video_captions.pop(chat_id, None)
        if not opts:
            await query.edit_message_text("Session expired. Run /schedulevideo again.")
            return
        key = query.data[len("vcap_"):]  # original / problem / lesson / hook
        caption = opts.get(key, "")
        vdraft_id = add_draft(None, "video_post", json.dumps({"path": opts["path"], "caption": caption}))
        update_draft_status(vdraft_id, "approved")
        preview = caption[:120] + ("..." if len(caption) > 120 else "")
        await query.edit_message_text(
            f"✅ Caption set. When to post?\n\n{preview}",
            reply_markup=_schedule_keyboard(vdraft_id),
        )
        return

    # Step-by-step schedule builder (day → hour → minute)
    if query.data.startswith(("sbday_", "sbhr_", "sbmin_")):
        await _handle_sched_builder(query, context)
        return

    # Uploads folder: subfolder navigation
    if query.data.startswith("ufolder_"):
        subfolder_name = query.data[len("ufolder_"):]
        search_dir = Path(UPLOADS_DIR) / subfolder_name
        await _list_uploads_dir(query.message.chat_id, context, search_dir, subfolder=subfolder_name)
        return

    # Uploads folder: per-video action (upost_N / utran_N)
    if query.data.startswith("upost_") or query.data.startswith("utran_"):
        key, idx_str = query.data.split("_", 1)
        idx = int(idx_str)
        chat_id = query.message.chat_id
        listing = _uploads_listing.get(chat_id, [])
        if idx >= len(listing):
            await query.edit_message_text("Session expired. Run /uploads again.")
            return
        file_path = listing[idx]
        p = Path(file_path)
        if not p.exists():
            await query.edit_message_text(f"❌ File not found: {p.name}")
            return
        if key == "upost":
            await query.edit_message_text(f"🎬 {p.name}\n⏳ Generating captions...")
            await _generate_video_captions(context.bot, chat_id, file_path)
        else:
            await query.edit_message_text(f"📝 {p.name}\n⏳ Transcribing for tweet ideas...")
            await _process_local_file(context.application, file_path, p.stem)
        return

    action, draft_id_str = query.data.rsplit("_", 1)
    draft_id = int(draft_id_str)

    if action == "posted":
        mark_draft_posted(draft_id)
        await query.edit_message_text(
            query.message.text + "\n\n🚀 Marked as posted",
        )
        return

    if action == "unschedule":
        unschedule_draft(draft_id)
        await query.edit_message_text(
            query.message.text + "\n\n❌ Unscheduled — moved back to queue",
            reply_markup=None,
        )
        return

    if action in ("pair_a", "pair_b", "pair_x"):
        await _handle_pair_action(query, context, action, draft_id)
        return

    draft = get_draft_by_id(draft_id)

    if not draft:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if action == "approve":
        update_draft_status(draft_id, "approved")
        add_good_post(draft_id, draft["format"], draft["content"])

        if _auto_post_enabled and twitter_configured():
            # Show scheduling keyboard instead of just confirming
            await query.edit_message_text(
                query.message.text + "\n\n✅ Approved. When to post?",
                reply_markup=_schedule_keyboard(draft_id),
            )
        else:
            await query.edit_message_text(
                query.message.text + "\n\n✅ Approved — added to queue",
            )

    elif action == "reject":
        update_draft_status(draft_id, "rejected")
        await query.edit_message_text(
            query.message.text + "\n\n❌ Rejected",
        )

    elif action == "edit":
        _pending_edits[query.message.chat_id] = draft_id
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "Send your edited version.\n\n"
                "• Single tweet: just type it\n"
                "• Thread: separate tweets with --- on its own line"
            ),
            reply_to_message_id=query.message.message_id,
        )

    elif action == "queue_sched":
        # From /queue: swap keyboard to time picker
        await query.edit_message_reply_markup(reply_markup=_schedule_keyboard(draft_id))

    elif action.startswith("sched"):
        await _handle_schedule_action(query, context, action, draft_id, draft)


# ── schedule builder ─────────────────────────────────────────────────────────

async def _handle_sched_builder(query, context):
    chat_id = query.message.chat_id
    data = query.data

    if data.startswith("sbday_"):
        _, draft_id_str, offset_str = data.split("_", 2)
        draft_id = int(draft_id_str)
        offset = int(offset_str)
        selected_date = date.today() + timedelta(days=offset)
        _sched_builder[chat_id] = {"draft_id": draft_id, "date": selected_date}
        await query.edit_message_text(
            query.message.text.split("\n\n📅")[0] + f"\n\n📅 {selected_date.strftime('%A %d %b')} — pick hour (UTC):",
            reply_markup=_hour_picker_keyboard(draft_id),
        )

    elif data.startswith("sbhr_"):
        _, draft_id_str, hour_str = data.split("_", 2)
        draft_id = int(draft_id_str)
        hour = int(hour_str)
        state = _sched_builder.get(chat_id)
        if not state or state["draft_id"] != draft_id:
            await query.edit_message_text("Session expired. Tap schedule again.")
            return
        state["hour"] = hour
        selected_date = state["date"]
        await query.edit_message_text(
            query.message.text.split("\n\n📅")[0] + f"\n\n📅 {selected_date.strftime('%d %b')} {hour:02d}:__ — pick minutes (UTC):",
            reply_markup=_minute_picker_keyboard(draft_id, selected_date, hour),
        )

    elif data.startswith("sbmin_"):
        _, draft_id_str, minute_str = data.split("_", 2)
        draft_id = int(draft_id_str)
        minute = int(minute_str)
        state = _sched_builder.pop(chat_id, None)
        if not state or state["draft_id"] != draft_id or "hour" not in state:
            await query.edit_message_text("Session expired. Tap schedule again.")
            return
        d = state["date"]
        dt = datetime(d.year, d.month, d.day, state["hour"], minute, tzinfo=timezone.utc)
        if dt <= datetime.now(timezone.utc):
            await query.edit_message_text(
                query.message.text.split("\n\n📅")[0] + "\n\n⚠️ That time is in the past. Pick again:",
                reply_markup=_day_picker_keyboard(draft_id),
            )
            return
        set_draft_scheduled(draft_id, dt.strftime("%Y-%m-%d %H:%M:%S"))
        label = dt.strftime("%a %d %b at %H:%M UTC")
        await query.edit_message_text(
            query.message.text.split("\n\n📅")[0] + f"\n\n⏰ Scheduled for {label}",
        )


# ── pair handlers ────────────────────────────────────────────────────────────

async def _handle_pair_action(query, context, action: str, draft_id: int):
    draft = get_draft_by_id(draft_id)

    if not draft or draft["status"] not in ("pending",):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    partner = get_draft_partner(draft_id)

    if action == "pair_x":
        # Reject both
        update_draft_status(draft_id, "rejected")
        if partner:
            update_draft_status(partner["id"], "rejected")
        await query.edit_message_text(query.message.text + "\n\n❌ Both rejected")
        return

    # approve_a or approve_b — draft_id is the one to approve
    chosen = draft
    rejected = partner

    if chosen:
        update_draft_status(chosen["id"], "approved")
        add_good_post(chosen["id"], chosen["format"], chosen["content"])
    if rejected:
        update_draft_status(rejected["id"], "rejected")

    version_label = "Original" if action == "pair_a" else "Trend angle"

    if _auto_post_enabled and twitter_configured() and chosen["format"] != "promo":
        await query.edit_message_text(
            query.message.text + f"\n\n✅ {version_label} approved. When to post?",
            reply_markup=_schedule_keyboard(chosen["id"]),
        )
    else:
        suffix = " — copy from /queue when ready" if chosen["format"] == "promo" else " — added to queue"
        await query.edit_message_text(
            query.message.text + f"\n\n✅ {version_label} approved{suffix}",
        )


# ── scheduling helpers ────────────────────────────────────────────────────────

_HOUR_OFFSETS = {
    "sched_now": 0, "sched_1h": 1, "sched_2h": 2,
    "sched_4h": 4,  "sched_8h": 8, "sched_24h": 24,
}


async def _handle_schedule_action(query, context, action: str, draft_id: int, draft: dict):
    now = datetime.now(timezone.utc)

    if action in _HOUR_OFFSETS:
        fire_at = now + timedelta(hours=_HOUR_OFFSETS[action])
        if action == "sched_now":
            # Post immediately
            await query.edit_message_text(query.message.text + "\n\n⏳ Posting now...")
            await _fire_post(context.application, draft_id, draft)
        else:
            set_draft_scheduled(draft_id, fire_at.strftime("%Y-%m-%d %H:%M:%S"))
            label = fire_at.strftime("%H:%M UTC")
            await query.edit_message_text(
                query.message.text + f"\n\n⏰ Scheduled for {label}"
            )

    elif action == "sched_custom":
        await query.edit_message_text(
            query.message.text + "\n\n📅 Pick a day (UTC):",
            reply_markup=_day_picker_keyboard(draft_id),
        )

    elif action == "sched_manual":
        await query.edit_message_text(
            query.message.text + "\n\n📋 Kept in manual queue"
        )


async def _fire_post(app, draft_id: int, draft: dict):
    """Post to X immediately and update status."""
    try:
        url = post_draft(draft["format"], draft["content"])
        mark_draft_posted(draft_id, url)
        # Clean up video file from server after successful post
        if draft["format"] == "video_post":
            try:
                Path(json.loads(draft["content"])["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🚀 Posted!\n{url}",
        )
    except Exception as e:
        logger.error(f"Failed to post draft {draft_id}: {e}")
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ Post failed: {e}",
        )


def _parse_schedule_time(text: str) -> datetime | None:
    """Parse user time input to UTC datetime."""
    text = text.strip().lower()
    now = datetime.now(timezone.utc)

    if text == "now":
        return now

    # HH:MM — today or tomorrow if past
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if m:
        h, minute = int(m.group(1)), int(m.group(2))
        dt = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        return dt

    # tomorrow HH:MM
    m = re.match(r"^tomorrow\s+(\d{1,2}):(\d{2})$", text)
    if m:
        h, minute = int(m.group(1)), int(m.group(2))
        return (now + timedelta(days=1)).replace(hour=h, minute=minute, second=0, microsecond=0)

    # YYYY-MM-DD HH:MM
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if dt <= now:
            return None  # caller will surface "time is in the past" error
        return dt
    except ValueError:
        pass

    return None


# ── edit reply handler ────────────────────────────────────────────────────────

async def on_edit_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    # Video post caption input
    if chat_id in _pending_video_captions:
        path = _pending_video_captions.pop(chat_id)
        caption = update.message.text.strip()
        vdraft_id = add_draft(None, "video_post", json.dumps({"path": path, "caption": caption}))
        update_draft_status(vdraft_id, "approved")
        preview = caption[:120] + ("..." if len(caption) > 120 else "")
        await update.message.reply_text(
            f"✅ Caption set. When to post?\n\n{preview}",
            reply_markup=_schedule_keyboard(vdraft_id),
        )
        return

    # Manual style example
    if chat_id in _pending_examples:
        _pending_examples.pop(chat_id)
        raw = update.message.text.strip()
        parts = [p.strip() for p in raw.split("\n---\n") if p.strip()]
        if len(parts) > 1:
            fmt = "thread"
            content = json.dumps(parts)
        else:
            fmt = "tweet"
            content = raw
        add_good_post(None, fmt, content)
        total = count_good_posts()
        await update.message.reply_text(
            f"✅ Saved as style example \\({fmt}\\)\\. Total examples: {total}",
            parse_mode="MarkdownV2",
        )
        return

    # Custom schedule time
    if chat_id in _pending_schedule_times:
        draft_id = _pending_schedule_times.pop(chat_id)
        dt = _parse_schedule_time(update.message.text)
        if not dt:
            await update.message.reply_text(
                "Could not parse time. Try: 15:30 / tomorrow 09:00 / 2025-06-01 14:00"
            )
            _pending_schedule_times[chat_id] = draft_id  # put back
            return
        set_draft_scheduled(draft_id, dt.strftime("%Y-%m-%d %H:%M:%S"))
        await update.message.reply_text(f"⏰ Scheduled for {dt.strftime('%Y-%m-%d %H:%M UTC')}")
        return

    # File title confirmation
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

async def cmd_uploads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/uploads [N] [subfolder] — list videos from uploads folder to post or transcribe."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return

    args = context.args or []
    limit = None
    subfolder = None

    if args:
        try:
            limit = int(args[0])
            rest = args[1:]
        except ValueError:
            rest = args
        if rest:
            subfolder = " ".join(rest)

    base = Path(UPLOADS_DIR)
    search_dir = base / subfolder if subfolder else base
    await _list_uploads_dir(update.effective_chat.id, context, search_dir, subfolder=subfolder, limit=limit)


async def _list_uploads_dir(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    search_dir: Path,
    subfolder: str | None = None,
    limit: int | None = None,
):
    """Scan a directory and send per-video messages with action buttons."""
    if not search_dir.exists():
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Folder not found: {search_dir}",
        )
        return

    video_files = sorted(
        [f for f in search_dir.iterdir() if f.is_file() and f.suffix.lower() in _VIDEO_EXTS],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    subfolders = [] if subfolder else sorted(d for d in search_dir.iterdir() if d.is_dir())

    if not video_files and not subfolders:
        label = f"uploads/{subfolder}/" if subfolder else "uploads/"
        await context.bot.send_message(chat_id=chat_id, text=f"📁 No videos found in {label}")
        return

    batch = video_files[:limit] if limit else video_files
    _uploads_listing[chat_id] = [str(v) for v in batch]

    label = f"uploads/{subfolder}/" if subfolder else "uploads/"
    header = f"📁 {label} — {len(batch)} video(s)"
    if limit and len(video_files) > limit:
        header += f" of {len(video_files)} total"

    if subfolders:
        folder_buttons = [
            [InlineKeyboardButton(f"📂 {d.name}", callback_data=f"ufolder_{d.name[:50]}")]
            for d in subfolders[:10]
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text=header + ("\n\n📂 Subfolders — tap to browse:" if subfolders else ""),
            reply_markup=InlineKeyboardMarkup(folder_buttons),
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text=header)

    for idx, vpath in enumerate(batch):
        p = Path(vpath)
        size_mb = p.stat().st_size / (1024 * 1024)
        text = f"🎬 {p.name}\n📦 {size_mb:.1f} MB"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📹 Post video",     callback_data=f"upost_{idx}"),
            InlineKeyboardButton("📝 Extract tweets", callback_data=f"utran_{idx}"),
        ]])
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger the daily video check."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    await update.message.reply_text("🔍 Checking for new videos...")
    summary = await _run_daily_check(context.application)
    await update.message.reply_text(f"✅ {summary}")


async def cmd_retrospective(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    await update.message.reply_text("🔄 Running retrospective analysis...")

    app = context.application

    async def _send_pair(id_a, id_b, title, idea, is_retrospective=True):
        await send_idea_pair(app, id_a, id_b, title, idea, is_retro=is_retrospective)

    count = await run_retrospective(_send_pair)
    await update.message.reply_text(f"Retrospective done. {count} new idea pairs created.")


async def cmd_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scheduled — list all posts queued for auto-posting with time and preview."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return

    drafts = get_all_scheduled_drafts()
    if not drafts:
        await update.message.reply_text("No posts scheduled.")
        return

    now = datetime.now(timezone.utc)

    def _preview(d: dict) -> str:
        fmt, content = d["format"], d["content"]
        if fmt == "tweet":
            text = content
        elif fmt == "thread":
            tweets = json.loads(content)
            text = tweets[0] if tweets else content
        elif fmt == "promo":
            data = json.loads(content)
            text = data.get("hook") or data.get("title") or content
        else:
            text = content
        text = text.strip()
        return (text[:50].rstrip() + "…") if len(text) > 50 else text

    def _fire_time(d: dict) -> datetime:
        return datetime.strptime(d["scheduled_for"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

    def _time_str(fire: datetime) -> str:
        delta = fire - now
        if delta.total_seconds() < 3600:
            return f"in {int(delta.total_seconds() // 60)}m"
        if delta.days == 0:
            return fire.strftime("today %H:%M UTC")
        if delta.days == 1:
            return fire.strftime("tomorrow %H:%M UTC")
        return fire.strftime("%b %d %H:%M UTC")

    icon = lambda fmt: "📝" if fmt == "tweet" else ("🧵" if fmt == "thread" else "🎬")

    overdue  = [d for d in drafts if _fire_time(d) <= now]
    upcoming = [d for d in drafts if _fire_time(d) > now]

    lines = []

    if overdue:
        await update.message.reply_text(
            f"⚠️ {len(overdue)} overdue (not posted). Choose action for each:"
        )
        for d in overdue:
            fire = _fire_time(d)
            text = (
                f"📌 {d['title']}\n"
                f"Was due: {fire.strftime('%b %d %H:%M UTC')}\n\n"
                f"{_preview(d)}"
            )
            # Reset to approved so queue keyboard actions work correctly
            update_draft_status(d["id"], "approved")
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                reply_markup=_queue_keyboard(d["id"], fmt=d["format"]),
            )

    if upcoming:
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⏰ {len(upcoming)} upcoming scheduled:",
        )
        for d in upcoming:
            fire = _fire_time(d)
            text = (
                f"{icon(d['format'])} {_time_str(fire)}  •  {d['title']}\n\n"
                f"{_preview(d)}"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Unschedule", callback_data=f"unschedule_{d['id']}"),
            ]])
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                reply_markup=kb,
            )
    if not overdue and not upcoming:
        await update.message.reply_text("Nothing scheduled.")


async def cmd_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/autopost on|off  — toggle X auto-posting at runtime."""
    global _auto_post_enabled
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return

    if not context.args:
        state = "ON" if _auto_post_enabled else "OFF"
        configured = "✅ Keys configured" if twitter_configured() else "⚠️ No API keys in .env"
        await update.message.reply_text(
            f"Auto-post is {state}.\n{configured}\n\nUse /autopost on or /autopost off"
        )
        return

    arg = context.args[0].lower()
    if arg == "on":
        if not twitter_configured():
            await update.message.reply_text(
                "⚠️ Cannot enable — Twitter API keys missing in .env\n"
                "Add TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET"
            )
            return
        _auto_post_enabled = True
        await update.message.reply_text("✅ Auto-post ON. Approve a draft to see scheduling options.")
    elif arg == "off":
        _auto_post_enabled = False
        await update.message.reply_text("📋 Auto-post OFF. Approved posts go to manual queue.")
    else:
        await update.message.reply_text("Usage: /autopost on  or  /autopost off")


async def cmd_fetchtranscripts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch transcripts for channel videos — no Claude, no drafts.

    Usage: /fetchtranscripts [N]
    Skips videos that already have a cached transcript, so repeated calls
    automatically continue from where the previous run left off.
    """
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return

    limit = 10
    if context.args:
        try:
            limit = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /fetchtranscripts [number]  e.g. /fetchtranscripts 10")
            return

    if not YOUTUBE_CHANNEL_URL:
        await update.message.reply_text("YOUTUBE_CHANNEL_URL not set in .env")
        return

    await update.message.reply_text(f"🔍 Scanning channel for videos without transcripts...")

    from youtube_monitor import fetch_channel_videos
    from transcript import get_transcript
    from config import TRANSCRIPTS_DIR

    try:
        all_videos = fetch_channel_videos(YOUTUBE_CHANNEL_URL)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch channel: {e}")
        return

    pending = [
        v for v in all_videos
        if not (Path(TRANSCRIPTS_DIR) / f"{v['youtube_id']}.txt").exists()
    ]

    total_without = len(pending)
    batch = pending[:limit]

    if not batch:
        await update.message.reply_text(
            f"✅ All {len(all_videos)} channel videos already have transcripts."
        )
        return

    await update.message.reply_text(
        f"📥 Fetching transcripts for {len(batch)} videos "
        f"({total_without} remaining without transcript). This may take a while."
    )

    done = 0
    failed = 0
    for v in batch:
        try:
            transcript = get_transcript(v["youtube_id"], v["title"])
            if transcript:
                done += 1
                logger.info(f"Transcript fetched: {v['youtube_id']} — {v['title']}")
            else:
                failed += 1
                logger.warning(f"No transcript for {v['youtube_id']} — {v['title']}")
        except Exception as e:
            failed += 1
            logger.error(f"Transcript error for {v['youtube_id']}: {e}")

    remaining = total_without - done
    lines = [f"✅ Done: {done}  ❌ Failed: {failed}"]
    if remaining > 0:
        lines.append(f"📋 {remaining} videos still without transcript — run /fetchtranscripts {limit} to continue")
    else:
        lines.append("🎉 All channel videos now have transcripts")
    await update.message.reply_text("\n".join(lines))


async def cmd_addexample(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually add a post as a style example for future Claude calls."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    _pending_examples[update.effective_chat.id] = True
    await update.message.reply_text(
        "📝 Send the post text to save as a style example\\.\n\n"
        "Single tweet: paste as\\-is\\.\n"
        "Thread: separate tweets with `---` on its own line\\.",
        parse_mode="MarkdownV2",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    n_videos = len(get_processed_video_ids())
    n_posts = count_good_posts()
    n_queue = len(get_approved_drafts())

    lines = [
        f"📊 Stats\n",
        f"• Videos processed: {n_videos}",
        f"• Approved posts (style examples): {n_posts}",
        f"• Ready to post (queue): {n_queue}",
        "",
        "Recent videos:",
    ]
    for v in get_recent_video_summaries(limit=8):
        jobs = v["jobs_run"] or ""
        seen = []
        for j in jobs.split(","):
            j = j.strip()
            if j and j not in seen:
                seen.append(j)
        jobs_str = ", ".join(seen) if seen else "none"

        source_icon = "▶️" if v["source"] == "youtube" else ("💾" if v["source"] == "local" else "•")
        dur = v["duration_seconds"]
        if dur:
            if dur < 600:
                dur_str = f"{dur // 60}m (short)"
            elif dur < 1800:
                dur_str = f"{dur // 60}m"
            else:
                dur_str = f"{dur // 3600}h {(dur % 3600) // 60}m (long)"
        else:
            dur_str = "?"

        lines.append(f"  {source_icon} {v['title'][:38]}  {dur_str}  [{jobs_str}]")

    await update.message.reply_text("\n".join(lines))


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

    file_path, title = _resolve_file_args(context.args)

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
        limit_msg = (
            f"⚠️ File is {file_size // (1024*1024)} MB — Telegram bot API limit is 20 MB.\n"
            "SCP the file to the server and use /schedulevideo <path> or /processlocal <path> instead."
        )
        await msg.reply_text(limit_msg)
        return

    # Video post scheduling mode — route to video post flow instead of transcription
    if msg.chat_id in _video_upload_mode:
        _video_upload_mode.discard(msg.chat_id)
        fname = getattr(tg_file, "file_name", None) or "video.mp4"
        dest = Path(UPLOADS_DIR) / fname
        dest.parent.mkdir(exist_ok=True)
        tg_file_obj = await context.bot.get_file(tg_file.file_id)
        await tg_file_obj.download_to_drive(str(dest))
        await _generate_video_captions(context.bot, msg.chat_id, str(dest))
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


async def _generate_video_captions(bot, chat_id: int, file_path: str):
    """Transcribe video, generate promo captions, show as selection buttons."""
    title = Path(file_path).stem
    video_id = "local_" + hashlib.md5(Path(file_path).name.encode()).hexdigest()[:10]

    await bot.send_message(chat_id=chat_id, text=f"🎬 Transcribing: {title}\nThis may take a while...")

    transcript, _ = transcribe_local_file(file_path, video_id)
    if not transcript:
        _pending_video_captions[chat_id] = file_path
        await bot.send_message(chat_id=chat_id, text="⚠️ Transcription failed. Type caption manually:")
        return

    await bot.send_message(chat_id=chat_id, text="✍️ Generating captions...")
    caps = generate_video_post_captions(video_id, title, transcript)

    if not caps:
        _pending_video_captions[chat_id] = file_path
        await bot.send_message(chat_id=chat_id, text="⚠️ Caption generation failed. Type caption manually:")
        return

    _video_caption_options[chat_id] = {
        "path": file_path,
        "original": caps.get("original", ""),
        "problem":  caps.get("problem", ""),
        "lesson":   caps.get("lesson", ""),
        "hook":     caps.get("hook", ""),
    }
    _pending_video_captions[chat_id] = file_path  # fallback if user types custom

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ Original",    callback_data="vcap_original")],
        [InlineKeyboardButton("2️⃣ Problem",     callback_data="vcap_problem")],
        [InlineKeyboardButton("3️⃣ Key Lesson",  callback_data="vcap_lesson")],
        [InlineKeyboardButton("4️⃣ Hook",        callback_data="vcap_hook")],
    ])
    text = (
        f"✅ 4 captions for: {title}\n\n"
        f"1️⃣ ORIGINAL (short)\n{caps.get('original', '')}\n\n"
        f"2️⃣ PROBLEM (short)\n{caps.get('problem', '')}\n\n"
        f"3️⃣ KEY LESSON (longer)\n{caps.get('lesson', '')}\n\n"
        f"4️⃣ HOOK (longer)\n{caps.get('hook', '')}\n\n"
        "Pick one, or type your own:"
    )
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def cmd_schedulevideo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/schedulevideo [path] — transcribe video, pick caption, schedule to X."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    if not twitter_configured():
        await update.message.reply_text("⚠️ Twitter API keys not configured.")
        return

    if context.args:
        file_path = " ".join(context.args)
        if not Path(file_path).exists():
            await update.message.reply_text(f"❌ File not found: {file_path}")
            return
        await _generate_video_captions(context.bot, update.effective_chat.id, file_path)
    else:
        _video_upload_mode.add(update.effective_chat.id)
        await update.message.reply_text(
            "📹 Send the video file (max 20 MB via Telegram).\n"
            "For larger files use: /schedulevideo /path/to/video.mp4"
        )


async def cmd_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/article <url> — write a long-form X article in David's voice."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/article <youtube_url>`", parse_mode="MarkdownV2")
        return

    url = context.args[0]
    await update.message.reply_text("🔍 Fetching video info...")

    video = fetch_single_video(url)
    if not video:
        await update.message.reply_text("❌ Could not fetch video. Check the URL.")
        return

    await update.message.reply_text(
        f"✍️ Writing article for: {video['title']}\nThis may take a moment..."
    )
    await _process_article_video(context.application, video)


async def _process_article_video(app: Application, video: dict):
    """Generate a long-form article for a YouTube video and send as file."""
    youtube_id = video["youtube_id"]
    title = video["title"]

    transcript = get_transcript(youtube_id, title)
    transcript_path = f"transcripts/{youtube_id}.txt" if transcript else None
    video_db_id = upsert_video(
        youtube_id, title, video["url"], transcript_path,
        source="youtube",
        duration_seconds=video.get("duration_seconds"),
    )

    if not transcript:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ No transcript available for: {title}",
        )
        return

    article = generate_article(youtube_id, title, transcript)
    if not article:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ Article generation failed for: {title}",
        )
        return

    # Save to DB
    content_json = json.dumps(article)
    draft_id = add_draft(video_db_id, "article", content_json)
    log_video_job(video_db_id, "article", 1)

    # Send as file attachment
    article_text = format_article_for_output(article)
    safe_title = re.sub(r"[^\w\s-]", "", title)[:50].strip().replace(" ", "_")
    filename = f"article_{youtube_id}_{safe_title}.txt"

    file_bytes = article_text.encode("utf-8")
    word_count = len(article_text.split())

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Mark as posted", callback_data=f"posted_{draft_id}"),
        InlineKeyboardButton("❌ Discard",        callback_data=f"reject_{draft_id}"),
    ]])

    await app.bot.send_document(
        chat_id=TELEGRAM_CHAT_ID,
        document=file_bytes,
        filename=filename,
        caption=f"✍️ *{_esc(title)}*\n≈{word_count} words",
        parse_mode="MarkdownV2",
        reply_markup=kb,
    )


async def cmd_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promo <url> — generate promotional title/hook/caption for a video."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/promo <youtube_url>`", parse_mode="MarkdownV2")
        return

    url = context.args[0]
    await update.message.reply_text("🔍 Fetching video info\\.\\.\\.", parse_mode="MarkdownV2")

    video = fetch_single_video(url)
    if not video:
        await update.message.reply_text("❌ Could not fetch video\\. Check the URL\\.", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(
        f"🎬 Generating promo for: *{_esc(video['title'])}*",
        parse_mode="MarkdownV2",
    )
    await _process_promo_video(context.application, video)


async def cmd_promolocal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/promolocal <path> [title] — generate promo from a local file."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/promolocal <file_path> [optional title]`",
            parse_mode="MarkdownV2",
        )
        return

    file_path, title = _resolve_file_args(context.args)

    if not Path(file_path).exists():
        await update.message.reply_text(
            f"❌ File not found: `{_esc(file_path)}`",
            parse_mode="MarkdownV2",
        )
        return

    await update.message.reply_text(
        f"🎬 Generating promo for: *{_esc(title)}*\nTranscribing \\(may take a while\\)\\.\\.\\.",
        parse_mode="MarkdownV2",
    )
    await _process_promo_local_file(context.application, file_path, title)


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all approved posts ready to copy-paste."""
    if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
        return

    approved = get_approved_drafts()
    if not approved:
        await update.message.reply_text("Queue is empty — no approved posts yet.")
        return

    await update.message.reply_text(f"📬 {len(approved)} post(s) ready to publish:")
    for draft in approved:
        text = _format_queue_item(draft)
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            reply_markup=_queue_keyboard(draft["id"], fmt=draft["format"]),
        )


def _format_queue_item(draft: dict) -> str:
    fmt = draft["format"]
    content = draft["content"]
    title = draft.get("title", "")
    header = f"📌 {title}\n\n"

    if fmt == "tweet":
        return header + content
    elif fmt == "thread":
        tweets = json.loads(content)
        body = "\n\n".join(f"{i + 1}/ {_strip_tweet_number(t)}" for i, t in enumerate(tweets))
        return header + f"🧵 Thread:\n\n{body}"
    elif fmt == "promo":
        data = json.loads(content)
        return (
            header
            + f"🎬 Title: {data['title']}\n\n"
            + f"🪝 Hook: {data['hook']}\n\n"
            + f"📝 Caption:\n{data['caption']}"
        )
    return header + content


def _strip_tweet_number(text: str) -> str:
    """Remove leading '1/ ' or '1. ' that Claude sometimes includes in thread tweets."""
    return re.sub(r"^\d+[/.]\s*", "", text.strip())


def _resolve_file_args(args: list) -> tuple[str, str]:
    """Parse path and optional title from command args.

    Handles three cases:
      - Quoted:   "C:\\path with spaces\\file.mp4" My Title
      - Unquoted: C:\\path with spaces\\file.mp4 My Title  (prefix scan)
      - No title: either format, title falls back to filename stem

    Returns (file_path, title).
    """
    full = " ".join(args)

    # Quoted path: starts with " — find closing quote
    if full.startswith('"'):
        close = full.find('"', 1)
        if close != -1:
            file_path = full[1:close]
            title = full[close + 1:].strip() or Path(file_path).stem
            return file_path, title
        # Unclosed quote — strip leading quote and fall through
        full = full[1:]
        args = full.split()

    # Unquoted: try progressively longer prefixes until one exists on disk
    file_path = None
    title_parts = []
    for i in range(len(args), 0, -1):
        candidate = " ".join(args[:i])
        if Path(candidate).exists():
            file_path = candidate
            title_parts = args[i:]
            break

    if not file_path:
        file_path = args[0]
        title_parts = args[1:]

    title = " ".join(title_parts) if title_parts else Path(file_path).stem
    return file_path, title


# ── core processing ───────────────────────────────────────────────────────────

async def _process_video(app: Application, video: dict):
    """Transcribe one video, generate drafts, send to Telegram queue."""
    youtube_id = video["youtube_id"]
    title = video["title"]

    transcript = get_transcript(youtube_id, title)
    transcript_path = f"transcripts/{youtube_id}.txt" if transcript else None

    video_id = upsert_video(
        youtube_id, title, video["url"], transcript_path,
        source="youtube",
        duration_seconds=video.get("duration_seconds"),
    )

    if not transcript:
        logger.warning(f"No transcript for {youtube_id}")
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ No transcript for: *{_esc(title)}*",
            parse_mode="MarkdownV2",
        )
        return

    ideas = generate_posts(youtube_id, title, transcript)
    if not ideas:
        logger.warning(f"No posts generated for {youtube_id}")
        return

    for idea in ideas:
        original_str = (
            idea["original"] if isinstance(idea["original"], str)
            else json.dumps(idea["original"])
        )
        trend_str = (
            idea["trend"] if isinstance(idea["trend"], str)
            else json.dumps(idea["trend"])
        )
        id_a, id_b = add_draft_pair(
            video_id, idea["format"],
            original_str, trend_str, idea.get("trend_reason", "")
        )
        await send_idea_pair(app, id_a, id_b, title, idea)

    log_video_job(video_id, "tweets", len(ideas))


async def _process_local_file(
    app: Application,
    file_path: str,
    title: str,
    delete_after: bool = False,
):
    """Transcribe a local file with Whisper and generate drafts."""
    video_id = "local_" + hashlib.md5(Path(file_path).name.encode()).hexdigest()[:10]

    transcript, duration = transcribe_local_file(file_path, video_id)

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
    video_db_id = upsert_video(
        video_id, title, file_path, transcript_path,
        source="local",
        duration_seconds=duration,
    )

    ideas = generate_posts(video_id, title, transcript)
    if not ideas:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ No posts generated for: {title}",
        )
        return

    for idea in ideas:
        original_str = (
            idea["original"] if isinstance(idea["original"], str)
            else json.dumps(idea["original"])
        )
        trend_str = (
            idea["trend"] if isinstance(idea["trend"], str)
            else json.dumps(idea["trend"])
        )
        id_a, id_b = add_draft_pair(
            video_db_id, idea["format"],
            original_str, trend_str, idea.get("trend_reason", "")
        )
        await send_idea_pair(app, id_a, id_b, title, idea)

    log_video_job(video_db_id, "tweets", len(ideas))


async def _process_promo_video(app: Application, video: dict):
    """Generate promo content (title/hook/caption) for a YouTube video."""
    youtube_id = video["youtube_id"]
    title = video["title"]

    transcript = get_transcript(youtube_id, title)
    transcript_path = f"transcripts/{youtube_id}.txt" if transcript else None
    video_id = upsert_video(
        youtube_id, title, video["url"], transcript_path,
        source="youtube",
        duration_seconds=video.get("duration_seconds"),
    )

    if not transcript:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ No transcript for: *{_esc(title)}*",
            parse_mode="MarkdownV2",
        )
        return

    promo = generate_promo(youtube_id, title, transcript)
    if not promo:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ Promo generation failed for: {title}",
        )
        return

    original_content = json.dumps({"title": promo["title_a"], "hook": promo["hook_a"], "caption": promo["caption_a"]})
    trend_content    = json.dumps({"title": promo["title_b"], "hook": promo["hook_b"], "caption": promo["caption_b"]})
    id_a, id_b = add_draft_pair(video_id, "promo", original_content, trend_content, promo.get("trend_reason", ""))
    await send_promo_pair(app, id_a, id_b, title, promo)
    log_video_job(video_id, "promo", 1)


async def _process_promo_local_file(app: Application, file_path: str, title: str):
    """Generate promo content from a local file."""
    video_id_str = "local_" + hashlib.md5(Path(file_path).name.encode()).hexdigest()[:10]

    transcript, duration = transcribe_local_file(file_path, video_id_str)
    if not transcript:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"❌ Whisper failed to transcribe: *{_esc(title)}*",
            parse_mode="MarkdownV2",
        )
        return

    transcript_path = f"transcripts/{video_id_str}.txt"
    video_db_id = upsert_video(
        video_id_str, title, file_path, transcript_path,
        source="local",
        duration_seconds=duration,
    )

    promo = generate_promo(video_id_str, title, transcript)
    if not promo:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ Promo generation failed for: {title}",
        )
        return

    original_content = json.dumps({"title": promo["title_a"], "hook": promo["hook_a"], "caption": promo["caption_a"]})
    trend_content    = json.dumps({"title": promo["title_b"], "hook": promo["hook_b"], "caption": promo["caption_b"]})
    id_a, id_b = add_draft_pair(video_db_id, "promo", original_content, trend_content, promo.get("trend_reason", ""))
    await send_promo_pair(app, id_a, id_b, title, promo)
    log_video_job(video_db_id, "promo", 1)


# ── scheduled jobs ────────────────────────────────────────────────────────────

async def _run_daily_check(app: Application) -> str:
    """Returns a summary string for the caller to report back."""
    logger.info("Daily check started")
    try:
        new_videos = get_new_videos(YOUTUBE_CHANNEL_URL)
        if not new_videos:
            logger.info("No new videos found")
            return "No new videos found."
        for video in new_videos:
            await _process_video(app, video)
        titles = "\n".join(f"• {v['title']}" for v in new_videos)
        return f"Found {len(new_videos)} new video(s):\n{titles}"
    except Exception as e:
        logger.error(f"Daily check failed: {e}", exc_info=True)
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ Daily check failed: {str(e)}",
        )
        return f"Check failed: {e}"


async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    await _run_daily_check(context.application)


async def scheduled_post_job(context: ContextTypes.DEFAULT_TYPE):
    """Every 60s: check for scheduled posts due to fire and post them."""
    due = get_scheduled_drafts()
    for draft in due:
        await _fire_post(context.application, draft["id"], draft)


# ── main ──────────────────────────────────────────────────────────────────────

async def _post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("uploads",      "Browse uploads folder — post video or extract tweets"),
        BotCommand("check",        "Trigger daily YouTube channel check"),
        BotCommand("process",      "Extract tweet ideas from a YouTube URL"),
        BotCommand("processall",   "Process all unprocessed channel videos"),
        BotCommand("processlocal", "Extract tweet ideas from a local file"),
        BotCommand("schedulevideo", "Upload a video to X — pick caption and schedule"),
        BotCommand("article",      "Write a long-form X article from a YouTube video"),
        BotCommand("promo",        "Generate video title, hook & caption from a URL"),
        BotCommand("promolocal",   "Generate promo content from a local file"),
        BotCommand("queue",        "Show approved posts ready to publish"),
        BotCommand("scheduled",    "List scheduled posts with time and preview"),
        BotCommand("retrospective","Re-analyse archived transcripts with new examples"),
        BotCommand("autopost",     "Toggle X auto-posting on/off"),
        BotCommand("fetchtranscripts", "Download transcripts only — no Claude, no drafts"),
        BotCommand("addexample",   "Add a post as a style example for Claude"),
        BotCommand("status",       "Stats + recent video job history"),
    ])


def main():
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("uploads",        cmd_uploads))
    app.add_handler(CommandHandler("check",         cmd_check))
    app.add_handler(CommandHandler("process",       cmd_process))
    app.add_handler(CommandHandler("processall",    cmd_processall))
    app.add_handler(CommandHandler("processlocal",  cmd_processlocal))
    app.add_handler(CommandHandler("schedulevideo", cmd_schedulevideo))
    app.add_handler(CommandHandler("article",       cmd_article))
    app.add_handler(CommandHandler("promo",         cmd_promo))
    app.add_handler(CommandHandler("promolocal",    cmd_promolocal))
    app.add_handler(CommandHandler("queue",         cmd_queue))
    app.add_handler(CommandHandler("scheduled",     cmd_scheduled))
    app.add_handler(CommandHandler("autopost",      cmd_autopost))
    app.add_handler(CommandHandler("fetchtranscripts", cmd_fetchtranscripts))
    app.add_handler(CommandHandler("addexample",    cmd_addexample))
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
    app.job_queue.run_repeating(scheduled_post_job, interval=60, first=10)

    logger.info(f"Bot started. Daily check at {DAILY_CHECK_HOUR:02d}:00 UTC. Auto-post: {_auto_post_enabled}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
