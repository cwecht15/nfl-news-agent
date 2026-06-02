"""Standalone NFL podcast collector (transcript-tag first, show-notes fallback).

Mirrors scripts/collect_youtube.py but for podcast RSS feeds. No audio
download / Whisper, so it's fast and CI-safe. Run locally and push the output
so the cloud Podcast Report dashboard tab can summarize it.

Outputs:
  data/raw/<date>/podcast.json     — episode metadata + text
  data/transcripts/<date>/pod_*.txt — one file per episode
  data/podcast_seen.json           — dedup set, updated in place

Publish to the cloud Podcast Report tab via:
  git add data/raw/<date>/podcast.json data/transcripts/<date> data/podcast_seen.json
  git commit -m "Podcast episodes <date>"
  git push
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.podcast_collector import collect_podcasts, save_podcast_results
from config_loader import get_data_dir
from processing.source_health import record_source_result


def setup_logging(date_str: str) -> None:
    log_dir = get_data_dir("logs")
    log_file = log_dir / f"{date_str}-podcast.log"
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Collect NFL podcast episodes from configured RSS feeds. Run "
            "before `git push` to seed the cloud dashboard's Podcast Report "
            "tab. Uses published transcripts when available, else show notes."
        )
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date stamp for output files (YYYY-MM-DD). Defaults to today's UTC date.",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help=(
            "Override how far back to scan each feed. Falls back to "
            "settings.yaml `collection.podcast_lookback_hours` (default 72). "
            "Bump to 168+ for an initial backfill."
        ),
    )
    args = parser.parse_args()

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    setup_logging(date_str)
    logger = logging.getLogger("collect_podcasts")

    logger.info("=" * 60)
    logger.info("Podcast collection: %s", date_str)
    if args.lookback_hours is not None:
        logger.info("Lookback window: %d hours", args.lookback_hours)
    logger.info("=" * 60)

    error = ""
    try:
        episodes = collect_podcasts(date_str, lookback_hours=args.lookback_hours)
    except Exception as e:
        logger.exception("Podcast collection failed")
        episodes = []
        error = f"Podcast collection failed: {e}"

    save_podcast_results(episodes, date_str)
    record_source_result("Podcasts", len(episodes), error=error, low_volume=True)

    logger.info("=" * 60)
    logger.info("Wrote %d podcast episodes.", len(episodes))
    logger.info("  metadata:  %s", get_data_dir("raw", date_str) / "podcast.json")
    logger.info("  text dir:  %s", get_data_dir("transcripts", date_str))
    logger.info("  seen set:  %s", PROJECT_ROOT / "data" / "podcast_seen.json")
    logger.info("=" * 60)
    logger.info("To publish to the cloud's Podcast Report tab:")
    logger.info(
        "  git add data/raw/%s/podcast.json data/transcripts/%s data/podcast_seen.json",
        date_str, date_str,
    )
    logger.info('  git commit -m "Podcast episodes %s"', date_str)
    logger.info("  git push")


if __name__ == "__main__":
    main()
