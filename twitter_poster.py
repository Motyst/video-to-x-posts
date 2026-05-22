import json
import logging
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


def post_draft(fmt: str, content: str) -> str:
    """Post a draft (tweet or thread). Returns URL."""
    if fmt == "tweet":
        return post_tweet(content)
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
