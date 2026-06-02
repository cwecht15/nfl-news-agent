"""Collect NFL podcast episodes via RSS (transcript-tag first, show-notes fallback).

Mirrors the YouTube tool: runs locally, writes ``data/raw/<date>/podcast.json``
and ``data/transcripts/<date>/*.txt``, and dedups via ``data/podcast_seen.json``.
Consumed by the Podcast Report dashboard tab.

Per the chosen strategy each episode's text comes from the Podcasting 2.0
``<podcast:transcript>`` tag when the show publishes one (free, parsed by the
same VTT/SRT code the YouTube path uses); otherwise it falls back to the
episode's show notes. No audio download / Whisper, so this path is light
enough to run unattended (and even on CI, unlike yt-dlp).

Episodes are stored as :class:`models.Transcript` records — the same shape the
YouTube tool and summarizer already use — so the dashboard and LLM summarizer
work unchanged. ``video_id`` holds the episode GUID and ``channel_name`` holds
the show title.
"""

import html
import json
import logging
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import feedparser

from collectors._transcription import parse_subtitle_file
from config_loader import get_data_dir, get_podcast_feeds, get_settings
from models import Transcript

logger = logging.getLogger(__name__)

_USER_AGENT = "NFL-News-Agent/1.0 (+podcast collector)"

# Preference order when an episode publishes several transcript formats.
_TRANSCRIPT_TYPE_PRIORITY = {
    "text/vtt": 0,
    "application/x-subrip": 1,
    "application/srt": 1,
    "text/plain": 2,
    "application/json": 3,
    "text/html": 4,
}


# ──────────────────────────────────────────────────────────────────────
# Seen-episode dedup (parity with data/youtube_seen.json)
# ──────────────────────────────────────────────────────────────────────

def _seen_path() -> Path:
    return get_data_dir("") / "podcast_seen.json"


def _load_seen() -> set[str]:
    path = _seen_path()
    if not path.exists():
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f).get("seen_ids", []))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    with open(_seen_path(), "w", encoding="utf-8") as f:
        json.dump({"seen_ids": sorted(seen)}, f, indent=2, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _published(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _episode_guid(entry) -> str:
    return (
        str(entry.get("id", "") or "").strip()
        or str(entry.get("link", "") or "").strip()
        or str(_podcast_audio_url(entry) or "").strip()
    )


def _podcast_audio_url(entry) -> Optional[str]:
    for enc in getattr(entry, "enclosures", None) or entry.get("enclosures", []) or []:
        url = enc.get("href") or enc.get("url") or ""
        mime = str(enc.get("type", "") or "").lower()
        if url and ("audio" in mime or url.lower().endswith(
            (".mp3", ".m4a", ".aac", ".ogg", ".wav")
        )):
            return url
    return None


def _fetch_bytes(url: str, timeout: int = 25) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.debug("fetch failed %s: %s", url, e)
        return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _extract_transcripts_xml(raw: bytes) -> dict[str, list[tuple[str, str]]]:
    """Map episode GUID -> [(type, url)] from <podcast:transcript> elements.

    feedparser doesn't reliably surface the Podcasting 2.0 namespace, so we
    read the raw XML directly. Namespace-agnostic: matches any element whose
    local name is "transcript" / "guid".
    """
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        logger.debug("xml parse failed: %s", e)
        return out

    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        guid = ""
        transcripts: list[tuple[str, str]] = []
        for child in item:
            name = _local_name(child.tag)
            if name == "guid" and (child.text or "").strip():
                guid = child.text.strip()
            elif name == "transcript":
                url = child.attrib.get("url", "").strip()
                typ = child.attrib.get("type", "").strip().lower()
                if url:
                    transcripts.append((typ, url))
        if guid and transcripts:
            out[guid] = transcripts
    return out


def _pick_transcript(options: list[tuple[str, str]]) -> Optional[tuple[str, str]]:
    if not options:
        return None
    return sorted(
        options, key=lambda t: _TRANSCRIPT_TYPE_PRIORITY.get(t[0], 99)
    )[0]


def _transcript_text(
    typ: str, url: str, temp_dir: Path, slug: str
) -> Optional[str]:
    """Download a transcript document and return clean text, or None."""
    raw = _fetch_bytes(url)
    if not raw:
        return None
    try:
        content = raw.decode("utf-8", errors="replace")
    except Exception:
        return None

    low_url = url.lower()
    if typ == "application/json" or low_url.endswith(".json"):
        try:
            data = json.loads(content)
            segs = data.get("segments") if isinstance(data, dict) else None
            if isinstance(segs, list):
                return _WS_RE.sub(
                    " ", " ".join(s.get("body", "") for s in segs)
                ).strip()
        except Exception:
            return None
        return None

    if typ in ("text/vtt", "application/x-subrip", "application/srt") or \
            low_url.endswith((".vtt", ".srt")):
        ext = "vtt" if ("vtt" in typ or low_url.endswith(".vtt")) else "srt"
        tmp = temp_dir / f"{slug}.{ext}"
        try:
            tmp.write_text(content, encoding="utf-8")
            return parse_subtitle_file(tmp)
        except Exception:
            return None

    # text/plain or text/html
    return _strip_html(content)


# ──────────────────────────────────────────────────────────────────────
# Per-feed processing
# ──────────────────────────────────────────────────────────────────────

def _process_feed(
    cfg: dict,
    cutoff: datetime,
    max_per_feed: int,
    transcripts_dir: Path,
    temp_dir: Path,
    seen_snapshot: set[str],
) -> tuple[list[Transcript], set[str]]:
    feed_url = str(cfg.get("feed_url", "") or "").strip()
    name = str(cfg.get("name", "") or "").strip()
    team = str(cfg.get("team", "NFL") or "NFL").strip()
    if not feed_url:
        return [], set()

    raw = _fetch_bytes(feed_url)
    if not raw:
        logger.warning("Podcast feed unreachable: %s (%s)", name, feed_url)
        return [], set()

    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        logger.warning("Podcast feed error (%s): %s", name, parsed.bozo_exception)
        return [], set()

    show_title = str(getattr(parsed.feed, "title", "") or name).strip() or name
    transcript_map = _extract_transcripts_xml(raw)

    out: list[Transcript] = []
    new_seen: set[str] = set()
    kept = 0

    for entry in parsed.entries:
        if kept >= max_per_feed:
            break
        pub = _published(entry)
        if pub < cutoff:
            continue

        guid = _episode_guid(entry)
        if not guid or guid in seen_snapshot or guid in new_seen:
            continue
        title = str(entry.get("title", "") or "").strip()
        if not title:
            continue

        link = str(entry.get("link", "") or "").strip() or feed_url
        slug = re.sub(r"[^\w\s-]", "", title)[:60].strip().replace(" ", "_") or "episode"

        text = ""
        method = "shownotes"
        chosen = _pick_transcript(transcript_map.get(guid, []))
        if chosen:
            typ, turl = chosen
            tx = _transcript_text(typ, turl, temp_dir, f"{team}_{slug}")
            if tx and len(tx.strip()) > 50:
                text, method = tx, "transcript"

        if method == "shownotes":
            notes = _strip_html(
                str(entry.get("summary", "") or "")
                or (entry.get("content", [{}])[0].get("value", "")
                    if entry.get("content") else "")
            )
            text = notes

        new_seen.add(guid)
        if not text or len(text.strip()) < 50:
            continue

        txt_path = transcripts_dir / f"pod_{team}_{slug}.txt"
        try:
            txt_path.write_text(text, encoding="utf-8")
        except Exception:
            pass

        out.append(Transcript(
            video_id=guid,
            title=title,
            team=team,
            channel_name=show_title,
            published=pub,
            url=link,
            text=text,
            method=method,
            duration_seconds=0,
        ))
        kept += 1

    logger.info("%s [%s]: %d episode(s) (%s)", name, team, len(out), show_title)
    return out, new_seen


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────

def collect_podcasts(
    date_str: str,
    lookback_hours: Optional[int] = None,
) -> list[Transcript]:
    """Collect recent podcast episodes across all configured feeds.

    Feeds are fetched in parallel. Each episode's text is the published
    transcript when available, else the show notes. Dedup is by episode GUID
    via ``data/podcast_seen.json``.
    """
    settings = get_settings()
    feeds = [f for f in get_podcast_feeds() if str(f.get("feed_url", "")).strip()]
    collection = settings.get("collection", {})
    workers = int(collection.get("podcast_workers", collection.get("youtube_workers", 6)))
    max_per_feed = int(collection.get("podcast_max_per_feed", 8))
    effective_lookback = (
        lookback_hours if lookback_hours is not None
        else int(collection.get("podcast_lookback_hours", 72))
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=effective_lookback)

    transcripts_dir = get_data_dir("transcripts", date_str)
    temp_dir = get_data_dir("raw", date_str) / "podcast_temp"
    temp_dir.mkdir(exist_ok=True)

    seen = _load_seen()
    snapshot = frozenset(seen)

    logger.info(
        "Scanning %d podcast feeds (workers=%d, lookback=%dh)...",
        len(feeds), workers, effective_lookback,
    )

    all_eps: list[Transcript] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _process_feed, cfg, cutoff, max_per_feed,
                transcripts_dir, temp_dir, snapshot,
            ): cfg
            for cfg in feeds
        }
        for fut in as_completed(futures):
            cfg = futures[fut]
            try:
                eps, new_seen = fut.result()
            except Exception as e:
                logger.error("Feed %s failed: %s", cfg.get("name"), e)
                continue
            all_eps.extend(eps)
            seen.update(new_seen)

    _save_seen(seen)
    logger.info(
        "Collected %d podcast episodes (%d total seen).",
        len(all_eps), len(seen),
    )
    return all_eps


def save_podcast_results(episodes: list[Transcript], date_str: str) -> Path:
    """Merge new episodes into data/raw/<date>/podcast.json (union by GUID).

    MERGE, not overwrite: ``collect_podcasts`` only returns episodes not yet in
    ``podcast_seen.json``, so a second run on the same date would otherwise
    rewrite the file with just the handful of new episodes and drop everything
    collected earlier that day. Unioning with the existing file keeps same-day
    re-runs (e.g. a manual run + the CI cron) additive and loss-free.
    """
    out_dir = get_data_dir("raw", date_str)
    path = out_dir / "podcast.json"
    by_id: dict[str, dict] = {}
    if path.exists():
        try:
            for rec in json.loads(path.read_text(encoding="utf-8")):
                vid = rec.get("video_id")
                if vid:
                    by_id[vid] = rec
        except Exception:
            pass
    for ep in episodes:
        rec = ep.to_dict()
        if rec.get("video_id"):
            by_id[rec["video_id"]] = rec
    merged = list(by_id.values())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    logger.info(
        "Saved %d podcast records (%d new this run) to %s",
        len(merged), len(episodes), path,
    )
    return path
