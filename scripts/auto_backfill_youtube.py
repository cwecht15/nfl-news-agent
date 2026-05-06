"""Unattended daily catch-up for YouTube transcripts.

Detects the last date that has a `data/raw/<YYYY-MM-DD>/youtube.json`,
computes how many days have gone uncovered, runs the daily collector
once with a lookback wide enough to span the gap (captions-only by
default so the run finishes in minutes), then commits and pushes the
new YouTube-related files to master so the live Streamlit Cloud
dashboard sees them.

Designed for Windows Task Scheduler. The wrapper `scripts/auto_backfill_youtube.bat`
captures stdout/stderr to `data/logs/<date>-yt-backfill.log`.

Honors the user's git-push-guardrails preference: stages only specific
YouTube paths (never `git add -A` or `git add data/`), pull-rebases
first to avoid colliding with the cloud GHA cron at 10 UTC.
"""
import argparse
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from collectors.youtube_collector import collect_youtube, save_youtube_results
from config_loader import get_data_dir
from processing.source_health import record_source_result

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_MAX_GAP_DAYS = 14
FIRST_RUN_GAP_DAYS = 2


def setup_logging(date_str: str) -> logging.Logger:
    log_dir = get_data_dir("logs")
    log_file = log_dir / f"{date_str}-yt-backfill.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("auto_backfill_yt")


def find_last_covered_date(logger: logging.Logger) -> str | None:
    """Highest YYYY-MM-DD daily dir under data/raw/ with a youtube.json."""
    raw_root = PROJECT_ROOT / "data" / "raw"
    if not raw_root.exists():
        return None
    candidates = []
    for d in raw_root.iterdir():
        if not d.is_dir() or not DATE_RE.match(d.name):
            continue
        yt_path = d / "youtube.json"
        if yt_path.exists():
            candidates.append(d.name)
    if not candidates:
        return None
    last = max(candidates)
    logger.info("Last covered date: %s (from %s)", last, raw_root / last / "youtube.json")
    return last


def compute_lookback_hours(
    today: datetime,
    last_covered: str | None,
    max_gap_days: int,
    logger: logging.Logger,
) -> tuple[int, int]:
    """Return (lookback_hours, effective_gap_days)."""
    if last_covered is None:
        logger.info(
            "No prior daily youtube.json found — treating as first run, "
            "looking back %d days.", FIRST_RUN_GAP_DAYS,
        )
        return FIRST_RUN_GAP_DAYS * 24 + 24, FIRST_RUN_GAP_DAYS

    last_dt = datetime.strptime(last_covered, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    gap_days = (today.date() - last_dt.date()).days
    if gap_days <= 0:
        return 0, 0
    if gap_days > max_gap_days:
        logger.warning(
            "Gap is %d days; capping at --max-gap-days=%d to avoid runaway. "
            "Use the dashboard backfill for older dates.",
            gap_days, max_gap_days,
        )
        gap_days = max_gap_days
    return gap_days * 24 + 24, gap_days


def run_git(cmd: list[str], logger: logging.Logger) -> subprocess.CompletedProcess:
    logger.info("$ %s", " ".join(cmd))
    return subprocess.run(
        cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
    )


def push_to_github(today_str: str, logger: logging.Logger) -> bool:
    """Stage YouTube paths only, commit if changed, push to origin master.

    Returns True on success (push happened OR nothing to push), False on
    any git failure. Honors the repo's git-push-guardrails: never stages
    data/ wholesale, never amends, never force-pushes.
    """
    paths = [
        f"data/raw/{today_str}/youtube.json",
        f"data/transcripts/{today_str}",
        "data/youtube_seen.json",
    ]
    existing = [p for p in paths if (PROJECT_ROOT / p).exists()]
    if not existing:
        logger.info("None of the expected YouTube paths exist — nothing to push.")
        return True

    pull = run_git(["git", "pull", "--rebase", "origin", "master"], logger)
    if pull.returncode != 0:
        logger.error(
            "git pull --rebase failed:\nstdout: %s\nstderr: %s",
            pull.stdout, pull.stderr,
        )
        return False

    # `data/raw/` and `data/transcripts/` are gitignored; the cloud GHA
    # cron uses `git add -f` to force-add them. Mirror that here so the
    # auto-backfill can publish without editing .gitignore.
    add = run_git(["git", "add", "-f", "--"] + existing, logger)
    if add.returncode != 0:
        logger.error("git add failed:\nstderr: %s", add.stderr)
        return False

    diff = run_git(["git", "diff", "--cached", "--quiet"], logger)
    if diff.returncode == 0:
        logger.info("No changes staged — no new transcripts to push.")
        return True

    msg = f"Auto: YouTube transcripts {today_str}"
    commit = run_git(["git", "commit", "-m", msg], logger)
    if commit.returncode != 0:
        logger.error(
            "git commit failed:\nstdout: %s\nstderr: %s",
            commit.stdout, commit.stderr,
        )
        return False

    push = run_git(["git", "push", "origin", "master"], logger)
    if push.returncode != 0:
        logger.error(
            "git push failed:\nstdout: %s\nstderr: %s",
            push.stdout, push.stderr,
        )
        return False

    logger.info("Pushed: %s", msg)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen; do no network or git work.",
    )
    parser.add_argument(
        "--no-push", action="store_true",
        help="Collect transcripts but skip the git commit/push step.",
    )
    parser.add_argument(
        "--max-gap-days", type=int, default=DEFAULT_MAX_GAP_DAYS,
        help=(
            f"Cap how many days of gap to attempt to fill in one run "
            f"(default {DEFAULT_MAX_GAP_DAYS}). Larger gaps suggest a "
            f"longer outage and are better handled via the dashboard."
        ),
    )
    parser.add_argument(
        "--enable-whisper", action="store_true",
        help=(
            "Enable Whisper fallback for videos without auto-captions. "
            "Off by default to keep unattended runs short."
        ),
    )
    args = parser.parse_args()

    today = datetime.now(timezone.utc)
    today_str = today.strftime("%Y-%m-%d")
    logger = setup_logging(today_str)

    logger.info("=" * 60)
    logger.info("Auto YouTube backfill: %s", today_str)
    logger.info("  enable_whisper=%s  max_gap_days=%d  dry_run=%s  no_push=%s",
                args.enable_whisper, args.max_gap_days, args.dry_run, args.no_push)
    logger.info("=" * 60)

    last_covered = find_last_covered_date(logger)
    lookback_hours, gap_days = compute_lookback_hours(
        today, last_covered, args.max_gap_days, logger,
    )

    if gap_days == 0:
        logger.info("Already covered through today (%s). Nothing to do.", today_str)
        return 0

    logger.info(
        "Gap: %d days. Lookback window: %d hours. Target date_str: %s",
        gap_days, lookback_hours, today_str,
    )

    if args.dry_run:
        logger.info("--dry-run set; exiting without collection.")
        return 0

    error = ""
    try:
        transcripts = collect_youtube(
            today_str,
            lookback_hours=lookback_hours,
            enable_whisper=args.enable_whisper,
        )
    except Exception as e:
        logger.exception("YouTube collection failed")
        transcripts = []
        error = f"YouTube collection failed: {e}"

    save_youtube_results(transcripts, today_str)
    record_source_result(
        "YouTube", len(transcripts), error=error, low_volume=True,
    )

    logger.info("Wrote %d transcripts for %s.", len(transcripts), today_str)

    if args.no_push:
        logger.info("--no-push set; skipping git push.")
        return 0 if not error else 1

    pushed_ok = push_to_github(today_str, logger)
    if error:
        return 1
    return 0 if pushed_ok else 2


if __name__ == "__main__":
    sys.exit(main())
