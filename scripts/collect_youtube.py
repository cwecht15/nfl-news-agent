"""Standalone YouTube transcript collector.

Lives outside `scripts/run_daily.py` because YouTube collection on GitHub
Actions CI is broken (yt-dlp rejects the cookies file there). This script
is meant to run locally, where the residential IP makes cookies optional
and yt-dlp behaves.

Outputs (same files the daily pipeline used to produce):
  data/raw/<date>/youtube.json     — transcript metadata + text
  data/transcripts/<date>/*.txt    — one file per transcript
  data/youtube_seen.json           — dedup set, updated in place

The local daily pipeline picks these up via:
  python scripts/run_daily.py --include-yt-section --date <date>

Push them to the cloud's YouTube Report dashboard tab via:
  git add data/raw/<date>/youtube.json data/transcripts/<date> data/youtube_seen.json
  git commit -m "YouTube transcripts <date>"
  git push
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.youtube_collector import collect_youtube, save_youtube_results
from config_loader import get_data_dir
from processing.source_health import record_source_result


def setup_logging(date_str: str) -> None:
    log_dir = get_data_dir("logs")
    log_file = log_dir / f"{date_str}-youtube.log"
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
            "Collect YouTube press-conference transcripts locally. Run "
            "before `scripts/run_daily.py --include-yt-section` to feed "
            "the local report's YouTube section, or before `git push` "
            "to seed the cloud dashboard's YouTube Report tab."
        )
    )
    parser.add_argument(
        "--date",
        default=None,
        help=(
            "Date stamp for output files (YYYY-MM-DD). Defaults to today's "
            "UTC date so it lines up with the cloud cron's `date_str` if "
            "you push before 10:00 UTC."
        ),
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help=(
            "Override how far back to scan each channel. Falls back to "
            "settings.yaml `collection.lookback_hours` (default 28). Bump "
            "to 96+ when catching up after several missed days."
        ),
    )
    args = parser.parse_args()

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    setup_logging(date_str)
    logger = logging.getLogger("collect_youtube")

    logger.info("=" * 60)
    logger.info("YouTube transcript collection: %s", date_str)
    if args.lookback_hours is not None:
        logger.info("Lookback window: %d hours", args.lookback_hours)
    logger.info("=" * 60)

    error = ""
    try:
        transcripts = collect_youtube(date_str, lookback_hours=args.lookback_hours)
    except Exception as e:
        logger.exception("YouTube collection failed")
        transcripts = []
        error = f"YouTube collection failed: {e}"

    save_youtube_results(transcripts, date_str)
    # YouTube can legitimately have quiet days (no team pressers in the
    # lookback window) — flag low_volume so 0 transcripts doesn't ring the
    # 3-consecutive-failures alarm.
    record_source_result("YouTube", len(transcripts), error=error, low_volume=True)

    logger.info("=" * 60)
    logger.info("Wrote %d transcripts.", len(transcripts))
    logger.info("  metadata:  %s", get_data_dir("raw", date_str) / "youtube.json")
    logger.info("  text dir:  %s", get_data_dir("transcripts", date_str))
    logger.info("  seen set:  %s", PROJECT_ROOT / "data" / "youtube_seen.json")
    logger.info("=" * 60)
    logger.info("To attach these to today's local daily report:")
    logger.info(
        "  python scripts/run_daily.py --date %s --include-yt-section",
        date_str,
    )
    logger.info("To publish raw transcripts to the cloud's YouTube Report tab:")
    logger.info(
        "  git add data/raw/%s/youtube.json data/transcripts/%s data/youtube_seen.json",
        date_str, date_str,
    )
    logger.info('  git commit -m "YouTube transcripts %s"', date_str)
    logger.info("  git push")


if __name__ == "__main__":
    main()
