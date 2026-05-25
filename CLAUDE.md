# David Post Bot

Telegram bot that monitors a YouTube channel, transcribes videos, and generates X/Twitter posts in the channel owner's (David's) communication style. Posts are reviewed and approved via Telegram before publishing.

## Architecture

```
telegram_bot.py  ← main entry point, all user interaction
    ├── youtube_monitor.py   ← discover new videos via yt-dlp
    ├── transcript.py        ← captions → Whisper fallback, file caching
    ├── content_generator.py ← Claude API, few-shot from approved posts
    ├── retrospective.py     ← re-mine old transcripts with newer examples
    └── database.py          ← SQLite: videos / drafts / good_posts / video_jobs
config.py        ← all env vars, loaded once at import
twitter_poster.py ← X/Twitter API posting via tweepy
```

## Database Schema

Four tables in `david_bot.db`:

```sql
videos      — youtube_id (UNIQUE), title, url, transcript_path, processed_at,
              retrospective_reviewed_at, source ('youtube'|'local'), duration_seconds

drafts      — video_id, format (tweet|thread|promo), content (TEXT), status
              (pending|approved|rejected|posted|scheduled), telegram_message_id,
              version ('original'|'trend'), pair_id (links the two versions),
              trend_reason, scheduled_for, posted_url

good_posts  — draft_id, format, content, approved_at  ← fed back as few-shot examples

video_jobs  — video_id, job_type ('tweets'|'promo'|'retrospective'),
              ran_at, draft_count
```

`content` field format by type:
- `format=tweet` → plain string
- `format=thread` → JSON array of strings, one per tweet
- `format=promo` → JSON object `{"title": "...", "hook": "...", "caption": "..."}`

## Key Design Decisions

**Feedback loop** — every approved post saved to `good_posts`, loaded as few-shot style examples in next Claude call. Quality improves as more posts approved.

**Two-version system** — every generated idea comes as two versions:
- Version A (Original): matches David's exact vocabulary, tone, rhythm from transcript
- Version B (Trend angle): same idea reframed for current X performance patterns
- Telegram shows both; approving one auto-rejects the other via `pair_id` link
- Tweets → single combined message with `[✅ Original] [✅ Trend] [❌ Reject both]`
- Threads → two separate messages (full content visible), each with own approve button

**Video promo content** — separate from tweet extraction. Generates title + hook + caption to post alongside a video (not ideas from it). Same transcript, different Claude prompt. Result stored as `format=promo`. Manual copy-paste workflow — bot does not upload video files to X.

**Job tracking** — `video_jobs` table logs every content generation run per video (type, count, timestamp). `/status` shows recent videos with source icon, duration, and which jobs ran.

**Transcript caching** — `get_transcript()` checks `TRANSCRIPTS_DIR` first. Running `/process` then `/promo` on the same video fetches transcript only once.

**Transcript strategy** — tries YouTube auto-captions first (fast, free), falls back to Whisper (slower, better quality). Local files go straight to Whisper. All cached as `.txt` files.

**Retrospective** — `retrospective.py` re-mines old transcripts monthly using current approved examples. Won't run until 5+ good posts exist.

**X/Twitter auto-post** — `twitter_poster.py` posts via tweepy. Toggle at runtime with `/autopost on|off` — no restart needed. When enabled, approving a draft shows a scheduling keyboard (+1h/+2h/+4h/+8h/+24h/Custom/Now). Scheduled posts fired by a job running every 60 seconds.

**Model switching** — `CLAUDE_MODEL` in `.env`. No code changes needed.

**Whisper quality** — `WHISPER_MODEL=base` by default (fast). Change to `medium` or `large-v3` for better accuracy on accents or technical content.

**Paths with spaces** — `/processlocal` and `/promolocal` auto-detect the file path by scanning argument prefixes against disk. No quotes needed.

**Emoji in posts** — system prompt instructs Claude to start every tweet with one contextually relevant emoji.

**Bot command menu** — registered via `set_my_commands()` on startup. Typing `/` in Telegram shows all commands with descriptions.

## Telegram Commands

| Command | Action |
|---|---|
| `/check` | Trigger daily YouTube channel check manually |
| `/process <url>` | Extract tweet ideas from a YouTube URL |
| `/processall` | Process entire unprocessed channel backlog |
| `/processlocal <path> [title]` | Extract tweet ideas from a local file |
| `/promo <url>` | Generate title + hook + caption for a YouTube video |
| `/promolocal <path> [title]` | Generate promo content from a local file |
| `/queue` | Show all approved posts ready to copy-paste |
| `/retrospective` | Re-analyse all archived transcripts |
| `/autopost [on\|off]` | Toggle X auto-posting; no args shows current state |
| `/status` | Stats + recent video job history |

**Sending a file to the bot** — bot downloads it (≤20 MB Telegram limit), asks for title, Whisper transcribes, generates tweet drafts.

**Inline buttons on each draft pair** — ✅ Original / ✅ Trend angle / ❌ Reject both.

**Edit flow** — tap ✏️ on a single draft, bot prompts for new text. Single tweet = plain text. Thread = tweets separated by `---` on its own line.

**Queue actions** — Post Now / Schedule / Mark as posted. Scheduling keyboard shown when X keys configured.

## State Dicts (in-memory, telegram_bot.py)

```python
_pending_edits: dict[int, int]               # chat_id → draft_id
_pending_file_titles: dict[int, tuple]       # chat_id → (tmp_path, suggested_title)
_pending_schedule_times: dict[int, int]      # chat_id → draft_id awaiting custom time
```

Lost on bot restart — in-flight edits/uploads drop silently. Acceptable given low volume.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in all values
python telegram_bot.py
```

Daily check fires at `DAILY_CHECK_HOUR` UTC (default 9). Bot must stay running — host on a VPS or keep local machine on.

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
| `AUTO_POST` | `false` | Default state for X auto-posting on startup |
| `TWITTER_API_KEY` | — | X Developer Portal |
| `TWITTER_API_SECRET` | — | X Developer Portal |
| `TWITTER_ACCESS_TOKEN` | — | X Developer Portal |
| `TWITTER_ACCESS_SECRET` | — | X Developer Portal |
