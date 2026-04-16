"""YouTube Press Conference Collector.

Scans all 32 NFL team YouTube channels for recent press conferences
using yt-dlp. Downloads captions or audio for transcription.

Team channels configured in config/teams.yaml.
Keywords configured in config/sources.yaml.
"""

import logging
import re
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config_loader import (
    get_teams, get_settings, get_youtube_keywords, get_data_dir,
)
from models import Transcript

logger = logging.getLogger(__name__)


def _is_press_conference(title: str, keywords: list[str]) -> bool:
    """Check if a video title matches press conference keywords."""
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in keywords)


def _extract_published_datetime(info: dict) -> Optional[datetime]:
    """Extract the best available publish timestamp from yt-dlp metadata."""
    for key in ("release_timestamp", "timestamp"):
        value = info.get(key)
        if not value:
            continue
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue

    for key in ("upload_date", "release_date"):
        value = str(info.get(key, "") or "").strip()
        if not value:
            continue
        try:
            return datetime.strptime(value, "%Y%m%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

    return None


def _fetch_video_details(video_url: str) -> dict:
    """Fetch full metadata for a single video.

    Flat channel scans often omit upload timestamps. Resolving the actual
    watch URL gives us reliable publish metadata for recency filtering.
    """
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)

    if isinstance(info, dict):
        return info
    return {}


def _extract_captions_from_info(info: dict, video_dir: Path) -> Optional[str]:
    """Try to extract auto-generated captions from yt-dlp info dict.

    Returns the transcript text if captions were downloaded, else None.
    """
    # Check for subtitle files that yt-dlp may have written
    for ext in ("vtt", "srt"):
        for pattern in (f"*.en.{ext}", f"*.{ext}"):
            files = list(video_dir.glob(pattern))
            if files:
                return _parse_subtitle_file(files[0])

    return None


def _parse_subtitle_file(path: Path) -> str:
    """Parse a VTT or SRT file to clean plaintext."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = []
    seen = set()

    for line in text.splitlines():
        line = line.strip()
        # Skip VTT header, timestamps, position tags
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
        # Remove HTML/VTT formatting tags
        line = re.sub(r"<[^>]+>", "", line)
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)

    return " ".join(lines)


def _scan_single_url(
    url: str,
    keywords: list[str],
    max_videos: int,
    cutoff: datetime,
) -> list[dict]:
    """Scan a single YouTube URL (videos or streams tab) for matching content."""
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": max_videos,
    }

    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get("entries", []) if info else []

            for entry in entries:
                if not entry:
                    continue
                title = entry.get("title", "")
                video_id = entry.get("id", "")
                if not video_id:
                    continue

                if not _is_press_conference(title, keywords):
                    continue

                video_url = f"https://www.youtube.com/watch?v={video_id}"
                published_at = _extract_published_datetime(entry)
                detailed_info: dict = {}

                if published_at is None or not entry.get("duration"):
                    try:
                        detailed_info = _fetch_video_details(video_url)
                    except Exception as e:
                        logger.debug(
                            "Failed to fetch full metadata for %s: %s",
                            video_url,
                            e,
                        )
                        detailed_info = {}

                if not title:
                    title = detailed_info.get("title", "")

                if not title or not _is_press_conference(title, keywords):
                    continue

                if published_at is None:
                    published_at = _extract_published_datetime(detailed_info)

                if published_at is None:
                    logger.debug(
                        "Skipping %s because YouTube did not provide a publish date.",
                        video_url,
                    )
                    continue

                if published_at < cutoff:
                    continue

                upload_date = (
                    str(detailed_info.get("upload_date", "") or "").strip()
                    or str(entry.get("upload_date", "") or "").strip()
                    or published_at.strftime("%Y%m%d")
                )

                results.append({
                    "video_id": video_id,
                    "title": title,
                    "url": video_url,
                    "upload_date": upload_date,
                    "published_at": published_at.isoformat(),
                    "duration": (
                        entry.get("duration")
                        or detailed_info.get("duration", 0)
                        or 0
                    ),
                })
    except Exception as e:
        logger.debug("Failed to scan %s: %s", url, e)

    return results


def scan_channel(
    team: dict,
    keywords: list[str],
    settings: dict,
    date_str: str,
) -> list[dict]:
    """Scan a team's YouTube channel(s) for recent matching videos.

    Supports multiple channels per team and scans both /videos and /streams
    tabs when configured.

    Returns list of video info dicts for matching videos.
    """
    # Support both old format (youtube_channel_id) and new (youtube_channels list)
    channels = team.get("youtube_channels", [])
    if not channels:
        old_id = team.get("youtube_channel_id", "")
        if old_id:
            channels = [{"id": old_id}]

    if not channels:
        logger.warning("No YouTube channels for %s", team["abbr"])
        return []

    max_videos = settings["collection"].get("youtube_max_per_channel", 5)
    lookback_hours = settings["collection"].get("lookback_hours", 28)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    matching = []
    seen_ids = set()

    for ch in channels:
        channel_id = ch.get("id", "")
        if not channel_id:
            continue

        # Always scan /videos tab
        videos_url = f"https://www.youtube.com/channel/{channel_id}/videos"
        results = _scan_single_url(videos_url, keywords, max_videos, cutoff)
        for v in results:
            if v["video_id"] not in seen_ids:
                seen_ids.add(v["video_id"])
                matching.append(v)

        # Scan /streams tab if configured (teams that post pressers as live streams)
        if ch.get("scan_streams", False):
            streams_url = f"https://www.youtube.com/channel/{channel_id}/streams"
            results = _scan_single_url(streams_url, keywords, max_videos, cutoff)
            for v in results:
                if v["video_id"] not in seen_ids:
                    seen_ids.add(v["video_id"])
                    matching.append(v)

    logger.info(
        "%s: found %d matching videos", team["abbr"], len(matching)
    )

    return matching


def download_captions(
    video_id: str,
    video_url: str,
    output_dir: Path,
    settings: dict,
) -> Optional[Path]:
    """Download captions (auto-generated) for a video.

    Returns path to the subtitle file, or None if no captions available.
    """
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "outtmpl": str(output_dir / f"{video_id}.%(ext)s"),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        # Find the downloaded caption file
        for ext in ("vtt", "srt"):
            files = list(output_dir.glob(f"{video_id}*.{ext}"))
            if files:
                return files[0]

    except Exception as e:
        logger.debug("No captions for %s: %s", video_id, e)

    return None


def download_audio(
    video_id: str,
    video_url: str,
    output_dir: Path,
    settings: dict,
) -> Optional[Path]:
    """Download audio only for Whisper transcription.

    Returns path to the audio file, or None on failure.
    """
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "worstaudio/worst",  # Smallest audio for transcription
        "outtmpl": str(output_dir / f"{video_id}.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        # Find the audio file
        for ext in ("mp3", "m4a", "wav", "webm", "opus"):
            files = list(output_dir.glob(f"{video_id}*.{ext}"))
            if files:
                return files[0]

    except Exception as e:
        logger.error("Failed to download audio for %s: %s", video_id, e)

    return None


def _load_seen_videos() -> set[str]:
    """Load the set of previously processed video IDs."""
    seen_path = Path(__file__).parent.parent / "data" / "youtube_seen.json"
    if not seen_path.exists():
        return set()
    try:
        with open(seen_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_ids", []))
    except (json.JSONDecodeError, KeyError):
        return set()


def _save_seen_videos(seen_ids: set[str]):
    """Persist the set of processed video IDs."""
    seen_path = Path(__file__).parent.parent / "data" / "youtube_seen.json"
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    with open(seen_path, "w", encoding="utf-8") as f:
        json.dump({"seen_ids": sorted(seen_ids)}, f, indent=2)


def collect_youtube(date_str: str) -> list[Transcript]:
    """Collect press conference transcripts from all team YouTube channels.

    1. Scan each channel for recent press conferences
    2. Skip videos already processed (tracked in youtube_seen.json)
    3. Download captions (preferred) or audio (fallback)
    4. Transcribe audio with Whisper if needed
    5. Return Transcript objects

    Args:
        date_str: Today's date string (YYYY-MM-DD) for file organization.

    Returns:
        List of Transcript objects.
    """
    settings = get_settings()
    teams = get_teams()
    keywords = get_youtube_keywords()
    delay = settings["collection"].get("request_delay", 2.0)

    transcripts_dir = get_data_dir("transcripts", date_str)
    temp_dir = get_data_dir("raw", date_str) / "youtube_temp"
    temp_dir.mkdir(exist_ok=True)

    seen_ids = _load_seen_videos()
    all_transcripts: list[Transcript] = []

    logger.info("Scanning %d team YouTube channels...", len(teams))

    for team in teams:
        videos = scan_channel(team, keywords, settings, date_str)

        for video in videos:
            video_id = video["video_id"]

            if video_id in seen_ids:
                logger.debug("Skipping already-processed video: %s", video_id)
                continue
            video_url = video["url"]
            title = video["title"]

            # Try captions first
            caption_path = download_captions(
                video_id, video_url, temp_dir, settings
            )

            transcript_text = None
            method = "captions"

            if caption_path:
                transcript_text = _parse_subtitle_file(caption_path)
                logger.info("Got captions for: %s", title)
            else:
                # Fallback: download audio and transcribe with Whisper
                logger.info("No captions for '%s', trying Whisper...", title)
                audio_path = download_audio(
                    video_id, video_url, temp_dir, settings
                )
                if audio_path:
                    transcript_text = _transcribe_with_whisper(
                        audio_path, settings
                    )
                    method = "whisper"

                    # Clean up audio file if configured
                    if settings.get("transcription", {}).get(
                        "delete_audio_after", True
                    ):
                        audio_path.unlink(missing_ok=True)

            if transcript_text and len(transcript_text.strip()) > 50:
                # Save transcript file
                slug = re.sub(r"[^\w\s-]", "", title)[:60].strip().replace(" ", "_")
                txt_path = transcripts_dir / f"{team['abbr']}_{slug}.txt"
                txt_path.write_text(transcript_text, encoding="utf-8")

                # Parse upload date
                published_at = str(video.get("published_at", "") or "").strip()
                upload_date = str(video.get("upload_date", "") or "").strip()
                if published_at:
                    try:
                        pub = datetime.fromisoformat(published_at)
                    except ValueError:
                        pub = datetime.now(timezone.utc)
                elif upload_date:
                    try:
                        pub = datetime.strptime(upload_date, "%Y%m%d")
                        pub = pub.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pub = datetime.now(timezone.utc)
                else:
                    pub = datetime.now(timezone.utc)

                all_transcripts.append(Transcript(
                    video_id=video_id,
                    title=title,
                    team=team["abbr"],
                    channel_name=team["name"],
                    published=pub,
                    url=video_url,
                    text=transcript_text,
                    method=method,
                    duration_seconds=video.get("duration", 0) or 0,
                ))

            seen_ids.add(video_id)
            time.sleep(delay)

        time.sleep(delay)

    _save_seen_videos(seen_ids)
    logger.info("Collected %d transcripts total (%d total seen).", len(all_transcripts), len(seen_ids))
    return all_transcripts


_whisper_model_cache: dict[str, Any] = {}


def _get_whisper_model(model_name: str):
    """Load and cache a Whisper model to avoid reloading per video."""
    if model_name not in _whisper_model_cache:
        import whisper
        logger.info("Loading Whisper model '%s'...", model_name)
        _whisper_model_cache[model_name] = whisper.load_model(model_name)
    return _whisper_model_cache[model_name]


def _transcribe_with_whisper(audio_path: Path, settings: dict) -> Optional[str]:
    """Transcribe audio file using OpenAI Whisper."""
    try:
        import whisper  # noqa: F401 — verify installed
    except ImportError:
        logger.error(
            "Whisper not installed. Install with: pip install openai-whisper"
        )
        return None

    model_name = settings.get("transcription", {}).get("whisper_model", "small")

    try:
        logger.info("Transcribing with Whisper (%s): %s", model_name, audio_path.name)
        model = _get_whisper_model(model_name)
        result = model.transcribe(str(audio_path))
        return result.get("text", "")
    except Exception as e:
        logger.error("Whisper transcription failed: %s", e)
        return None


def save_youtube_results(transcripts: list[Transcript], date_str: str):
    """Save transcript metadata to a JSON file."""
    out_dir = get_data_dir("raw", date_str)
    path = out_dir / "youtube.json"
    data = [t.to_dict() for t in transcripts]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d transcript records to %s", len(transcripts), path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    today = datetime.now().strftime("%Y-%m-%d")
    results = collect_youtube(today)
    for t in results:
        print(f"[{t.team}] {t.title} ({t.method})")
        print(f"  {t.url}")
        print(f"  Text length: {len(t.text)} chars")
        print()
    print(f"Total: {len(results)} transcripts")
