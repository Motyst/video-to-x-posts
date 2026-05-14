import json
import logging
from anthropic import Anthropic
from database import get_good_posts
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)
client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """\
You are a social media content creator for David's X (Twitter) account.

Goal: extract the most valuable, shareable ideas from YouTube video transcripts \
and write engaging posts that sound exactly like David.

Rules:
- Match David's communication style from the transcript — his vocabulary, tone, \
sentence rhythm, energy level. If he speaks casually, write casually. \
If he's intense, write intense.
- No generic motivational filler. Every post must deliver a specific, concrete idea.
- Single tweet: sharp insight, memorable quote, or punchy one-liner. Max 280 chars.
- Thread: an idea that earns depth — 3 to 7 tweets. Each tweet must stand alone \
but pull the reader to the next one.
- Extract as many genuinely good ideas as the transcript contains. Don't pad, \
don't cut good ideas to hit a number.

Output: a JSON array only — no explanation, no markdown, just the array.
Schema:
[
  {"format": "tweet", "content": "tweet text here"},
  {"format": "thread", "content": ["tweet 1", "tweet 2", "tweet 3"]}
]
"""


def generate_posts(youtube_id: str, title: str, transcript: str) -> list:
    good_posts = get_good_posts(limit=15)
    few_shot = _build_few_shot_block(good_posts)

    user_prompt = (
        f'Video: "{title}"\n\n'
        f"{few_shot}"
        f"Transcript:\n{transcript[:20000]}\n\n"
        "Extract the best ideas and return only the JSON array."
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    raw = _strip_code_fence(raw)

    try:
        posts = json.loads(raw)
        if not isinstance(posts, list):
            raise ValueError("Expected a JSON array")
        return posts
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Invalid Claude response for {youtube_id}: {e}\nRaw: {raw[:300]}")
        return []


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.split("\n")
        # drop first line (```json or ```) and last line (```)
        inner = lines[1:] if lines[-1].strip() == "```" else lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return text


def _build_few_shot_block(good_posts: list) -> str:
    if not good_posts:
        return ""

    examples = []
    for post in good_posts:
        if post["format"] == "tweet":
            examples.append(f"[Approved tweet]\n{post['content']}")
        else:
            tweets = json.loads(post["content"])
            thread_text = "\n".join(f"{i + 1}/ {t}" for i, t in enumerate(tweets))
            examples.append(f"[Approved thread]\n{thread_text}")

    block = "Style examples (approved posts — match this tone exactly):\n\n"
    block += "\n\n---\n\n".join(examples)
    block += "\n\n"
    return block
