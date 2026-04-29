"""Beat Writer Collector.

Reads `beat_writers` from config/sources.yaml and collects:
  - Articles from author/byline RSS feeds
  - YouTube videos from writer channels (all recent, or keyword-filtered)
  - Podcast episodes (metadata only, or Whisper-transcribed audio)

Every output NewsItem carries:
  - source        = "<writer name> (<outlet>)"
  - source_type   = "beat_writer"
  - author        = writer name
  - teams         = writer's declared teams, merged with any detected teams

See `config/sources.yaml` for the writer schema.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import feedparser

sys.path.insert(0, str(Path(__file__).parent.parent))

from config_loader import (
    get_beat_writers,
    get_data_dir,
    get_settings,
    get_teams_by_abbr,
)
from models import NewsItem, Transcript
from collectors._transcription import transcribe_audio_url
from collectors.rss_collector import _detect_teams, _parse_published
from collectors.youtube_collector import (
    _extract_published_datetime,
    _parse_subtitle_file,
    _scan_single_url,
    _transcribe_with_whisper,
    _load_seen_videos,
    _save_seen_videos,
    download_audio,
    download_captions,
)

logger = logging.getLogger(__name__)


def collect_beat_writers(
    date_str: str,
    lookback_hours: Optional[int] = None,
    teams_by_abbr: Optional[dict] = None,
) -> tuple[list[NewsItem], list[Transcript]]:
    """Collect from all configured beat writers. Returns (news_items, transcripts)."""
    settings = get_settings()
    if lookback_hours is None:
        lookback_hours = settings["collection"].get("lookback_hours", 28)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    delay = settings["collection"].get("request_delay", 2.0)

    if teams_by_abbr is None:
        teams_by_abbr = get_teams_by_abbr()

    writers = get_beat_writers() or []
    if not writers:
        logger.info("No beat writers configured.")
        return [], []

    news_items: list[NewsItem] = []
    transcripts: list[Transcript] = []
    seen_video_ids = _load_seen_videos()

    logger.info(
        "Collecting from %d beat writers (lookback=%dh)...",
        len(writers), lookback_hours,
    )

    for writer in writers:
        name = str(writer.get("name", "") or "").strip()
        if not name:
            continue
        default_teams = [
            str(t).strip().upper() for t in (writer.get("teams") or []) if t
        ]

        for feed_url in writer.get("rss_feeds") or []:
            try:
                items = _collect_writer_rss(
                    feed_url, writer, cutoff, teams_by_abbr, default_teams
                )
                news_items.extend(items)
                logger.info(
                    "%s RSS %s: %d items", name, feed_url, len(items),
                )
            except Exception as e:
                logger.error(
                    "Beat writer RSS failed (%s / %s): %s", name, feed_url, e,
                )
            time.sleep(delay)

        for feed_cfg in writer.get("custom_feeds") or []:
            try:
                items = _collect_writer_custom_feed(
                    feed_cfg, writer, cutoff, teams_by_abbr, default_teams
                )
                news_items.extend(items)
                logger.info(
                    "%s custom(%s): %d items",
                    name, feed_cfg.get("type", "?"), len(items),
                )
            except Exception as e:
                logger.error(
                    "Beat writer custom feed failed (%s / %s): %s",
                    name, feed_cfg.get("type", "?"), e,
                )
            time.sleep(delay)

        for channel in writer.get("youtube_channels") or []:
            try:
                yt_items, yt_tx = _collect_writer_youtube(
                    channel, writer, cutoff, settings, date_str,
                    default_teams, seen_video_ids, teams_by_abbr,
                )
                news_items.extend(yt_items)
                transcripts.extend(yt_tx)
                logger.info(
                    "%s YouTube %s: %d news, %d transcripts",
                    name, channel.get("handle") or channel.get("id"),
                    len(yt_items), len(yt_tx),
                )
            except Exception as e:
                logger.error("Beat writer YouTube failed (%s): %s", name, e)
            time.sleep(delay)

        for podcast_cfg in writer.get("podcast_feeds") or []:
            try:
                pod_items = _collect_writer_podcasts(
                    podcast_cfg, writer, cutoff, settings, date_str,
                    teams_by_abbr, default_teams,
                )
                news_items.extend(pod_items)
                logger.info(
                    "%s Podcast %s: %d items",
                    name, podcast_cfg.get("url"), len(pod_items),
                )
            except Exception as e:
                logger.error("Beat writer podcast failed (%s): %s", name, e)
            time.sleep(delay)

    _save_seen_videos(seen_video_ids)

    logger.info(
        "Beat writers complete: %d news items, %d transcripts across %d writers",
        len(news_items), len(transcripts), len(writers),
    )
    return news_items, transcripts


def _writer_source_label(writer: dict) -> str:
    name = str(writer.get("name", "") or "").strip() or "Beat Writer"
    outlet = str(writer.get("outlet", "") or "").strip()
    return f"{name} ({outlet})" if outlet else name


def _merge_teams(default_teams: list[str], detected: list[str]) -> list[str]:
    return list(dict.fromkeys(default_teams + detected))


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


# ─── RSS ─────────────────────────────────────────────────────────────

def _collect_writer_rss(
    feed_url: str,
    writer: dict,
    cutoff: datetime,
    teams_by_abbr: dict,
    default_teams: list[str],
) -> list[NewsItem]:
    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        logger.warning(
            "Beat writer feed error (%s / %s): %s",
            writer.get("name"), feed_url, parsed.bozo_exception,
        )
        return []

    items: list[NewsItem] = []
    source_label = _writer_source_label(writer)
    writer_name = str(writer.get("name", "") or "").strip()

    for entry in parsed.entries:
        pub_date = _parse_published(entry)
        if pub_date is None:
            pub_date = datetime.now(timezone.utc)
        if pub_date < cutoff:
            continue

        title = str(entry.get("title", "") or "").strip()
        link = str(entry.get("link", "") or "").strip()
        summary = _strip_html(str(entry.get("summary", "") or ""))
        if not title or not link:
            continue

        detected = _detect_teams(f"{title} {summary}", teams_by_abbr)
        teams = _merge_teams(default_teams, detected)

        items.append(NewsItem(
            title=title,
            url=link,
            source=source_label,
            source_type="beat_writer",
            published=pub_date,
            summary=summary,
            teams=teams,
            author=writer_name,
            category="news",
        ))

    return items


# ─── Custom JSON feeds ───────────────────────────────────────────────

def _collect_writer_custom_feed(
    feed_cfg: dict,
    writer: dict,
    cutoff: datetime,
    teams_by_abbr: dict,
    default_teams: list[str],
) -> list[NewsItem]:
    """Dispatch to a per-site custom JSON feed parser based on `type`."""
    feed_type = str(feed_cfg.get("type", "") or "").strip().lower()
    if feed_type == "neworleans_football":
        return _collect_nof_feed(feed_cfg, writer, cutoff, teams_by_abbr, default_teams)
    logger.warning(
        "Unknown custom feed type: %s (writer: %s)",
        feed_type, writer.get("name"),
    )
    return []


def _parse_iso8601(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _collect_nof_feed(
    feed_cfg: dict,
    writer: dict,
    cutoff: datetime,
    teams_by_abbr: dict,
    default_teams: list[str],
) -> list[NewsItem]:
    """Parse neworleans.football's custom JSON feed at /api/feed.

    Emits NewsItems for both long-form articles and short quickPosts.
    """
    import requests

    settings = get_settings()
    url = str(feed_cfg.get("url", "") or "").strip() or "https://neworleans.football/api/feed"
    user_agent = settings["collection"].get("user_agent", "NFL-News-Agent/1.0")
    timeout = settings["collection"].get("request_timeout", 30)

    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("Failed to fetch NOF feed %s: %s", url, e)
        return []

    items: list[NewsItem] = []
    source_label = _writer_source_label(writer)
    writer_name = str(writer.get("name", "") or "").strip()

    # Optional allowlist of author names (case-insensitive). Empty = all authors.
    author_filter = {
        str(a).strip().lower()
        for a in (feed_cfg.get("authors") or [])
        if str(a).strip()
    }
    include_quickposts = bool(feed_cfg.get("quickposts", True))

    def _passes_author(name: str) -> bool:
        if not author_filter:
            return True
        return name.strip().lower() in author_filter

    for art in data.get("articles", []) or []:
        pub = _parse_iso8601(str(art.get("publishedAt", "") or ""))
        if pub is None:
            pub = datetime.now(timezone.utc)
        if pub < cutoff:
            continue

        title = str(art.get("title", "") or "").strip()
        slug = ((art.get("slug") or {}).get("current") or "").strip()
        if not title or not slug:
            continue

        author = (art.get("author") or {}).get("name") or writer_name
        if not _passes_author(author):
            continue

        summary = str(art.get("excerpt", "") or "").strip()
        detected = _detect_teams(f"{title} {summary}", teams_by_abbr)
        teams = _merge_teams(default_teams, detected)

        premium = bool(art.get("premium", False))
        source_str = f"{source_label} [Premium]" if premium else source_label

        items.append(NewsItem(
            title=title,
            url=f"https://neworleans.football/article/{slug}",
            source=source_str,
            source_type="beat_writer",
            published=pub,
            summary=summary,
            teams=teams,
            author=author,
            category="news",
        ))

    if include_quickposts:
        for qp in data.get("quickPosts", []) or []:
            pub = _parse_iso8601(str(qp.get("publishedAt", "") or ""))
            if pub is None:
                pub = datetime.now(timezone.utc)
            if pub < cutoff:
                continue

            headline = str(qp.get("headline", "") or "").strip()
            text = str(qp.get("text", "") or "").strip()
            qp_id = str(qp.get("_id", "") or "").strip()
            if not qp_id:
                continue

            author = str(qp.get("author", "") or "").strip() or writer_name
            if not _passes_author(author):
                continue

            title = headline or (text[:90] + "…" if len(text) > 90 else text) or "Quick Post"
            detected = _detect_teams(f"{title} {text}", teams_by_abbr)
            teams = _merge_teams(default_teams, detected)

            items.append(NewsItem(
                title=title,
                url=f"https://neworleans.football/posts/{qp_id}",
                source=f"{source_label} [Quick Post]",
                source_type="beat_writer",
                published=pub,
                summary=text[:1000],
                full_text=text,
                teams=teams,
                author=author,
                category="news",
            ))

    return items


# ─── YouTube ─────────────────────────────────────────────────────────

def _collect_writer_youtube(
    channel: dict,
    writer: dict,
    cutoff: datetime,
    settings: dict,
    date_str: str,
    default_teams: list[str],
    seen_video_ids: set[str],
    teams_by_abbr: dict,
) -> tuple[list[NewsItem], list[Transcript]]:
    channel_id = str(channel.get("id", "") or "").strip()
    if not channel_id:
        return [], []

    use_keyword_filter = bool(channel.get("keyword_filter", False))
    transcribe_enabled = bool(channel.get("transcribe", True))
    max_videos = settings["collection"].get("youtube_max_per_channel", 5)

    # Drop YouTube Shorts and brief clips (YT Shorts max is currently 180s).
    min_duration = int(channel.get("min_duration_seconds", 180))

    # Require the writer's team to be named in the title. Default: on when the
    # writer has declared teams, off otherwise (national-insider channels).
    team_focus = channel.get("team_focus")
    if team_focus is None:
        team_focus = bool(default_teams)

    keywords: list[str] = []
    if use_keyword_filter:
        from config_loader import get_youtube_keywords
        keywords = get_youtube_keywords()

    videos_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    videos = _scan_single_url(videos_url, keywords, max_videos, cutoff)

    writer_name = str(writer.get("name", "") or "").strip()
    outlet = str(writer.get("outlet", "") or "").strip()
    source_label = _writer_source_label(writer)
    channel_name = str(channel.get("handle") or writer_name or "YouTube").strip()

    transcripts_dir = get_data_dir("transcripts", date_str)
    temp_dir = get_data_dir("raw", date_str) / "youtube_temp"
    temp_dir.mkdir(exist_ok=True)

    news_items: list[NewsItem] = []
    transcripts: list[Transcript] = []
    skipped_short = 0
    skipped_off_team = 0

    for video in videos:
        video_id = video["video_id"]
        if video_id in seen_video_ids:
            continue

        video_url = video["url"]
        title = video["title"]

        duration = int(video.get("duration") or 0)
        if min_duration > 0 and 0 < duration < min_duration:
            logger.info(
                "%s: skipping short (%ds): %s",
                writer_name, duration, title[:70],
            )
            skipped_short += 1
            continue

        if team_focus and default_teams:
            detected = _detect_teams(title, teams_by_abbr)
            if not set(default_teams) & set(detected):
                logger.info(
                    "%s: skipping off-team video: %s",
                    writer_name, title[:70],
                )
                skipped_off_team += 1
                continue

        published_iso = video.get("published_at", "")
        try:
            pub = datetime.fromisoformat(published_iso) if published_iso else datetime.now(timezone.utc)
        except ValueError:
            pub = datetime.now(timezone.utc)

        transcript_text: Optional[str] = None
        method = "captions"

        if transcribe_enabled:
            caption_path = download_captions(video_id, video_url, temp_dir, settings)
            if caption_path:
                transcript_text = _parse_subtitle_file(caption_path)
            else:
                audio_path = download_audio(video_id, video_url, temp_dir, settings)
                if audio_path:
                    transcript_text = _transcribe_with_whisper(audio_path, settings)
                    method = "whisper"
                    if settings.get("transcription", {}).get(
                        "delete_audio_after", True
                    ):
                        audio_path.unlink(missing_ok=True)

        # Emit Transcript if we have substantive transcribed text
        if transcript_text and len(transcript_text.strip()) > 50:
            slug = re.sub(r"[^\w\s-]", "", title)[:60].strip().replace(" ", "_")
            team_tag = default_teams[0] if default_teams else "NAT"
            txt_path = transcripts_dir / f"BW_{team_tag}_{slug}.txt"
            txt_path.write_text(transcript_text, encoding="utf-8")

            transcripts.append(Transcript(
                video_id=video_id,
                title=title,
                team=team_tag,
                channel_name=channel_name or writer_name,
                published=pub,
                url=video_url,
                text=transcript_text,
                method=method,
                duration_seconds=video.get("duration", 0) or 0,
            ))

        # Always also emit a NewsItem so the video shows up in normal sections
        news_summary = (
            (transcript_text[:1000] + "…") if transcript_text and len(transcript_text) > 1000
            else (transcript_text or "")
        )
        news_items.append(NewsItem(
            title=title,
            url=video_url,
            source=f"{source_label} [YouTube]",
            source_type="beat_writer",
            published=pub,
            summary=news_summary,
            full_text=transcript_text or "",
            teams=default_teams,
            author=writer_name,
            category="news",
        ))

        seen_video_ids.add(video_id)

    if skipped_short or skipped_off_team:
        logger.info(
            "%s YouTube filters: skipped %d short, %d off-team",
            writer_name, skipped_short, skipped_off_team,
        )

    return news_items, transcripts


# ─── Podcasts ────────────────────────────────────────────────────────

def _collect_writer_podcasts(
    podcast_cfg: dict,
    writer: dict,
    cutoff: datetime,
    settings: dict,
    date_str: str,
    teams_by_abbr: dict,
    default_teams: list[str],
) -> list[NewsItem]:
    feed_url = str(podcast_cfg.get("url", "") or "").strip()
    if not feed_url:
        return []

    transcribe_enabled = bool(podcast_cfg.get("transcribe", False))

    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        logger.warning(
            "Podcast feed error (%s / %s): %s",
            writer.get("name"), feed_url, parsed.bozo_exception,
        )
        return []

    show_title = str(
        getattr(parsed.feed, "title", "") or podcast_cfg.get("name", "") or ""
    ).strip()
    source_label = _writer_source_label(writer)
    source_with_podcast = (
        f"{source_label} [{show_title}]" if show_title
        else f"{source_label} [Podcast]"
    )
    writer_name = str(writer.get("name", "") or "").strip()

    whisper_model = settings.get("transcription", {}).get("whisper_model", "small")
    user_agent = settings["collection"].get("user_agent", "NFL-News-Agent/1.0")
    temp_dir = get_data_dir("raw", date_str) / "podcast_temp"
    delete_after = settings.get("transcription", {}).get("delete_audio_after", True)

    items: list[NewsItem] = []

    for entry in parsed.entries:
        pub_date = _parse_published(entry)
        if pub_date is None:
            pub_date = datetime.now(timezone.utc)
        if pub_date < cutoff:
            continue

        title = str(entry.get("title", "") or "").strip()
        link = str(entry.get("link", "") or "").strip()
        show_notes = _strip_html(str(entry.get("summary", "") or ""))
        if not title:
            continue

        audio_url = _podcast_audio_url(entry)
        if not link:
            link = audio_url or feed_url

        full_text = show_notes
        if transcribe_enabled and audio_url:
            slug = re.sub(r"[^\w\s-]", "", title)[:60].strip().replace(" ", "_") or "episode"
            transcript = transcribe_audio_url(
                audio_url, temp_dir, slug,
                whisper_model=whisper_model,
                delete_after=delete_after,
                user_agent=user_agent,
            )
            if transcript and len(transcript.strip()) > 50:
                full_text = transcript

        detected = _detect_teams(f"{title} {show_notes} {full_text[:500]}", teams_by_abbr)
        teams = _merge_teams(default_teams, detected)

        items.append(NewsItem(
            title=title,
            url=link,
            source=source_with_podcast,
            source_type="beat_writer",
            published=pub_date,
            summary=show_notes[:1000] if show_notes else (full_text[:1000] if full_text else ""),
            full_text=full_text,
            teams=teams,
            author=writer_name,
            category="news",
        ))

    return items


def _podcast_audio_url(entry) -> Optional[str]:
    """Return the first audio/* enclosure URL from a podcast feed entry."""
    enclosures = getattr(entry, "enclosures", None) or entry.get("enclosures", [])
    for enc in enclosures or []:
        url = enc.get("href") or enc.get("url") or ""
        mime = str(enc.get("type", "") or "").lower()
        if url and ("audio" in mime or url.lower().endswith((".mp3", ".m4a", ".aac", ".ogg", ".wav"))):
            return url
    return None


def save_beat_writer_results(
    news_items: list[NewsItem],
    transcripts: list[Transcript],
    date_str: str,
):
    """Persist beat writer collection output to data/raw/YYYY-MM-DD/beat_writers.json."""
    out_dir = get_data_dir("raw", date_str)
    path = out_dir / "beat_writers.json"
    payload = {
        "news_items": [item.to_dict() for item in news_items],
        "transcripts": [t.to_dict() for t in transcripts],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info(
        "Saved %d beat writer news items + %d transcripts to %s",
        len(news_items), len(transcripts), path,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    today = datetime.now().strftime("%Y-%m-%d")
    news, tx = collect_beat_writers(today)
    for item in news[:10]:
        print(f"[{item.source}] {item.title}")
        if item.teams:
            print(f"  Teams: {', '.join(item.teams)}")
        print(f"  {item.url}")
        print()
    print(f"Total: {len(news)} news items, {len(tx)} transcripts")
