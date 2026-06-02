"""Backfill the real YouTube channel name into already-collected youtube.json.

Older collections stored `channel_name` as the *team* display name (e.g.
"Miami Dolphins") even when the video came from a beat/analysis channel under
that team (e.g. "Dolphins Collective"). The collector now captures the actual
uploader at scan time; this one-off script patches existing data so the
dashboard's Channel column shows the genuine channel name without re-running
transcription.

Metadata-only: one lightweight yt-dlp call per video, no audio/captions.
yt-dlp works locally (not on CI), so run this on the same machine you collect on.

Usage:
    python scripts/backfill_youtube_channel_names.py                 # all dated dirs
    python scripts/backfill_youtube_channel_names.py --start 2026-05-26 --end 2026-06-01
    python scripts/backfill_youtube_channel_names.py --dry-run       # report only
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.youtube_collector import _fetch_video_details
from config_loader import get_data_dir

logger = logging.getLogger("yt_channel_backfill")


def _dated_dirs(raw_base: Path, start: str, end: str) -> list[Path]:
    out = []
    for d in sorted(raw_base.iterdir()):
        if not d.is_dir() or len(d.name) != 10 or d.name[4] != "-":
            continue
        if start and d.name < start:
            continue
        if end and d.name > end:
            continue
        if (d / "youtube.json").exists():
            out.append(d)
    return out


def _real_channel(url: str) -> str:
    try:
        info = _fetch_video_details(url)
    except Exception as e:
        logger.debug("fetch failed for %s: %s", url, e)
        return ""
    return (info.get("channel") or info.get("uploader") or "").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="", help="YYYY-MM-DD lower bound (inclusive)")
    ap.add_argument("--end", default="", help="YYYY-MM-DD upper bound (inclusive)")
    ap.add_argument("--delay", type=float, default=1.0, help="Politeness delay between fetches")
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    raw_base = get_data_dir("raw")
    dirs = _dated_dirs(raw_base, args.start, args.end)
    if not dirs:
        logger.info("No youtube.json files found in range.")
        return

    total_changed = 0
    total_seen = 0
    # Cache by video_id so the same video across files is fetched once.
    cache: dict[str, str] = {}

    for d in dirs:
        path = d / "youtube.json"
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Skipping %s (unreadable: %s)", path, e)
            continue

        changed = 0
        for r in records:
            total_seen += 1
            vid = r.get("video_id", "")
            url = r.get("url", "")
            if not url:
                continue
            if vid in cache:
                real = cache[vid]
            else:
                real = _real_channel(url)
                cache[vid] = real
                time.sleep(args.delay)
            if real and real != r.get("channel_name", ""):
                logger.info(
                    "  [%s] %r -> %r  (%s)",
                    r.get("team", ""), r.get("channel_name", ""), real,
                    r.get("title", "")[:45],
                )
                r["channel_name"] = real
                changed += 1

        if changed and not args.dry_run:
            path.write_text(
                json.dumps(records, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        logger.info("%s: %d/%d updated%s", d.name, changed, len(records),
                    " (dry-run)" if args.dry_run else "")
        total_changed += changed

    logger.info(
        "Done. %d/%d records updated across %d day(s)%s.",
        total_changed, total_seen, len(dirs),
        " (dry-run, nothing written)" if args.dry_run else "",
    )


if __name__ == "__main__":
    main()
