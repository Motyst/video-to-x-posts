# YouTube → X Post Bot

Telegram bot that monitors a YouTube channel, transcribes videos, and generates X (Twitter) posts matching the channel owner's communication style. Posts are reviewed and approved via Telegram before publishing.

## What it does

1. **Monitors** a YouTube channel for new videos (daily automatic check)
2. **Transcribes** each video (YouTube captions → Whisper fallback)
3. **Generates** X post ideas from the transcript using Claude AI
4. **Sends drafts to Telegram** for review — approve, edit, or reject
5. **Posts to X** manually or automatically on a schedule

Every idea comes in two versions:
- **Original** — matches the creator's exact vocabulary and tone
- **Trend angle** — same idea reframed around content patterns that perform well on X

The bot gets smarter over time: every approved post is saved as a style example and fed back into future Claude prompts.

## Stack

- **Python** — core language
- **Claude (Anthropic)** — post generation
- **Telegram Bot API** — review queue interface
- **yt-dlp + youtube-transcript-api** — video transcription
- **faster-whisper** — local Whisper fallback transcription
- **tweepy** — X/Twitter posting
- **SQLite** — local database
- **APScheduler** — daily check + scheduled posting

## Key features

- Two-version drafts (original style vs trend angle) for every idea
- Inline Telegram review — approve / edit / reject without leaving chat
- Promo content generation (title + hook + caption) for posting alongside videos
- Scheduled X posting (+1h / +2h / +4h / +8h / +24h / custom)
- Retrospective: re-mines old transcripts monthly using newer style examples
- Local file processing — transcribe and generate posts from video files on disk
- Style feedback loop — approved posts improve future generation quality

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in all values
python telegram_bot.py
```

## Environment variables

| Variable | Notes |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | Your personal chat ID with the bot |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `CLAUDE_MODEL` | Default: `claude-sonnet-4-6` |
| `YOUTUBE_CHANNEL_URL` | Channel or playlist URL to monitor |
| `YOUTUBE_COOKIES_FILE` | Path to cookies.txt (needed on VPS to bypass IP blocks) |
| `WHISPER_MODEL` | Default: `base`. Use `medium` or `large-v3` for better accuracy |
| `WHISPER_DEVICE` | `cpu` or `cuda` |
| `AUTO_POST` | `true` to enable X auto-posting on startup |
| `TWITTER_API_KEY` / `TWITTER_API_SECRET` / `TWITTER_ACCESS_TOKEN` / `TWITTER_ACCESS_SECRET` | X Developer Portal |

## Telegram commands

| Command | Action |
|---|---|
| `/check` | Trigger daily YouTube channel check |
| `/process <url>` | Generate post drafts from a YouTube URL |
| `/processlocal <path>` | Generate post drafts from a local video file |
| `/promo <url>` | Generate promo content for a YouTube video |
| `/promolocal <path>` | Generate promo content from a local video file |
| `/processall` | Process entire unprocessed channel backlog |
| `/queue` | Show approved posts ready to publish |
| `/addexample` | Add a post as a style example for Claude |
| `/retrospective` | Re-analyse all archived transcripts |
| `/autopost [on\|off]` | Toggle X auto-posting |
| `/status` | Stats and recent job history |

## Processing videos on VPS (IP block workaround)

YouTube blocks most VPS IPs. Workaround:

1. Run `python fetch_transcript.py <url>` on your local machine
2. Upload the generated transcript: `scp transcripts/<id>.txt user@server:/path/transcripts/`
3. Run `/process <url>` in Telegram — bot uses cached transcript, skips YouTube
