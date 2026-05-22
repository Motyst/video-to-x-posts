import logging
from pathlib import Path
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp
from config import TRANSCRIPTS_DIR, WHISPER_DEVICE, WHISPER_COMPUTE, WHISPER_MODEL

logger = logging.getLogger(__name__)


def get_transcript(youtube_id: str, title: str) -> str | None:
    Path(TRANSCRIPTS_DIR).mkdir(exist_ok=True)

    cache = Path(TRANSCRIPTS_DIR) / f"{youtube_id}.txt"
    if cache.exists():
        logger.info(f"Transcript cache hit: {youtube_id}")
        return cache.read_text(encoding="utf-8")

    text = _try_captions(youtube_id) or _try_whisper(youtube_id, title)

    if text:
        cache.write_text(text, encoding="utf-8")
        logger.info(f"Transcript saved: {youtube_id} ({len(text)} chars)")

    return text


def _try_captions(youtube_id: str) -> str | None:
    try:
        api = YouTubeTranscriptApi()
        transcript = api.fetch(youtube_id)
        return " ".join(entry.text for entry in transcript)
    except Exception as e:
        logger.info(f"No captions for {youtube_id} — trying Whisper ({e})")
        return None


def transcribe_local_file(file_path: str, video_id: str) -> str | None:
    """Transcribe a local audio/video file with Whisper and cache the result."""
    Path(TRANSCRIPTS_DIR).mkdir(exist_ok=True)

    cache = Path(TRANSCRIPTS_DIR) / f"{video_id}.txt"
    if cache.exists():
        logger.info(f"Transcript cache hit: {video_id}")
        return cache.read_text(encoding="utf-8")

    text = _run_whisper(file_path, video_id)
    if text:
        cache.write_text(text, encoding="utf-8")
        logger.info(f"Local transcript saved: {video_id} ({len(text)} chars)")
    return text


def _try_whisper(youtube_id: str, title: str) -> str | None:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error("faster-whisper not installed. Run: pip install faster-whisper")
        return None

    audio_path = Path(TRANSCRIPTS_DIR) / f"{youtube_id}.mp3"
    url = f"https://www.youtube.com/watch?v={youtube_id}"

    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(audio_path.with_suffix("")),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            "quiet": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        return _run_whisper(str(audio_path), youtube_id)
    except Exception as e:
        logger.error(f"Whisper (YouTube download) failed for {youtube_id}: {e}")
        return None
    finally:
        audio_path.unlink(missing_ok=True)


def _run_whisper(audio_path: str, label: str) -> str | None:
    """Run Whisper on any local audio/video file."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error("faster-whisper not installed. Run: pip install faster-whisper")
        return None

    try:
        logger.info(f"Whisper transcribing '{label}' ({WHISPER_DEVICE}/{WHISPER_MODEL})")
        model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
        segments, _ = model.transcribe(audio_path)
        return " ".join(seg.text.strip() for seg in segments)
    except Exception as e:
        logger.error(f"Whisper failed for '{label}': {e}")
        return None
