# David Post Bot

Telegram bot that monitors a YouTube channel, transcribes videos, and generates X/Twitter posts in the channel owner's (David's) communication style. Posts are reviewed and approved via Telegram before publishing.

## Architecture

```
telegram_bot.py  ← main entry point, all user interaction
    ├── youtube_monitor.py   ← discover new videos via yt-dlp
    ├── transcript.py        ← captions → Whisper fallback, file caching
    ├── content_generator.py ← Claude API, few-shot from approved posts
    ├── retrospective.py     ← re-mine old transcripts with newer examples
    └── database.py          ← SQLite: videos / drafts / good_posts
config.py        ← all env vars, loaded once at import
```

## Database Schema

Three tables in `david_bot.db`:

```sql
videos      — youtube_id (UNIQUE), title, url, transcript_path, processed_at, retrospective_reviewed_at
drafts      — video_id, format (tweet|thread), content (TEXT, JSON array for threads), status (pending|approved|rejected|posted), telegram_message_id
good_posts  — draft_id, format, content, approved_at  ← fed back as few-shot examples
```

`content` in `drafts`/`good_posts`:
- `format=tweet` → plain string
- `format=thread` → JSON array of strings, one per tweet

## Key Design Decisions

**Feedback loop** — every approved post is saved to `good_posts` and loaded as few-shot style examples in the next Claude call. Quality improves as more posts are approved.

**Retrospective** — `retrospective.py` re-mines old transcripts monthly using the current (larger) set of approved examples. Won't run until 5+ good posts exist. Triggered via `/retrospective` command or could be scheduled.

**Transcript strategy** — `get_transcript()` tries YouTube auto-captions first (fast, free), falls back to Whisper (slower, better quality). Local files skip straight to Whisper. All transcripts cached as `.txt` files in `TRANSCRIPTS_DIR`.

**Model switching** — `CLAUDE_MODEL` in `.env`. No code changes needed to switch model.

**Whisper quality** — `WHISPER_MODEL=base` by default (fast). Change to `medium` or `large-v3` in `.env` for better accuracy on non-native speakers or technical content.

## Telegram Commands

| Command | Action |
|---|---|
| `/check` | Trigger daily YouTube channel check manually |
| `/process <url>` | Process any YouTube URL immediately |
| `/processall` | Process entire unprocessed channel backlog |
| `/processlocal <path> [title]` | Transcribe a file on the server with Whisper |
| `/queue` | Show all approved posts ready to copy-paste |
| `/retrospective` | Re-analyse all archived transcripts |
| `/status` | Stats: videos processed, approved posts, queue size |

**Sending a file to the bot** — bot downloads it (≤20 MB Telegram limit), asks for title, Whisper transcribes, generates drafts.

**Inline buttons on each draft** — ✅ Approve / ✏️ Edit / ❌ Reject.

**Edit flow** — tap ✏️, bot prompts for new text. Single tweet = plain text. Thread = tweets separated by `---` on its own line.

**Mark as posted** — visible in `/queue`. Moves draft status to `posted`.

## State Dicts (in-memory, telegram_bot.py)

```python
_pending_edits: dict[int, int]               # chat_id → draft_id
_pending_file_titles: dict[int, tuple]       # chat_id → (tmp_path, suggested_title)
```

These are lost on bot restart — in-flight edits/uploads will silently drop. Acceptable given low volume.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in all values
python telegram_bot.py
```

Daily check fires at `DAILY_CHECK_HOUR` UTC (default 9). Bot must stay running — host on a VPS or keep local machine on.

## Adding X/Twitter Auto-Post (future)

Skeleton is ready — `mark_draft_posted()` in `database.py` already exists. Steps when X API is available:
1. Add `TWITTER_*` keys to `config.py` / `.env`
2. On `/queue` approval, call X API to post
3. Call `mark_draft_posted(draft_id)` after successful post
4. Optionally add per-post time scheduling before posting

## Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | From @BotFather |
| `TELEGRAM_CHAT_ID` | — | Your personal chat ID with the bot |
| `ANTHROPIC_API_KEY` | — | |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Change here to switch models |
| `YOUTUBE_CHANNEL_URL` | — | Channel or playlist URL |
| `DB_PATH` | `david_bot.db` | |
| `TRANSCRIPTS_DIR` | `transcripts` | |
| `DAILY_CHECK_HOUR` | `9` | UTC hour |
| `WHISPER_DEVICE` | `cpu` | `cuda` if GPU available |
| `WHISPER_COMPUTE` | `int8` | `float16` for GPU |
| `WHISPER_MODEL` | `base` | `medium` or `large-v3` for better quality |
