import json
import logging
import time
import tweepy
from config import (
    TWITTER_API_KEY,
    TWITTER_API_SECRET,
    TWITTER_ACCESS_TOKEN,
    TWITTER_ACCESS_SECRET,
)

logger = logging.getLogger(__name__)


def _client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_SECRET,
    )


def _v1_api() -> tweepy.API:
    """v1.1 API — required for chunked media upload."""
    auth = tweepy.OAuth1UserHandler(
        TWITTER_API_KEY, TWITTER_API_SECRET,
        TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET,
    )
    return tweepy.API(auth)


def post_tweet(content: str) -> str:
    """Post a single tweet. Returns URL."""
    client = _client()
    response = client.create_tweet(text=content)
    tweet_id = response.data["id"]
    return f"https://x.com/i/web/status/{tweet_id}"


def post_thread(tweets: list) -> str:
    """Post a thread. Returns URL of first tweet."""
    client = _client()
    previous_id = None
    first_url = None

    for i, tweet_text in enumerate(tweets):
        kwargs = {"text": tweet_text}
        if previous_id:
            kwargs["in_reply_to_tweet_id"] = previous_id

        response = client.create_tweet(**kwargs)
        tweet_id = response.data["id"]

        if i == 0:
            first_url = f"https://x.com/i/web/status/{tweet_id}"
        previous_id = tweet_id

    return first_url


def post_video_tweet(video_path: str, caption: str) -> str:
    """Upload a video file and post a tweet with it. Returns URL.

    To change video upload behavior: modify media_category or add_alt_text here.
    """
    api = _v1_api()
    client = _client()

    logger.info(f"Uploading video to X: {video_path}")
    media = api.media_upload(
        filename=video_path,
        chunked=True,
        media_category="tweet_video",
    )
    media_id = media.media_id

    # Poll until X finishes processing the video
    if hasattr(media, "processing_info"):
        while True:
            state = media.processing_info.get("state", "succeeded")
            if state == "succeeded":
                break
            if state == "failed":
                err = media.processing_info.get("error", {})
                raise Exception(f"X video processing failed: {err}")
            wait = media.processing_info.get("check_after_secs", 5)
            logger.info(f"X video processing: {state}, retrying in {wait}s")
            time.sleep(wait)
            media = api.get_media_upload_status(media_id)

    response = client.create_tweet(text=caption, media_ids=[str(media_id)])
    tweet_id = response.data["id"]
    logger.info(f"Video tweet posted: {tweet_id}")
    return f"https://x.com/i/web/status/{tweet_id}"


def post_reply(parent_tweet_id: str, content: str) -> str:
    """Post a reply to an existing tweet. Returns URL."""
    client = _client()
    response = client.create_tweet(text=content, in_reply_to_tweet_id=parent_tweet_id)
    tweet_id = response.data["id"]
    return f"https://x.com/i/web/status/{tweet_id}"


def post_draft(fmt: str, content: str) -> str:
    """Post a draft (tweet, thread, or video_post). Returns URL."""
    if fmt == "tweet":
        return post_tweet(content)
    elif fmt == "video_post":
        data = json.loads(content)
        return post_video_tweet(data["path"], data["caption"])
    else:
        tweets = json.loads(content)
        return post_thread(tweets)


def twitter_configured() -> bool:
    """Check if all Twitter API keys are set."""
    return all([
        TWITTER_API_KEY,
        TWITTER_API_SECRET,
        TWITTER_ACCESS_TOKEN,
        TWITTER_ACCESS_SECRET,
    ])
