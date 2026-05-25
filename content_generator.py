import json
import logging
from anthropic import Anthropic
from database import get_good_posts
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)
client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """\
You are a social media content creator for David's X (Twitter) account.

Goal: extract the most valuable ideas from YouTube video transcripts and write \
two versions of each post.

Rules for both versions:
- NEVER use the em dash character —. It is the single most detectable AI writing tell. Replace with a period, comma, colon, or rewrite the sentence. This rule has no exceptions.
- No generic motivational filler. Every post must deliver a specific, concrete idea.
- Start every tweet (including each tweet in a thread) with one relevant emoji. Pick based on the content, not randomly. One emoji only — at the very beginning.
- Single tweet: sharp insight, memorable quote, or punchy one-liner. Max 280 chars.
- Thread: an idea that earns depth (3 to 7 tweets). Each tweet must stand alone \
but pull the reader to the next one.
- Extract as many genuinely good ideas as the transcript contains.

Version A — Original:
- Match David's communication style exactly from the transcript.
- His vocabulary, tone, sentence rhythm, energy level.
- Faithful to what he actually said and how he said it.

Version B — Trend angle:
- Same core idea, reframed around a content angle that currently performs well on X.
- Think: what hook, frame, or narrative structure is getting traction right now?
- Strong angles: counter-intuitive takes, specific numbers, direct challenges, \
"most people do X, here's why that's wrong", story-first openers.
- Still sounds human. No corporate language.
- Include a brief reason (1-2 sentences) explaining the angle choice.

Output: a JSON array only — no explanation, no markdown, just the array.
Schema:
[
  {
    "format": "tweet",
    "original": "version A tweet text",
    "trend": "version B tweet text",
    "trend_reason": "Why this angle works right now"
  },
  {
    "format": "thread",
    "original": ["tweet 1", "tweet 2", "tweet 3"],
    "trend": ["tweet 1", "tweet 2", "tweet 3"],
    "trend_reason": "Why this angle works right now"
  }
]
"""


PROMO_SYSTEM_PROMPT = """\
You are a social media content creator for David's X (Twitter) account.

Goal: write promotional content that makes people want to watch David's video.
This is NOT about extracting ideas — it is about selling the video itself.

Rules:
- NEVER use the em dash character —. Replace with period, comma, colon, or rewrite. No exceptions.
- No generic hype. Every line must be specific to what is actually in this video.
- Title: under 100 chars, creates curiosity, makes the viewer feel they are missing something.
- Hook: single opening line. Stops the scroll. Implies a payoff without giving it away.
- Caption: 3 to 5 sentences. Tease the value, build tension, end with a soft CTA to watch.

Version A — Original:
- Match David's communication style exactly from the transcript.
- His vocabulary, tone, sentence rhythm, energy level.

Version B — Trend angle:
- Same video, reframed around a content angle that currently performs well on X.
- Strong angles: curiosity gaps, specific numbers, direct challenges, story-first openers.
- Include a brief reason (1 to 2 sentences) explaining the angle choice.

Output: a JSON object only — no explanation, no markdown, just the object.
Schema:
{
  "title_a": "Version A title",
  "hook_a": "Version A hook (one line)",
  "caption_a": "Version A full caption",
  "title_b": "Version B title",
  "hook_b": "Version B hook (one line)",
  "caption_b": "Version B full caption",
  "trend_reason": "Why this angle works right now"
}
"""


def generate_promo(youtube_id: str, title: str, transcript: str) -> dict | None:
    """Generate promotional content (title + hook + caption) for a video.

    Returns dict with keys: title_a, hook_a, caption_a, title_b, hook_b, caption_b, trend_reason.
    Returns None on failure.
    """
    user_prompt = (
        f'Video: "{title}"\n\n'
        f"Transcript:\n{transcript[:20000]}\n\n"
        "Write promotional content for this video. Return only the JSON object."
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=PROMO_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    raw = _strip_code_fence(raw)

    try:
        promo = json.loads(raw)
        if not isinstance(promo, dict):
            raise ValueError("Expected a JSON object")
        return {
            k: v.replace("—", ",").replace(" ,", ",") if isinstance(v, str) else v
            for k, v in promo.items()
        }
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Invalid promo response for {youtube_id}: {e}\nRaw: {raw[:300]}")
        return None


def generate_posts(youtube_id: str, title: str, transcript: str) -> list:
    """Returns list of idea dicts with 'original', 'trend', 'trend_reason', 'format'."""
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
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    raw = _strip_code_fence(raw)

    try:
        ideas = json.loads(raw)
        if not isinstance(ideas, list):
            raise ValueError("Expected a JSON array")
        return [_clean_idea(idea) for idea in ideas]
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Invalid Claude response for {youtube_id}: {e}\nRaw: {raw[:300]}")
        return []


def _clean_idea(idea: dict) -> dict:
    """Strip em dashes from all text content regardless of prompt compliance."""
    def clean(v):
        if isinstance(v, str):
            return v.replace("—", ",").replace(" ,", ",")
        if isinstance(v, list):
            return [clean(t) for t in v]
        return v

    return {k: clean(v) for k, v in idea.items()}


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.split("\n")
        inner = lines[1:]
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
