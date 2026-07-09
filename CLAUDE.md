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
config.py          ← all env vars, loaded once at import
twitter_poster.py  ← X/Twitter API posting via tweepy
fetch_transcript.py ← standalone script: fetch transcript locally, no Claude
```

Commands: full table in README.md; registered in `telegram_bot.py` via `set_my_commands()`.
Env vars: `.env.example` for the list, `config.py` for defaults — those files are the source of truth.

## Database Schema

Four tables in `david_bot.db`:

```sql
videos      — youtube_id (UNIQUE), title, url, transcript_path, processed_at,
              retrospective_reviewed_at, source ('youtube'|'local'), duration_seconds

drafts      — video_id, format (tweet|thread|promo|article), content (TEXT), status
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
- `format=article` → JSON object `{"title": "...", "body": "..."}` (body uses X article markdown)

## Key Design Decisions

**Feedback loop** — every approved post saved to `good_posts`, loaded as few-shot style examples in next Claude call. Quality improves as more posts approved. `/addexample` inserts examples directly, no video needed.

**Two-version system** — every generated idea comes as two versions:
- Version A (Original): matches David's exact vocabulary, tone, rhythm from transcript
- Version B (Trend angle): same idea reframed for current X performance patterns
- Telegram shows both; approving one auto-rejects the other via `pair_id` link
- Tweets → single combined message with `[✅ Original] [✅ Trend] [❌ Reject both]`
- Threads → two separate messages (full content visible), each with own approve button

**Video promo content** — separate from tweet extraction: title + hook + caption to post *alongside* a video, not ideas from it. Same transcript, different prompt, stored as `format=promo`. Manual copy-paste workflow — bot does not upload video files to X.

**Transcripts** — YouTube auto-captions first (fast, free), Whisper fallback (slower, better). Local files go straight to Whisper. All cached as `.txt` in `TRANSCRIPTS_DIR`; `get_transcript()` checks cache first, so `/process` then `/promo` on the same video fetches once.

**Retrospective** — `retrospective.py` re-mines old transcripts monthly using current approved examples. Won't run until 5+ good posts exist.

**Long-form articles** — `/article <url>` writes a full X article (single version, no trend angle), delivered as `.txt` attachment. Output format abstracted via `format_article_for_output()` — `ARTICLE_OUTPUT_FORMAT` env switches platforms.

**Edit flow** — ✏️ button prompts for replacement text. Single tweet = plain text; thread = tweets separated by `---` on its own line.

**Paths with spaces** — `/processlocal` and `/promolocal` auto-detect the file path by scanning argument prefixes against disk. No quotes needed.

**Emoji in posts** — system prompt instructs Claude to start every tweet with one contextually relevant emoji.

## State Dicts (in-memory, telegram_bot.py)

```python
_pending_edits: dict[int, int]               # chat_id → draft_id
_pending_file_titles: dict[int, tuple]       # chat_id → (tmp_path, suggested_title)
_pending_schedule_times: dict[int, int]      # chat_id → draft_id awaiting custom time
_pending_examples: dict[int, bool]           # chat_id → True when awaiting example text
```

Lost on bot restart — in-flight edits/uploads drop silently. Acceptable given low volume.

## Deployment

Production runs on a VPS. Host, service name, and the full deploy procedure live in `CLAUDE.local.md` (gitignored — contains the server address; never move that content into committed files).

Read LEARNING.md at the start of every session and follow its instructions.
