"""Daily orchestrator.

Main entry point that runs the full pipeline:
1. Collect from all sources (RSS, web, YouTube)
2. Deduplicate news items
3. Summarize with the configured LLM provider
4. Build and save the daily report

Run manually: python scripts/run_daily.py
Or via Task Scheduler using run_daily.bat
"""

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
from collectors.youtube_collector import collect_youtube, save_youtube_results
from processing.deduplicator import deduplicate, flatten_groups
from processing.source_health import get_health_alerts, record_source_result
from processing.summarizer import run_summarization
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


def run():
    """Execute the full daily pipeline."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    setup_logging(date_str)
    logger = logging.getLogger("orchestrator")

    write_status("Starting", "running")

    logger.info("=" * 60)
    logger.info("NFL News Agent - Daily Run: %s", date_str)
    logger.info("=" * 60)

    summary_provider = get_summary_provider()

    write_status("Step 1", "running", "Collecting from all sources")
    logger.info("Step 1: Collecting from all sources (parallel)...")

    rss_items = []
    web_items = []
    reddit_items = []
    transcripts = []
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

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_rss = executor.submit(collect_rss)
        future_espn = executor.submit(collect_espn_team_news)
        future_web = executor.submit(collect_web)
        future_reddit = executor.submit(collect_reddit)
        future_yt = executor.submit(collect_youtube, date_str)

    rss_items = _safe_result(future_rss, "RSS")
    espn_items = _safe_result(future_espn, "ESPN Teams")
    web_items = _safe_result(future_web, "Web")
    reddit_items = _safe_result(future_reddit, "Reddit")
    transcripts = _safe_result(future_yt, "YouTube")

    save_rss_results(rss_items + espn_items, date_str)
    save_web_results(web_items, date_str)
    save_reddit_results(reddit_items, date_str)
    save_youtube_results(transcripts, date_str)

    # Record source health
    record_source_result("RSS Feeds", len(rss_items),
                         error=next((a["message"] for a in collector_alerts if "RSS" in a.get("source", "")), ""))
    record_source_result("ESPN Teams", len(espn_items),
                         error=next((a["message"] for a in collector_alerts if "ESPN" in a.get("source", "")), ""))
    record_source_result("Web Scraper", len(web_items),
                         error=next((a["message"] for a in collector_alerts if "Web" in a.get("source", "")), ""))
    record_source_result("Reddit", len(reddit_items),
                         error=next((a["message"] for a in collector_alerts if "Reddit" in a.get("source", "")), ""))
    record_source_result("YouTube", len(transcripts),
                         error=next((a["message"] for a in collector_alerts if "YouTube" in a.get("source", "")), ""))

    web_source_status = get_last_web_source_status()
    health_alerts = get_health_alerts()
    source_alerts = collector_alerts + _build_source_alerts(web_source_status) + health_alerts
    _log_source_alerts(logger, source_alerts)

    all_news = rss_items + espn_items + web_items + reddit_items
    logger.info(
        "Collection complete: %d news items, %d transcripts",
        len(all_news),
        len(transcripts),
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

    write_status("Step 3", "running", f"Summarizing with {_summary_provider_label(summary_provider)}")
    logger.info(
        "Step 3: Summarizing with %s...",
        _summary_provider_label(summary_provider),
    )
    try:
        summary_result = run_summarization(deduped_news, transcripts)
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
                "press_conferences": {
                    "summary": "Summarization unavailable.",
                    "count": len(transcripts),
                },
                "analysis": {
                    "summary": "Summarization unavailable.",
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

    write_status("Step 4", "running", "Building daily report")
    logger.info("Step 4: Building daily report...")
    report = build_report(
        date_str=date_str,
        sections=summary_result["sections"],
        team_highlights=summary_result["team_highlights"],
        news_items=deduped_news,
        transcripts=transcripts,
        llm_usage=summary_result.get("llm_usage"),
        alerts=source_alerts,
    )
    json_path, html_path = save_report(report)

    write_status("Step 5", "running", "Snapshotting projections")
    logger.info("Step 5: Snapshotting projections...")
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
        prev_players = _latest_snapshot("players")
        cur_players = snapshot_players(gc, date_str)

        prev_fantasy = _latest_snapshot("fantasy")
        cur_fantasy = snapshot_fantasy(gc, date_str)

        prev_teams = _latest_snapshot("teams")
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

        adj_count = len([c for c in player_changes if "Adj" in c.get("metric", "")])
        proj_count = len([c for c in player_changes if c.get("type") == "metric_change" and "Adj" not in c.get("metric", "")])
        rank_changes = len([c for c in fantasy_changes if c.get("adjusted")])
        logger.info(
            "Projection snapshot: %d players, %d fantasy, %d teams | %d adj tweaks, %d projection shifts, %d rank changes, %d team changes",
            len(cur_players), len(cur_fantasy), len(cur_teams),
            adj_count, proj_count, rank_changes, len(team_changes),
        )
    except Exception as e:
        logger.warning("Projection snapshot failed (non-fatal): %s", e)

    write_status("Step 6", "running", "Updating depth charts")
    logger.info("Step 6: Updating depth charts...")
    try:
        from collectors.depth_chart_collector import (
            scrape_all_teams, save_depth_charts, load_latest_depth_charts,
            diff_depth_charts, get_depth_chart_dates,
        )
        # Load previous before scraping new
        prev_dc = load_latest_depth_charts()
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

    write_status("Step 7", "running", "Cleaning up old data")
    logger.info("Step 7: Cleaning up old data...")
    cleanup_old_data(logger)

    logger.info("=" * 60)
    logger.info("Daily run complete!")
    logger.info("JSON report: %s", json_path)
    logger.info("HTML report: %s", html_path)
    logger.info(
        "Stats: %d news items, %d transcripts, %d team highlights",
        len(deduped_news),
        len(transcripts),
        len(report.team_highlights),
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

    # Clean old raw data directories
    raw_dir = get_data_dir("raw")
    old_raw = _date_dirs_older_than(raw_dir, raw_keep)
    for d in old_raw:
        shutil.rmtree(d)
    if old_raw:
        logger.info("Removed %d raw data directories older than %d days.", len(old_raw), raw_keep)

    # Clean old transcript directories
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
    try:
        run()
    except Exception:
        clear_status()
        raise
