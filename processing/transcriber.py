"""Transcription utilities.

Handles VTT/SRT caption parsing and Whisper fallback transcription.
Used by youtube_collector.py and can be run standalone on audio files.
"""

import logging
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config_loader import get_settings

logger = logging.getLogger(__name__)


def parse_vtt(path: Path) -> str:
    """Parse a WebVTT file to clean plaintext."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = []
    seen = set()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if re.match(r"^\d{2}:\d{2}", line):
            continue
        if re.match(r"^\d+$", line):
            continue
        # Strip formatting tags
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\{[^}]+\}", "", line)  # SSA/ASS tags
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)

    return " ".join(lines)


def parse_srt(path: Path) -> str:
    """Parse an SRT file to clean plaintext."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = []
    seen = set()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\d+$", line):
            continue
        if re.match(r"\d{2}:\d{2}:\d{2}", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)

    return " ".join(lines)


def parse_subtitle_file(path: Path) -> str:
    """Auto-detect and parse a subtitle file (VTT or SRT)."""
    suffix = path.suffix.lower()
    if suffix == ".vtt":
        return parse_vtt(path)
    elif suffix == ".srt":
        return parse_srt(path)
    else:
        logger.warning("Unknown subtitle format: %s", suffix)
        return parse_vtt(path)  # Try VTT parser as fallback


def transcribe_audio(audio_path: Path, model_name: Optional[str] = None) -> str:
    """Transcribe an audio file using OpenAI Whisper.

    Args:
        audio_path: Path to audio file (mp3, m4a, wav, etc.)
        model_name: Whisper model size. Defaults to settings.yaml value.

    Returns:
        Transcribed text string.
    """
    try:
        import whisper
    except ImportError:
        raise ImportError(
            "Whisper not installed. Run: pip install openai-whisper"
        )

    if model_name is None:
        settings = get_settings()
        model_name = settings.get("transcription", {}).get(
            "whisper_model", "small"
        )

    logger.info("Loading Whisper model '%s'...", model_name)
    model = whisper.load_model(model_name)

    logger.info("Transcribing: %s", audio_path.name)
    result = model.transcribe(str(audio_path))
    text = result.get("text", "")
    logger.info("Transcription complete: %d characters", len(text))
    return text


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python transcriber.py <file_path>")
        print("  Supports: .vtt, .srt (captions) or audio files (via Whisper)")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    if path.suffix.lower() in (".vtt", ".srt"):
        text = parse_subtitle_file(path)
    else:
        text = transcribe_audio(path)

    print(text)
