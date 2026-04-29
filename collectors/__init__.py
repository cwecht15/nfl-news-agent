"""NFL News Agent — Data Collectors"""

import os
import shutil
import sys
from pathlib import Path


def _ensure_ffmpeg_on_path() -> None:
    if shutil.which("ffmpeg"):
        return
    # ffmpeg ships in the conda env's Library/bin on Windows; run_daily.py
    # calls python.exe directly without activating the env, so that directory
    # isn't on PATH by default. Prepend it so yt-dlp's postprocessor and
    # Whisper's audio loader can find ffmpeg/ffprobe.
    candidate = Path(sys.prefix) / "Library" / "bin"
    if (candidate / "ffmpeg.exe").exists():
        os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")


_ensure_ffmpeg_on_path()
