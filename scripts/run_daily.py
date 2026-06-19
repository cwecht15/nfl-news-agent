"""Daily orchestrator.

Main entry point that runs the full news pipeline:
1. Collect from all sources (RSS, web, Reddit, beat writers, Twitter — no YouTube)
2. Deduplicate news items
3. Summarize with the configured LLM provider
4. Build and save the daily report

YouTube transcripts live in their own tool now. To collect transcripts and
get a YouTube section appended to today's local report:
    python scripts/collect_youtube.py
    python scripts/run_daily.py --include-yt-section

The cloud GitHub Actions cron never passes --include-yt-section, so the
public daily report stays YouTube-free.

Catch-up mode: when the PC has been off for multiple days, widen the
collection window on a single run so more of the missed news still in
feeds is captured:
    python scripts/run_daily.py --lookback-hours 96   # 4 days
"""

import argparse
import json
import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_data_dir, get_settings, get_summary_provider
from collectors.rss_collector import collect_rss, collect_espn_team_news, save_rss_results
from collectors.web_scraper import (
    collect_web,
    get_last_web_source_status,
    save_web_results,
)
from collectors.reddit_collector import collect_reddit, save_reddit_results
from collectors.beat_writer_collector import (
    collect_beat_writers,
    save_beat_writer_results,
)
from collectors.fantasypoints_collector import (
    collect_fantasypoints,
    save_fantasypoints_results,
)
from collectors.twitter_collector import (
    collect_twitter_list,
    save_twitter_results,
)
from models import Transcript
from processing.cross_day_filter import filter_recent_duplicates
from processing.deduplicator import deduplicate, flatten_groups
from processing.quality_filter import filter_news_items
from processing.source_health import get_health_alerts, record_source_result
from processing.fp_section import build_fp_section
from processing.summarizer import run_summarization
from processing.yt_section import build_yt_section
from reports.report_builder import build_report, save_report


def setup_logging(date_str: str):
    """Configure logging to file and console."""
    log_dir = get_data_dir("logs")
    log_file = log_dir / f"{date_str}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _summary_provider_label(provider: str) -> str:
    """Return a human-readable label for the configured provider."""
    if provider == "openai":
        return "OpenAI API"
    if provider == "ollama":
        return "Ollama API"
    return "Anthropic API"


def _log_llm_usage(logger: logging.Logger, usage: dict):
    """Log a concise LLM usage summary when available."""
    if not usage:
        return

    provider = usage.get("provider", "unknown")
    model = usage.get("model", "unknown")
    request_count = usage.get("request_count", 0)
    estimated_cost = usage.get("estimated_cost_usd")

    logger.info(
        "LLM summary: %s/%s | calls=%s | input=%s | output=%s | estimated cost=$%.4f",
        provider,
        model,
        request_count,
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        float(estimated_cost or 0.0),
    )

    tracking_note = usage.get("tracking_note")
    if tracking_note:
        logger.info("LLM note: %s", tracking_note)


def _build_source_alerts(source_status: dict[str, dict]) -> list[dict]:
    """Convert source status warnings into report alerts."""
    alerts = []
    for payload in source_status.values():
        severity = str(payload.get("severity", "info") or "info").lower()
        if severity == "info":
            continue

        alerts.append({
            "source": payload.get("source", "Unknown source"),
            "severity": severity,
            "message": payload.get("message", ""),
            "status": payload.get("status", ""),
            "latest_expiry": payload.get("latest_expiry"),
        })

    return alerts


def _log_source_alerts(logger: logging.Logger, alerts: list[dict]):
    """Write any source alerts to the run log."""
    for alert in alerts:
        logger.warning(
            "Source alert [%s] %s: %s",
            str(alert.get("severity", "warning")).upper(),
            alert.get("source", "Unknown source"),
            alert.get("message", ""),
        )


def _status_file() -> Path:
    """Return path to the pipeline status file."""
    return PROJECT_ROOT / "data" / "pipeline_status.json"


def write_status(step: str, state: str = "running", detail: str = ""):
    """Write current pipeline status to a JSON file for the dashboard."""
    import json
    status = {
        "state": state,
        "step": step,
        "detail": detail,
        "pid": os.getpid(),
        "started_at": getattr(write_status, "_started", datetime.now().isoformat()),
        "updated_at": datetime.now().isoformat(),
    }
    if not hasattr(write_status, "_started"):
        write_status._started = status["started_at"]
    _status_file().write_text(json.dumps(status), encoding="utf-8")


def clear_status():
    """Remove the status file when the pipeline finishes."""
    _status_file().unlink(missing_ok=True)


def _load_existing_transcripts(date_str: str, logger: logging.Logger) -> list[Transcript]:
    """Hydrate transcripts from the file `scripts/collect_youtube.py` writes.

    Returns [] when the file is missing or malformed. The cloud pipeline
    never calls this (only --include-yt-section runs do).
    """
    path = get_data_dir("raw", date_str) / "youtube.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning("Could not load %s: %s", path, e)
        return []
    return [Transcript.from_dict(d) for d in raw]


def run(
    lookback_hours: int | None = None,
    include_yt_section: bool = False,
    date_override: str | None = None,
):
    """Execute the full daily pipeline.

    Args:
        lookback_hours: Optional override for how far back each collector
            should look. Use for catch-up runs after missed days. When
            None, collectors fall back to `collection.lookback_hours` in
            settings.yaml (default 28).
        include_yt_section: When True, attach a YouTube section to the
            report by loading transcripts that `scripts/collect_youtube.py`
            wrote earlier. Off by default — the cloud cron never sets it,
            so the public daily report stays YouTube-free.
        date_override: When set (YYYY-MM-DD), stamp the run with this
            date instead of "today". Useful for re-generating yesterday's
            report after a logic change.
    """
    date_str = date_override or datetime.now().strftime("%Y-%m-%d")
    setup_logging(date_str)
    logger = logging.getLogger("orchestrator")

    write_status("Starting", "running")

    logger.info("=" * 60)
    logger.info("NFL News Agent - Daily Run: %s", date_str)
    if lookback_hours is not None:
        logger.info("Catch-up mode: lookback window = %d hours", lookback_hours)
    logger.info("=" * 60)

    summary_provider = get_summary_provider()

    write_status("Step 1", "running", "Collecting from all sources")
    logger.info("Step 1: Collecting from all sources (parallel)...")

    collector_alerts: list[dict] = []

    def _safe_result(future, label):
        try:
            return future.result()
        except Exception as e:
            logger.error("%s collector failed: %s", label, e)
            collector_alerts.append({
                "source": f"{label} Collector",
                "severity": "error",
                "message": f"{label} collection failed: {e}",
            })
            return []

    # Twitter is collected on the cloud pipeline ONLY (GitHub Actions), so it
    # isn't double-pulled and double-summarized by both the local scheduled
    # task and the cloud run. For a manual local pull use scripts/collect_twitter.py.
    collect_twitter_on_ci = bool(os.environ.get("GITHUB_ACTIONS"))

    with ThreadPoolExecutor(max_workers=7) as executor:
        future_rss = executor.submit(collect_rss, lookback_hours=lookback_hours)
        future_espn = executor.submit(
            collect_espn_team_news, lookback_hours=lookback_hours
        )
        future_web = executor.submit(collect_web, lookback_hours=lookback_hours)
        future_reddit = executor.submit(
            collect_reddit, lookback_hours=lookback_hours
        )
        future_bw = executor.submit(
            collect_beat_writers,
            date_str,
            lookback_hours=lookback_hours,
            skip_youtube=not include_yt_section,
        )
        # FantasyPoints articles use their own per-section lookback
        # (settings.fantasypoints.lookback_hours), not the news pipeline's
        # window — the section is meant to mirror "today's articles".
        future_fp = executor.submit(collect_fantasypoints)
        # Twitter/X insider lists via the TwitterAPI.io REST API. API-only and
        # CI-safe (unlike YouTube), but collected on the cloud run ONLY (see
        # collect_twitter_on_ci) to avoid double-pull/double-summarize. Tweets
        # come back as NewsItems and flow through dedup → Team Notes /
        # League-Wide exactly like RSS. Still self-gates on twitter.enabled.
        future_twitter = (
            executor.submit(collect_twitter_list, date_str, lookback_hours=lookback_hours)
            if collect_twitter_on_ci else None
        )

    rss_items = _safe_result(future_rss, "RSS")
    espn_items = _safe_result(future_espn, "ESPN Teams")
    web_items = _safe_result(future_web, "Web")
    reddit_items = _safe_result(future_reddit, "Reddit")
    bw_result = _safe_result(future_bw, "Beat Writers")
    if isinstance(bw_result, tuple) and len(bw_result) == 2:
        bw_items, bw_transcripts = bw_result
    else:
        bw_items, bw_transcripts = [], []
    fp_items = _safe_result(future_fp, "FantasyPoints")
    twitter_items = _safe_result(future_twitter, "Twitter") if future_twitter else []

    save_rss_results(rss_items + espn_items, date_str)
    save_web_results(web_items, date_str)
    save_reddit_results(reddit_items, date_str)
    save_beat_writer_results(bw_items, bw_transcripts, date_str)
    save_fantasypoints_results(fp_items, date_str)
    if future_twitter:
        save_twitter_results(twitter_items, date_str)

    # Record source health
    record_source_result("RSS Feeds", len(rss_items),
                         error=next((a["message"] for a in collector_alerts if "RSS" in a.get("source", "")), ""))
    record_source_result("ESPN Teams", len(espn_items),
                         error=next((a["message"] for a in collector_alerts if "ESPN" in a.get("source", "")), ""))
    record_source_result("Web Scraper", len(web_items),
                         error=next((a["message"] for a in collector_alerts if "Web" in a.get("source", "")), ""))
    record_source_result("Reddit", len(reddit_items),
                         error=next((a["message"] for a in collector_alerts if "Reddit" in a.get("source", "")), ""))
    record_source_result(
        "Beat Writers",
        len(bw_items) + len(bw_transcripts),
        error=next((a["message"] for a in collector_alerts if "Beat Writers" in a.get("source", "")), ""),
        low_volume=True,
    )
    record_source_result(
        "FantasyPoints",
        len(fp_items),
        error=next((a["message"] for a in collector_alerts if "FantasyPoints" in a.get("source", "")), ""),
        low_volume=True,
    )
    if future_twitter:
        record_source_result(
            "Twitter",
            len(twitter_items),
            error=next((a["message"] for a in collector_alerts if "Twitter" in a.get("source", "")), ""),
            low_volume=True,
        )

    web_source_status = get_last_web_source_status()
    health_alerts = get_health_alerts()
    source_alerts = collector_alerts + _build_source_alerts(web_source_status) + health_alerts
    _log_source_alerts(logger, source_alerts)

    all_news = rss_items + espn_items + web_items + reddit_items + bw_items + twitter_items

    # Optional: hydrate transcripts for the YT section. The news pipeline
    # itself never collects YouTube — that's `scripts/collect_youtube.py`.
    yt_transcripts: list[Transcript] = []
    if include_yt_section:
        yt_transcripts = _load_existing_transcripts(date_str, logger) + bw_transcripts
        if yt_transcripts:
            logger.info(
                "Hydrated %d transcripts for the YouTube section.",
                len(yt_transcripts),
            )
        else:
            logger.warning(
                "--include-yt-section set but no transcripts found at "
                "data/raw/%s/youtube.json. Run scripts/collect_youtube.py "
                "first to populate them.",
                date_str,
            )

    logger.info(
        "Collection complete: %d news items%s",
        len(all_news),
        f", {len(yt_transcripts)} transcripts (YT section)" if yt_transcripts else "",
    )

    all_news, dropped_fluff = filter_news_items(all_news)
    if dropped_fluff:
        logger.info(
            "Quality filter: dropped %d items (e.g. %s)",
            len(dropped_fluff),
            (dropped_fluff[0].title if dropped_fluff else "")[:80],
        )

    write_status("Step 2", "running", "Deduplicating stories")
    logger.info("Step 2: Deduplicating stories...")
    groups = deduplicate(all_news)
    deduped_news = flatten_groups(groups)
    logger.info(
        "Deduplicated: %d items -> %d unique stories",
        len(all_news),
        len(deduped_news),
    )

    cross_day_cfg = get_settings().get("cross_day_dedup", {})
    if cross_day_cfg.get("enabled", False):
        write_status("Step 2b", "running", "Filtering cross-day duplicates")
        logger.info("Step 2b: Filtering cross-day duplicates...")
        skip = set(cross_day_cfg.get("skip_categories") or [])
        before = len(deduped_news)
        deduped_news, _dropped = filter_recent_duplicates(
            deduped_news,
            raw_dir=PROJECT_ROOT / "data" / "raw",
            current_date=date_str,
            lookback_days=int(cross_day_cfg.get("lookback_days", 2)),
            threshold=float(cross_day_cfg.get("threshold", 0.82)),
            skip_categories=skip,
        )
        logger.info(
            "Cross-day filter: %d -> %d unique stories (%d suppressed as repeats)",
            before, len(deduped_news), before - len(deduped_news),
        )

    write_status("Step 3", "running", f"Summarizing with {_summary_provider_label(summary_provider)}")
    logger.info(
        "Step 3: Summarizing with %s...",
        _summary_provider_label(summary_provider),
    )
    try:
        summary_result = run_summarization(deduped_news)
    except ValueError as e:
        logger.error("Summarization failed: %s", e)
        if summary_provider == "openai":
            logger.error("Check your OPENAI_API_KEY in .env")
        elif summary_provider == "anthropic":
            logger.error("Check your ANTHROPIC_API_KEY in .env")
        else:
            logger.error(
                "Check that Ollama is running and the configured model is available."
            )

        summary_result = {
            "sections": {
                "transactions": {
                    "summary": "Summarization unavailable (provider not configured).",
                    "count": len(
                        [i for i in deduped_news if i.category == "transaction"]
                    ),
                },
                "injuries": {
                    "summary": "Summarization unavailable.",
                    "count": len(
                        [i for i in deduped_news if i.category == "injury"]
                    ),
                },
                "league_wide": {
                    "summary": "Summarization unavailable.",
                    "numbered_sources": [],
                },
            },
            "team_highlights": {},
            "llm_usage": {
                "provider": summary_provider,
                "tracked": summary_provider == "openai",
                "request_count": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "uncached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "input_cost_usd": 0.0,
                "output_cost_usd": 0.0,
                "estimated_cost_usd": 0.0,
                "error": str(e),
            },
        }

    _log_llm_usage(logger, summary_result.get("llm_usage", {}))

    write_status("Step 4", "running", "Snapshotting projections")
    logger.info("Step 4: Snapshotting projections...")
    rank_movers: list[dict] = []
    try:
        from scripts.snapshot_projections import (
            _get_client as _get_sheets_client,
            snapshot_players,
            snapshot_teams,
            snapshot_fantasy,
            diff_snapshots,
            diff_fantasy,
            write_changelog,
            _latest_snapshot,
        )
        gc = _get_sheets_client()
        prev_players = _latest_snapshot("players", before_date=date_str)
        cur_players = snapshot_players(gc, date_str)

        prev_fantasy = _latest_snapshot("fantasy", before_date=date_str)
        cur_fantasy = snapshot_fantasy(gc, date_str)

        prev_teams = _latest_snapshot("teams", before_date=date_str)
        cur_teams = snapshot_teams(gc, date_str)

        player_changes = diff_snapshots(cur_players, prev_players, "player") if prev_players else []
        fantasy_changes = diff_fantasy(cur_fantasy, prev_fantasy) if prev_fantasy else []
        team_changes = diff_snapshots(cur_teams, prev_teams, "team") if prev_teams else []
        if player_changes:
            write_changelog(player_changes, date_str, "player")
        if fantasy_changes:
            write_changelog(fantasy_changes, date_str, "fantasy")
        if team_changes:
            write_changelog(team_changes, date_str, "team")

        rank_movers = [c for c in fantasy_changes if c.get("adjusted")]

        adj_count = len([c for c in player_changes if "Adj" in c.get("metric", "")])
        proj_count = len([c for c in player_changes if c.get("type") == "metric_change" and "Adj" not in c.get("metric", "")])
        logger.info(
            "Projection snapshot: %d players, %d fantasy, %d teams | %d adj tweaks, %d projection shifts, %d rank changes, %d team changes",
            len(cur_players), len(cur_fantasy), len(cur_teams),
            adj_count, proj_count, len(rank_movers), len(team_changes),
        )
    except Exception as e:
        logger.warning("Projection snapshot failed (non-fatal): %s", e)

    write_status("Step 5", "running", "Updating depth charts")
    logger.info("Step 5: Updating depth charts...")
    dc_changes: list[dict] = []
    try:
        from collectors.depth_chart_collector import (
            scrape_all_teams, save_depth_charts, load_latest_depth_charts,
            diff_depth_charts, get_depth_chart_dates,
        )
        # Load previous before scraping new — strictly before today so
        # multiple same-day runs don't compare today against itself.
        prev_dc = load_latest_depth_charts(before_date=date_str)
        cur_dc = scrape_all_teams(delay=1.5)
        save_depth_charts(cur_dc, date_str)

        if prev_dc:
            dc_changes = diff_depth_charts(cur_dc, prev_dc)
            promos = [c for c in dc_changes if c["type"] == "promoted"]
            demos = [c for c in dc_changes if c["type"] == "demoted"]
            adds = [c for c in dc_changes if c["type"] == "added"]
            removes = [c for c in dc_changes if c["type"] == "removed"]
            team_moves = [c for c in dc_changes if c["type"] == "team_change"]
            logger.info(
                "Depth chart changes: %d promotions, %d demotions, %d added, %d removed, %d team moves",
                len(promos), len(demos), len(adds), len(removes), len(team_moves),
            )
        else:
            logger.info("First depth chart snapshot — no changes to compare.")
    except Exception as e:
        logger.warning("Depth chart update failed (non-fatal): %s", e)

    yt_section: dict = {}
    if include_yt_section and yt_transcripts:
        write_status("Step 6a", "running", "Building YouTube section")
        logger.info("Step 6a: Building YouTube section from %d transcripts...",
                    len(yt_transcripts))
        try:
            yt_section = build_yt_section(
                yt_transcripts,
                date_label=date_str,
            )
        except Exception as e:
            logger.error("YouTube section build failed (non-fatal): %s", e)

    fp_section: dict | None = None
    if fp_items:
        write_status("Step 6b", "running", "Building FantasyPoints section")
        logger.info(
            "Step 6b: Building FantasyPoints section from %d articles...",
            len(fp_items),
        )
        try:
            # Reuse summarization's usage tracker so FP tokens roll up
            # into the same daily total displayed at the bottom of the report.
            fp_section = build_fp_section(
                fp_items,
                usage_tracker=summary_result.get("llm_usage"),
                date_label=date_str,
            )
        except Exception as e:
            logger.error("FantasyPoints section build failed (non-fatal): %s", e)
            fp_section = None

    write_status("Step 6", "running", "Building daily report")
    logger.info("Step 6: Building daily report...")
    report = build_report(
        date_str=date_str,
        sections=summary_result["sections"],
        team_highlights=summary_result["team_highlights"],
        news_items=deduped_news,
        llm_usage=summary_result.get("llm_usage"),
        alerts=source_alerts,
        depth_chart_changes=dc_changes,
        projection_movers=rank_movers,
        yt_section=yt_section,
        fp_section=fp_section,
    )
    json_path, html_path = save_report(report)

    write_status("Step 7", "running", "Cleaning up old data")
    logger.info("Step 7: Cleaning up old data...")
    cleanup_old_data(logger)

    logger.info("=" * 60)
    logger.info("Daily run complete!")
    logger.info("JSON report: %s", json_path)
    logger.info("HTML report: %s", html_path)
    logger.info(
        "Stats: %d news items, %d team highlights%s",
        len(deduped_news),
        len(report.team_highlights),
        f", YT section: {len(yt_transcripts)} transcripts" if yt_transcripts else "",
    )
    logger.info("=" * 60)

    clear_status()


def _date_dirs_older_than(base_dir: Path, days: int) -> list[Path]:
    """Return date-named subdirectories older than the given number of days."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    old = []
    if not base_dir.exists():
        return old
    for d in base_dir.iterdir():
        if d.is_dir() and d.name <= cutoff:
            try:
                datetime.strptime(d.name, "%Y-%m-%d")
                old.append(d)
            except ValueError:
                continue
    return old


def cleanup_old_data(logger: logging.Logger):
    """Delete reports and raw data older than configured retention thresholds."""
    settings = get_settings()
    storage = settings.get("storage", {})
    reports_keep = storage.get("reports_to_keep", 90)
    raw_keep = storage.get("raw_data_to_keep", 7)

    # Clean old reports
    reports_dir = get_data_dir("reports")
    cutoff_date = (datetime.now() - timedelta(days=reports_keep)).strftime("%Y-%m-%d")
    removed_reports = 0
    for f in reports_dir.iterdir():
        if f.is_file() and f.stem <= cutoff_date:
            try:
                datetime.strptime(f.stem, "%Y-%m-%d")
                f.unlink()
                removed_reports += 1
            except ValueError:
                continue
    if removed_reports:
        logger.info("Removed %d report files older than %d days.", removed_reports, reports_keep)

    # Skip raw/transcript pruning when running on GitHub Actions: the cloud
    # repo intentionally accumulates these year-round (manual end-of-season
    # reset) so the YouTube Report tab can summarize transcripts the user
    # pushed locally days or weeks ago. Local runs still prune to keep disk
    # usage in check.
    if os.environ.get("GITHUB_ACTIONS"):
        logger.info("Skipping raw/transcript cleanup on CI (preserves pushed YouTube data).")
    else:
        raw_dir = get_data_dir("raw")
        old_raw = _date_dirs_older_than(raw_dir, raw_keep)
        for d in old_raw:
            shutil.rmtree(d)
        if old_raw:
            logger.info("Removed %d raw data directories older than %d days.", len(old_raw), raw_keep)

        transcripts_dir = get_data_dir("transcripts")
        old_transcripts = _date_dirs_older_than(transcripts_dir, raw_keep)
        for d in old_transcripts:
            shutil.rmtree(d)
        if old_transcripts:
            logger.info("Removed %d transcript directories older than %d days.", len(old_transcripts), raw_keep)

    # Clean old log files
    logs_dir = get_data_dir("logs")
    cutoff_logs = (datetime.now() - timedelta(days=reports_keep)).strftime("%Y-%m-%d")
    removed_logs = 0
    for f in logs_dir.iterdir():
        if f.is_file() and f.stem <= cutoff_logs:
            try:
                datetime.strptime(f.stem, "%Y-%m-%d")
                f.unlink()
                removed_logs += 1
            except ValueError:
                continue
    if removed_logs:
        logger.info("Removed %d log files older than %d days.", removed_logs, reports_keep)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the NFL News Agent daily pipeline."
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=None,
        help=(
            "Override the collection lookback window (hours). Use for "
            "catch-up runs after missed days, e.g. --lookback-hours 96 "
            "after a long weekend off. Falls back to settings.yaml "
            "(default 28) when omitted."
        ),
    )
    parser.add_argument(
        "--include-yt-section",
        action="store_true",
        help=(
            "Attach a YouTube section to the daily report by loading "
            "transcripts that scripts/collect_youtube.py wrote earlier. "
            "Off by default — the cloud cron never sets it, so the public "
            "daily report stays YouTube-free."
        ),
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Override the run date (YYYY-MM-DD). Useful for re-generating "
             "a prior day's report after a logic change.",
    )
    args = parser.parse_args()

    try:
        run(
            lookback_hours=args.lookback_hours,
            include_yt_section=args.include_yt_section,
            date_override=args.date,
        )
    except Exception:
        clear_status()
        raise
