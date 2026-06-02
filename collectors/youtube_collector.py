"""YouTube Press Conference Collector.

Scans all 32 NFL team YouTube channels for recent press conferences
using yt-dlp. Downloads captions or audio for transcription.

Team channels configured in config/teams.yaml.
Keywords configured in config/sources.yaml.
"""

import logging
import os
import re
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def _ydl_cookie_opts() -> dict:
    """Return yt-dlp options for cookie authentication if configured.

    YouTube blocks unauthenticated requests from datacenter IPs (e.g. CI
    runners). Setting YOUTUBE_COOKIES_PATH to a Netscape-format cookies
    file lets yt-dlp authenticate. On a residential IP this is unset and
    yt-dlp works without it.
    """
    path = os.environ.get("YOUTUBE_COOKIES_PATH")
    if path and os.path.isfile(path):
        return {"cookiefile": path}
    return {}

# Whisper / PyTorch inference is not safe to call from multiple threads
# concurrently. Keep team scans + caption downloads parallel, but serialize
# the actual transcription call.
_whisper_lock = threading.Lock()

sys.path.insert(0, str(Path(__file__).parent.parent))

from config_loader import (
    get_teams, get_settings, get_youtube_keywords, get_data_dir,
    get_national_youtube_channels,
)
from models import Transcript
from collectors._transcription import (
    extract_captions_from_dir,
    parse_subtitle_file,
    transcribe_file,
)

logger = logging.getLogger(__name__)

# Backwards-compatible re-exports so existing callers
# (dashboard/pages/transcripts.py, scripts/backfill_youtube.py) keep working.
_parse_subtitle_file = parse_subtitle_file


# Player-quote title pattern, e.g. "Ja'Kobi Lane: 'I Definitely Wanted...'".
# Catches player media availabilities whose titles carry no keyword. Requires
# a 2-3 word proper name, a colon, and a quote — rejects show-name titles
# like "The Lounge: Why We Love..." that have no quote after the colon.
_PLAYER_QUOTE_TITLE_RE = re.compile(
    r"^[A-Z][\w'.\-]*\s+[A-Z][\w'.\-]*(?:\s+[A-Z][\w'.\-]*)?:\s*['\"‘’“”]"
)


def _is_press_conference(title: str, keywords: list[str]) -> bool:
    """Check if a video title matches keywords or the player-quote pattern.

    Empty keyword list = match everything.
    """
    if not keywords:
        return True
    title_lower = title.lower()
    if any(kw.lower() in title_lower for kw in keywords):
        return True
    return bool(_PLAYER_QUOTE_TITLE_RE.match(title))


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
    """Fetch metadata for a single video (publish time + duration).

    Flat channel scans often omit upload timestamps. Resolving the actual
    watch URL gives us reliable publish metadata for recency filtering.

    Uses cookies (channel/metadata calls are what trip YouTube's "confirm
    you're not a bot" wall under high volume) but with `process=False` so
    yt-dlp does NOT run format selection. An authenticated session is served
    PO-token-gated formats; processing them raises "Requested format is not
    available" even though we only want metadata. process=False skips all of
    that and still returns timestamp / upload_date / duration.
    """
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        **_ydl_cookie_opts(),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False, process=False)

    if isinstance(info, dict):
        return info
    return {}


def _extract_captions_from_info(info: dict, video_dir: Path) -> Optional[str]:
    """Try to extract auto-generated captions from yt-dlp info dict.

    Returns the transcript text if captions were downloaded, else None.
    """
    return extract_captions_from_dir(video_dir)


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
        **_ydl_cookie_opts(),
    }

    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get("entries", []) if info else []
            # Real channel/uploader name from the channel page itself —
            # applied to every entry as a fallback. This is the actual
            # YouTube channel name (e.g. "Baltimore Collective"), not the
            # team display name we tag the transcript's team with.
            playlist_channel = (
                (info.get("channel") if info else "")
                or (info.get("uploader") if info else "")
                or ""
            )

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

                channel_name = (
                    entry.get("channel")
                    or entry.get("uploader")
                    or detailed_info.get("channel")
                    or detailed_info.get("uploader")
                    or playlist_channel
                    or ""
                )

                results.append({
                    "video_id": video_id,
                    "title": title,
                    "url": video_url,
                    "upload_date": upload_date,
                    "published_at": published_at.isoformat(),
                    "channel": channel_name,
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
    lookback_hours: Optional[int] = None,
) -> list[dict]:
    """Scan a team's YouTube channel(s) for recent matching videos.

    Supports multiple channels per team and scans both /videos and /streams
    tabs when configured. If `lookback_hours` is provided it overrides the
    value from settings — used for catch-up runs after missed days.

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
    effective_lookback = (
        lookback_hours
        if lookback_hours is not None
        else settings["collection"].get("lookback_hours", 28)
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=effective_lookback)

    matching = []
    seen_ids = set()

    for ch in channels:
        channel_id = ch.get("id", "")
        if not channel_id:
            continue

        # Per-channel keyword filter override. Dedicated press-conference
        # channels (e.g. @EaglesPressConferences) are 100% pressers — the
        # keyword filter only loses signal there. Set keyword_filter: false
        # on the channel entry to accept every video. Defaults to True.
        effective_keywords = (
            [] if ch.get("keyword_filter") is False else keywords
        )

        # Always scan /videos tab
        videos_url = f"https://www.youtube.com/channel/{channel_id}/videos"
        results = _scan_single_url(
            videos_url, effective_keywords, max_videos, cutoff
        )
        for v in results:
            if v["video_id"] not in seen_ids:
                seen_ids.add(v["video_id"])
                matching.append(v)

        # Most teams publish pressers as live streams that land on /streams,
        # not /videos — scan it by default. Set scan_streams: false on a
        # channel entry to opt out.
        if ch.get("scan_streams", True):
            streams_url = f"https://www.youtube.com/channel/{channel_id}/streams"
            results = _scan_single_url(
                streams_url, effective_keywords, max_videos, cutoff
            )
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
    Reuses an existing .vtt/.srt in `output_dir` if present so re-runs of
    an interrupted backfill (and the unattended auto-backfill) skip the
    network round-trip.
    """
    import yt_dlp

    for ext in ("vtt", "srt"):
        for cached in output_dir.glob(f"{video_id}*.{ext}"):
            if cached.stat().st_size > 0:
                return cached

    # NOTE: deliberately NO cookies here. Subtitle/audio fetches run format
    # selection, and an authenticated (cookie'd) session is served
    # PO-token-gated formats that yt-dlp can't resolve -> "Requested format
    # is not available". Anonymous requests get normal formats and succeed.
    # Cookies are only needed for the channel/metadata scan to dodge the
    # bot-detection wall; per-video downloads are low-volume and fine without.
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

    Returns path to the audio file, or None on failure. Reuses an existing
    audio file in `output_dir` if present (mostly relevant when
    `delete_audio_after` is False).
    """
    import yt_dlp

    for ext in ("mp3", "m4a", "wav", "webm", "opus"):
        for cached in output_dir.glob(f"{video_id}*.{ext}"):
            if cached.stat().st_size > 0:
                return cached

    # No cookies — see download_captions: authenticated sessions get
    # PO-token-gated formats that fail format selection. Anonymous works.
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


def _process_team(
    team: dict,
    keywords: list[str],
    settings: dict,
    date_str: str,
    lookback_hours: Optional[int],
    transcripts_dir: Path,
    temp_dir: Path,
    seen_snapshot: set[str],
    delay: float,
    enable_whisper: bool = True,
) -> tuple[list[Transcript], set[str]]:
    """Scan one team and return (transcripts, newly_seen_ids).

    Pure per-team work suitable for a thread pool. Each call only reads
    `seen_snapshot` and returns the set of video_ids it touched so the
    caller can merge them back into the persisted set.
    """
    transcripts: list[Transcript] = []
    new_seen: set[str] = set()

    videos = scan_channel(
        team, keywords, settings, date_str,
        lookback_hours=lookback_hours,
    )

    for video in videos:
        video_id = video["video_id"]
        if video_id in seen_snapshot or video_id in new_seen:
            continue

        video_url = video["url"]
        title = video["title"]

        caption_path = download_captions(
            video_id, video_url, temp_dir, settings
        )

        transcript_text = None
        method = "captions"

        if caption_path:
            transcript_text = _parse_subtitle_file(caption_path)
            logger.info("Got captions for: %s", title)
        elif not enable_whisper:
            logger.info(
                "No captions for '%s'; Whisper disabled, skipping.", title,
            )
        else:
            duration = int(video.get("duration") or 0)
            max_dur = int(
                settings.get("transcription", {})
                .get("whisper_max_duration_seconds", 1800)
            )
            if max_dur > 0 and duration > max_dur:
                logger.info(
                    "Skipping Whisper for '%s' (%dm%ds > %dm cap) — "
                    "would risk hanging the run; check captions manually later.",
                    title, duration // 60, duration % 60, max_dur // 60,
                )
            else:
                logger.info("No captions for '%s', trying Whisper...", title)
                audio_path = download_audio(
                    video_id, video_url, temp_dir, settings
                )
                if audio_path:
                    with _whisper_lock:
                        transcript_text = _transcribe_with_whisper(
                            audio_path, settings
                        )
                    method = "whisper"

                    if settings.get("transcription", {}).get(
                        "delete_audio_after", True
                    ):
                        audio_path.unlink(missing_ok=True)

        if transcript_text and len(transcript_text.strip()) > 50:
            slug = re.sub(r"[^\w\s-]", "", title)[:60].strip().replace(" ", "_")
            txt_path = transcripts_dir / f"{team['abbr']}_{slug}.txt"
            txt_path.write_text(transcript_text, encoding="utf-8")

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

            transcripts.append(Transcript(
                video_id=video_id,
                title=title,
                team=team["abbr"],
                # Actual YouTube channel name when the scan captured it,
                # falling back to the team display name. Lets beat/analysis
                # channels under a team show their own name instead of the
                # team's.
                channel_name=(video.get("channel") or team["name"]),
                published=pub,
                url=video_url,
                text=transcript_text,
                method=method,
                duration_seconds=video.get("duration", 0) or 0,
            ))

        new_seen.add(video_id)
        time.sleep(delay)

    return transcripts, new_seen


def collect_youtube(
    date_str: str,
    lookback_hours: Optional[int] = None,
    enable_whisper: bool = True,
) -> list[Transcript]:
    """Collect press conference transcripts from all team YouTube channels.

    Teams are scanned in parallel (each is an independent YouTube channel).
    Within a team, videos are processed serially with the configured polite
    delay. Whisper transcription is serialized via a module-level lock
    because PyTorch inference is not thread-safe.

    Set `enable_whisper=False` for unattended runs that should finish in
    minutes regardless of how many videos lack auto-captions. Captions-only
    runs cover the vast majority of NFL team uploads.
    """
    settings = get_settings()
    teams = get_teams()
    keywords = get_youtube_keywords()
    delay = settings["collection"].get("request_delay", 2.0)
    workers = int(settings["collection"].get("youtube_workers", 6))

    # National / league-wide channels have no single team. Wrap each one in a
    # synthetic "team" dict so the existing per-team scan path handles it: all
    # are tagged abbr="NFL" (they share a league-wide group in the YouTube
    # Report) but each keeps its real channel name as the transcript's
    # channel_name.
    national_targets = [
        {
            "abbr": "NFL",
            "name": ch.get("name") or ch.get("handle", "NFL"),
            "youtube_channels": [ch],
        }
        for ch in get_national_youtube_channels()
    ]
    scan_targets = list(teams) + national_targets

    transcripts_dir = get_data_dir("transcripts", date_str)
    temp_dir = get_data_dir("raw", date_str) / "youtube_temp"
    temp_dir.mkdir(exist_ok=True)

    seen_ids = _load_seen_videos()
    all_transcripts: list[Transcript] = []

    logger.info(
        "Scanning %d team + %d national YouTube channels (workers=%d)...",
        len(teams), len(national_targets), workers,
    )

    # Snapshot seen_ids at start; workers only read it. Each worker returns
    # its own set of touched ids, merged after all teams finish.
    snapshot = frozenset(seen_ids)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _process_team, team, keywords, settings, date_str,
                lookback_hours, transcripts_dir, temp_dir, snapshot, delay,
                enable_whisper,
            ): team
            for team in scan_targets
        }
        for fut in as_completed(futures):
            team = futures[fut]
            try:
                team_transcripts, new_seen = fut.result()
            except Exception as e:
                logger.error("Team %s failed: %s", team["abbr"], e)
                continue
            all_transcripts.extend(team_transcripts)
            seen_ids.update(new_seen)

    _save_seen_videos(seen_ids)
    logger.info(
        "Collected %d transcripts total (%d total seen).",
        len(all_transcripts), len(seen_ids),
    )
    return all_transcripts


def _transcribe_with_whisper(audio_path: Path, settings: dict) -> Optional[str]:
    """Transcribe audio file using OpenAI Whisper."""
    model_name = settings.get("transcription", {}).get("whisper_model", "small")
    return transcribe_file(audio_path, whisper_model=model_name)


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
