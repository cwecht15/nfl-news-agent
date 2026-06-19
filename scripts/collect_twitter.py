"""Standalone X/Twitter list collector (TwitterAPI.io REST API).

Mirrors scripts/collect_podcasts.py. API-only and CI-safe (just an API key),
so it can run on GitHub Actions. Run it, then push the output so the cloud
dashboard can surface / summarize the tweets.

Outputs:
  data/raw/<date>/twitter.json   — tweets as NewsItem records (merged by URL)
  data/twitter_seen.json         — dedup set of tweet IDs, updated in place

Setup:
  - Lists in config/sources.yaml under `twitter_lists:`
  - Knobs in config/settings.yaml under `twitter:` (set `enabled: true`)
  - API key in .env as TWITTERAPI_IO_KEY

Publish to the cloud via:
  git add data/raw/<date>/twitter.json data/twitter_seen.json
  git commit -m "Twitter list <date>"
  git push
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.twitter_collector import collect_twitter_list, save_twitter_results
from config_loader import get_data_dir
from processing.source_health import record_source_result


def setup_logging(date_str: str) -> None:
    log_dir = get_data_dir("logs")
    log_file = log_dir / f"{date_str}-twitter.log"
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
            "Collect tweets from the configured X/Twitter lists via "
            "TwitterAPI.io. Run before `git push` to seed the cloud dashboard."
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
            "Override how far back to pull. Falls back to settings.yaml "
            "`twitter.lookback_hours` (default 28). Bump for an initial "
            "backfill — but note every extra tweet returned is billed."
        ),
    )
    args = parser.parse_args()

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    setup_logging(date_str)
    logger = logging.getLogger("collect_twitter")

    logger.info("=" * 60)
    logger.info("Twitter list collection: %s", date_str)
    if args.lookback_hours is not None:
        logger.info("Lookback window: %d hours", args.lookback_hours)
    logger.info("=" * 60)

    error = ""
    try:
        tweets = collect_twitter_list(date_str, lookback_hours=args.lookback_hours)
    except Exception as e:
        logger.exception("Twitter collection failed")
        tweets = []
        error = f"Twitter collection failed: {e}"

    save_twitter_results(tweets, date_str)
    record_source_result("Twitter", len(tweets), error=error, low_volume=True)

    logger.info("=" * 60)
    logger.info("Wrote %d tweet(s).", len(tweets))
    logger.info("  metadata:  %s", get_data_dir("raw", date_str) / "twitter.json")
    logger.info("  seen set:  %s", PROJECT_ROOT / "data" / "twitter_seen.json")
    logger.info("=" * 60)
    logger.info("To publish to the cloud dashboard:")
    logger.info(
        "  git add data/raw/%s/twitter.json data/twitter_seen.json", date_str,
    )
    logger.info('  git commit -m "Twitter list %s"', date_str)
    logger.info("  git push")


if __name__ == "__main__":
    main()
