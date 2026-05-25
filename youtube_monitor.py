import re
import logging
import yt_dlp
from database import get_processed_video_ids

logger = logging.getLogger(__name__)

_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


def fetch_channel_videos(channel_url: str) -> list:
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    entries = info.get("entries", [])
    videos = []
    for entry in entries:
        vid_id = entry.get("id")
        if vid_id:
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
    """Fetch metadata for one video URL (any YouTube URL format)."""
    ydl_opts = {"quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            logger.error(f"Failed to fetch video info for {url}: {e}")
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
