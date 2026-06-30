import json
import logging
from anthropic import Anthropic
from database import get_good_posts
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, ARTICLE_TARGET_WORDS, ARTICLE_OUTPUT_FORMAT

logger = logging.getLogger(__name__)
client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """\
You are a social media content creator for David's X (Twitter) account.

Goal: extract the most valuable ideas from YouTube video transcripts and write \
two versions of each post.

Rules for both versions:
- NEVER use the em dash character —. It is the single most detectable AI writing tell. Replace with a period, comma, colon, or rewrite the sentence. This rule has no exceptions.
- No generic motivational filler. Every post must deliver a specific, concrete idea.
- No emojis. Do not start tweets or threads with emojis.
- Single tweet: sharp insight, memorable quote, or punchy one-liner. Max 280 chars. \
Must be fully self-contained — no open questions, no cliffhangers, no "here's why", \
no unanswered setups. If an idea needs a follow-up to make sense, it is a thread, not a tweet.
- Thread: an idea that earns depth (3 to 7 tweets). Pick the style that fits:

  Style A — Curiosity thread (use when the idea has list structure, numbered lessons, \
or a surprising payoff the reader has to earn):
    • Tweet 1: bold, specific hook that creates an open loop. End with "Here's why:", \
"Here's what I did:", a numbered first item implying more, or "..." to force the tap. \
Make the reader feel they are about to miss something important.
    • Tweets 2+: deliver the payoff. Numbered if it is a list. Each one punchy, \
self-contained. Last tweet: strong conclusion or unexpected close.
    • Strong opener patterns: "Nobody warned me about X. So I am warning you." / \
"I spent Y years doing X. Here is what I would do instead:" / \
"Most men get this wrong about [topic]." / "[Specific bold claim]. Here is why:"

  Style B — Insight/argument thread (use when the idea flows as a story or builds \
as a single argument):
    • Each tweet builds on the previous. No forced numbering. Reads like a conversation.

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
- NEVER write in third person. Never say "David explains", "David shares", "he talks about", or any variant. Write AS the person speaking, in first person, or write directly to the viewer in second person.
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


ARTICLE_SYSTEM_PROMPT = """\
You are a ghostwriter for David's X (Twitter) long-form articles.

Goal: write a single article from the video transcript in David's exact voice.

Rules:
- NEVER use the em dash character —. Replace with period, comma, colon, or rewrite. No exceptions.
- Write ONLY in David's voice — his vocabulary, rhythm, energy, sentence structure from the transcript.
- No filler, no generic advice. Every sentence must carry weight.
- Structure: clear title, natural sections with headers, strong opening, strong close.
- Length: judge based on content depth. Short transcript = shorter article. Rich transcript = longer. \
Do not pad. Do not cut good material.
- No corporate language, no AI-sounding phrases.

Output: a JSON object only — no explanation, no markdown wrapper, just the object.
Schema:
{
  "title": "Article title",
  "body": "Full article body using X article markdown:\\n# for section headers\\n\\nParagraphs separated by blank lines\\n**bold** for emphasis"
}
"""


def format_article_for_output(article: dict, fmt: str = None) -> str:
    """Convert article dict to a string ready for the target platform.

    Change ARTICLE_OUTPUT_FORMAT in .env (or pass fmt) to switch platforms.
    Supported: 'x_native' (default).
    Future: 'substack', 'html', 'plain'.
    """
    fmt = fmt or ARTICLE_OUTPUT_FORMAT
    title = article.get("title", "").strip()
    body = article.get("body", "").strip()

    if fmt == "x_native":
        # X article editor: title on first line, blank line, then body markdown
        return f"{title}\n\n{body}"

    # Fallback — plain concatenation
    return f"{title}\n\n{body}"


def generate_article(youtube_id: str, title: str, transcript: str) -> dict | None:
    """Generate a long-form article in David's voice.

    Returns dict with keys: title, body. Returns None on failure.
    To change target length: set ARTICLE_TARGET_WORDS in .env (e.g. '800').
    """
    good_posts = get_good_posts(limit=10)
    few_shot = _build_few_shot_block(good_posts)

    length_hint = ""
    if ARTICLE_TARGET_WORDS:
        length_hint = f"Target length: approximately {ARTICLE_TARGET_WORDS} words.\n\n"

    user_prompt = (
        f'Video: "{title}"\n\n'
        f"{few_shot}"
        f"{length_hint}"
        f"Transcript:\n{transcript[:25000]}\n\n"
        "Write the article. Return only the JSON object."
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            system=ARTICLE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        logger.error(f"Article generation failed for {youtube_id}: {e}")
        return None

    raw = response.content[0].text.strip()
    raw = _strip_code_fence(raw)

    try:
        article = json.loads(raw)
        if not isinstance(article, dict):
            raise ValueError("Expected JSON object")
        # strip em dashes from all fields
        return {
            k: v.replace("—", ",").replace(" ,", ",") if isinstance(v, str) else v
            for k, v in article.items()
        }
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Invalid article response for {youtube_id}: {e}\nRaw: {raw[:300]}")
        return None


VIDEO_CAPTION_SYSTEM_PROMPT = """\
You are a caption writer for video posts on X (Twitter).

Goal: write 4 different captions for the same video. All in the exact voice of the person \
speaking — derived from the transcript. No exceptions.

Rules:
- NEVER use the em dash character —. Replace with period, comma, colon, or rewrite.
- NEVER write in third person. Never say "David says", "he explains", "the speaker shares", or any variant.
- Write AS the speaker (first person) or directly to the viewer (second person).
- Every caption must feel like this specific person wrote it — use their vocabulary, rhythm, energy.
- No generic motivational filler. Every sentence must earn its place.

Caption 1 — Original (1-2 sentences, SHORT):
Most faithful to exactly what was said. Use the speaker's own words and phrasing wherever possible.

Caption 2 — Problem (1-2 sentences, SHORT):
Target a specific struggle that ambitious people face which this video addresses. \
Create instant recognition ("that's me") without giving away the solution.

Caption 3 — Key Lesson (3-4 sentences, LONGER):
Extract the single most important insight. Build from setup to payoff. End with why it matters.

Caption 4 — Hook (3-4 sentences, LONGER):
A compelling hook that would stop the scroll. Any angle — story, contrast, bold claim, \
unexpected truth — as long as it is grounded in this video's content.

Output: JSON object only — no explanation, no markdown wrapper.
Schema:
{
  "original": "...",
  "problem": "...",
  "lesson": "...",
  "hook": "..."
}
"""


def generate_video_post_captions(video_id: str, title: str, transcript: str) -> dict | None:
    """Generate 4 caption variants for a video post on X.

    Returns dict with keys: original, problem, lesson, hook. None on failure.
    """
    user_prompt = (
        f'Video: "{title}"\n\n'
        f"Transcript:\n{transcript[:20000]}\n\n"
        "Write the 4 captions. Return only the JSON object."
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=VIDEO_CAPTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        logger.error(f"Video caption generation failed for {video_id}: {e}")
        return None

    raw = response.content[0].text.strip()
    raw = _strip_code_fence(raw)

    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Expected JSON object")
        return {
            k: v.replace("—", ",").replace(" ,", ",") if isinstance(v, str) else v
            for k, v in result.items()
        }
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Invalid caption response for {video_id}: {e}\nRaw: {raw[:300]}")
        return None


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


HOOK_REWRITE_PROMPT = """\
You are an X (Twitter) hook specialist writing in David's exact voice.

Goal: rewrite the given hook/opening line as 3 stronger alternatives optimized for X engagement.

Rules:
- NEVER use the em dash character —. Replace with period, comma, colon, or rewrite.
- Stay in David's voice — use his vocabulary, rhythm, energy from the style examples.
- Each variant must work as a standalone tweet opener.
- Start with one relevant emoji.

Write exactly 3 variants:
1. Curiosity gap: implies a payoff without revealing it, makes reader tap "more"
2. Bold claim: counterintuitive, specific, challenges a common assumption
3. Pattern interrupt: unexpected angle, contrast, or framing that stops the scroll

Output: JSON array of exactly 3 strings — no explanation, no markdown.
["variant 1", "variant 2", "variant 3"]
"""


def rewrite_hook(hook_text: str, video_title: str) -> list[str] | None:
    """Generate 3 alternative hooks. Returns list of 3 strings or None on failure."""
    good_posts = get_good_posts(limit=10)
    few_shot = _build_few_shot_block(good_posts)

    user_prompt = (
        f'Video: "{video_title}"\n\n'
        f"{few_shot}"
        f"Current hook:\n{hook_text}\n\n"
        "Write 3 alternative hooks. Return only the JSON array."
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            system=HOOK_REWRITE_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        logger.error(f"Hook rewrite failed: {e}")
        return None

    raw = response.content[0].text.strip()
    raw = _strip_code_fence(raw)

    try:
        variants = json.loads(raw)
        if not isinstance(variants, list) or len(variants) < 3:
            raise ValueError("Expected array of 3")
        return [v.replace("—", ",").replace(" ,", ",") for v in variants[:3]]
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Invalid hook rewrite response: {e}\nRaw: {raw[:300]}")
        return None


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
