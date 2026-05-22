import json
import logging
from pathlib import Path
from database import (
    get_videos_for_retrospective,
    add_draft_pair,
    mark_retrospective_reviewed,
    count_good_posts,
)
from content_generator import generate_posts

logger = logging.getLogger(__name__)

MIN_GOOD_POSTS = 5


async def run_retrospective(send_pair_fn) -> int:
    """Re-mine archived transcripts using current approved posts as style examples.

    send_pair_fn signature:
        async (id_a, id_b, video_title, idea, is_retrospective=True) -> None
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
            ideas = generate_posts(video["youtube_id"], video["title"], transcript)
        except Exception as e:
            logger.error(f"Generation failed for {video['youtube_id']}: {e}")
            continue

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
                video["id"], idea["format"],
                original_str, trend_str, idea.get("trend_reason", "")
            )
            await send_pair_fn(
                id_a, id_b, video["title"], idea, is_retrospective=True
            )
            total += 1

        mark_retrospective_reviewed(video["id"])
        logger.info(f"Retrospective: {len(ideas)} idea pairs from '{video['title']}'")

    return total
