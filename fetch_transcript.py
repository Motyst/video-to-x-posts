"""
Fetch and cache transcript for a YouTube URL — no Claude, no drafts.
Run on laptop, then upload transcript to VPS.

Usage:
    python fetch_transcript.py <youtube_url>
"""

import sys
from pathlib import Path
from youtube_monitor import fetch_single_video
from transcript import get_transcript

def main():
    if len(sys.argv) < 2:
        print("Usage: python fetch_transcript.py <youtube_url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Fetching video info: {url}")

    video = fetch_single_video(url)
    if not video:
        print("ERROR: Could not fetch video info.")
        sys.exit(1)

    vid_id = video["youtube_id"]
    title = video["title"]
    print(f"Video: {title} ({vid_id})")

    print("Fetching transcript...")
    transcript = get_transcript(vid_id, title)
    if not transcript:
        print("ERROR: Could not get transcript.")
        sys.exit(1)

    txt_path = Path("transcripts") / f"{vid_id}.txt"
    print(f"Done. Transcript saved: {txt_path} ({len(transcript)} chars)")
    print()
    print(f"Now upload to VPS:")
    print(f'  scp "{txt_path.resolve()}" root@5.78.218.169:/root/david_post_bot/transcripts/')
    print(f"Then in Telegram: /process {url}")

if __name__ == "__main__":
    main()
