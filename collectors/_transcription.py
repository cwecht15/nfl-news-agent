"""Shared transcription helpers.

Caption parsing, audio downloads (for non-YouTube sources like podcast MP3
enclosures), and Whisper transcription. Used by the YouTube collector and
the beat writer podcast collector so we don't duplicate the caption/Whisper
pipeline.
"""

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def parse_subtitle_file(path: Path) -> str:
    """Parse a VTT or SRT file to clean plaintext."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = []
    seen = set()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT"):
            continue
        if line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if re.match(r"^\d{2}:\d{2}", line):
            continue
        if re.match(r"^\d+$", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)

    return " ".join(lines)


def extract_captions_from_dir(video_dir: Path) -> Optional[str]:
    """Find and parse any VTT/SRT file in a directory (yt-dlp output dir)."""
    for ext in ("vtt", "srt"):
        for pattern in (f"*.en.{ext}", f"*.{ext}"):
            files = list(video_dir.glob(pattern))
            if files:
                return parse_subtitle_file(files[0])
    return None


_whisper_model_cache: dict[str, Any] = {}


def get_whisper_model(model_name: str):
    """Load and cache a Whisper model to avoid reloading per clip."""
    if model_name not in _whisper_model_cache:
        import whisper
        logger.info("Loading Whisper model '%s'...", model_name)
        _whisper_model_cache[model_name] = whisper.load_model(model_name)
    return _whisper_model_cache[model_name]


def transcribe_file(audio_path: Path, whisper_model: str = "small") -> Optional[str]:
    """Transcribe a local audio file with Whisper."""
    try:
        import whisper  # noqa: F401 — verify installed
    except ImportError:
        logger.error(
            "Whisper not installed. Install with: pip install openai-whisper"
        )
        return None

    try:
        logger.info(
            "Transcribing with Whisper (%s): %s", whisper_model, audio_path.name
        )
        model = get_whisper_model(whisper_model)
        result = model.transcribe(str(audio_path))
        return result.get("text", "")
    except Exception as e:
        logger.error("Whisper transcription failed: %s", e)
        return None


def download_audio_url(
    url: str,
    out_path: Path,
    timeout: int = 120,
    user_agent: str = "NFL-News-Agent/1.0",
) -> bool:
    """Stream an audio URL to disk (e.g., a podcast MP3 enclosure).

    Returns True on success, False on failure. Streams to avoid loading
    large podcast files into memory.
    """
    import requests

    try:
        with requests.get(
            url,
            stream=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        ) as resp:
            resp.raise_for_status()
            with out_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 15):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error("Failed to download audio from %s: %s", url, e)
        return False


def transcribe_audio_url(
    url: str,
    work_dir: Path,
    filename_stem: str,
    whisper_model: str = "small",
    delete_after: bool = True,
    user_agent: str = "NFL-News-Agent/1.0",
) -> Optional[str]:
    """Download + transcribe a podcast-style audio URL. Returns text or None."""
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_path = work_dir / f"{filename_stem}.mp3"

    if not download_audio_url(url, audio_path, user_agent=user_agent):
        return None

    text = transcribe_file(audio_path, whisper_model)

    if delete_after:
        audio_path.unlink(missing_ok=True)

    return text
