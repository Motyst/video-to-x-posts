import json
import logging
from pathlib import Path
from database import (
    get_videos_for_retrospective,
    add_draft,
    mark_retrospective_reviewed,
    count_good_posts,
)
from content_generator import generate_posts

logger = logging.getLogger(__name__)

MIN_GOOD_POSTS = 5


async def run_retrospective(send_draft_fn) -> int:
    """Re-mine archived transcripts using current approved posts as style examples.

    send_draft_fn signature:
        async (draft_id, video_title, fmt, content, is_retrospective=True) -> None
    """
    n_good = count_good_posts()
    if n_good < MIN_GOOD_POSTS:
        logger.info(
            f"Retrospective skipped — only {n_good} approved posts "
            f"(need {MIN_GOOD_POSTS})"
        )
        return 0

    videos = get_videos_for_retrospective()
    logger.info(f"Retrospective: {len(videos)} videos to re-analyse")
    total = 0

    for video in videos:
        transcript_path = video.get("transcript_path")
        if not transcript_path or not Path(transcript_path).exists():
            logger.warning(f"Transcript missing for {video['youtube_id']} — skipping")
            mark_retrospective_reviewed(video["id"])
            continue

        transcript = Path(transcript_path).read_text(encoding="utf-8")

        try:
            posts = generate_posts(video["youtube_id"], video["title"], transcript)
        except Exception as e:
            logger.error(f"Generation failed for {video['youtube_id']}: {e}")
            continue

        for post in posts:
            content_str = (
                post["content"]
                if isinstance(post["content"], str)
                else json.dumps(post["content"])
            )
            draft_id = add_draft(video["id"], post["format"], content_str)
            await send_draft_fn(
                draft_id,
                video["title"],
                post["format"],
                post["content"],
                is_retrospective=True,
            )
            total += 1

        mark_retrospective_reviewed(video["id"])
        logger.info(f"Retrospective: {len(posts)} drafts from '{video['title']}'")

    return total
