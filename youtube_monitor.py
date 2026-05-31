import re
import logging
import yt_dlp
from database import get_processed_video_ids
from config import YOUTUBE_COOKIES_FILE

logger = logging.getLogger(__name__)

_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


def _cookies_opt() -> dict:
    """Return cookiefile opt if configured, else empty dict."""
    if YOUTUBE_COOKIES_FILE:
        return {"cookiefile": YOUTUBE_COOKIES_FILE}
    return {}


def fetch_channel_videos(channel_url: str) -> list:
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        **_cookies_opt(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    # yt-dlp may return nested playlists (Shorts, Live tabs) — flatten one level
    raw_entries = info.get("entries", [])
    entries = []
    for e in raw_entries:
        if e.get("_type") == "playlist" or (e.get("id") and len(e["id"]) != 11):
            # sub-playlist: grab its entries if already loaded, else skip
            for sub in e.get("entries") or []:
                entries.append(sub)
        else:
            entries.append(e)

    videos = []
    for entry in entries:
        vid_id = entry.get("id")
        # YouTube video IDs are always exactly 11 chars — skip channels/playlists
        if vid_id and len(vid_id) == 11:
            videos.append({
                "youtube_id": vid_id,
                "title": entry.get("title", ""),
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "duration_seconds": entry.get("duration"),
            })
    return videos


def get_new_videos(channel_url: str) -> list:
    """Return only videos not yet in the database."""
    all_videos = fetch_channel_videos(channel_url)
    processed = get_processed_video_ids()
    new = [v for v in all_videos if v["youtube_id"] not in processed]
    logger.info(f"Channel has {len(all_videos)} videos, {len(new)} new")
    return new


def get_unprocessed_videos(channel_url: str) -> list:
    """Alias of get_new_videos — full backlog scan."""
    return get_new_videos(channel_url)


def fetch_single_video(url: str) -> dict | None:
    """Fetch metadata for one video URL (any YouTube URL format).

    Uses process=False to skip format resolution — we only need id/title/duration.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        **_cookies_opt(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # process=False skips format selection — safe for metadata-only fetches
            info = ydl.extract_info(url, download=False, process=False)
        except Exception as e:
            logger.error(f"Failed to fetch video info for {url}: {e}")
            return None

    if not info:
        return None
    vid_id = info.get("id")
    if not vid_id:
        return None
    return {
        "youtube_id": vid_id,
        "title": info.get("title", ""),
        "url": f"https://www.youtube.com/watch?v={vid_id}",
        "duration_seconds": info.get("duration"),
    }
