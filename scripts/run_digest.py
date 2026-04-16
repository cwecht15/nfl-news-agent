"""Weekly digest generator.

Synthesizes the past N daily reports into a single rollup briefing.

Usage:
    python scripts/run_digest.py           # Default: last 7 days
    python scripts/run_digest.py --days 14 # Custom range
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_data_dir, get_summary_provider
from processing.summarizer import _get_client, _get_runtime_config, _call_model, _init_usage_tracker, SYSTEM_PROMPT
from reports.report_builder import list_available_reports, load_report

logger = logging.getLogger(__name__)


def _collect_digest_data(dates: list[str]) -> dict:
    """Aggregate data from multiple daily reports."""
    all_transactions = []
    all_injuries = []
    all_press = []
    all_analysis = []
    team_mentions: dict[str, int] = {}
    team_summaries: dict[str, list[str]] = {}
    total_cost = 0.0

    for date_str in dates:
        try:
            report = load_report(date_str)
        except Exception:
            continue

        for section_key, section in report.sections.items():
            summary = section.get("summary", "")
            if not summary or "unavailable" in summary.lower():
                continue

            entry = f"**{date_str}:** {summary}"
            if section_key == "transactions":
                all_transactions.append(entry)
            elif section_key == "injuries":
                all_injuries.append(entry)
            elif section_key == "press_conferences":
                all_press.append(entry)
            elif section_key == "analysis":
                all_analysis.append(entry)

        for team, highlight in report.team_highlights.items():
            team_mentions[team] = team_mentions.get(team, 0) + 1
            text = highlight.get("summary", "") if isinstance(highlight, dict) else str(highlight)
            if text:
                team_summaries.setdefault(team, []).append(f"{date_str}: {text}")

        total_cost += float(report.llm_usage.get("estimated_cost_usd", 0) or 0)

    return {
        "transactions": all_transactions,
        "injuries": all_injuries,
        "press_conferences": all_press,
        "analysis": all_analysis,
        "team_mentions": team_mentions,
        "team_summaries": team_summaries,
        "total_cost": total_cost,
        "dates": dates,
    }


def _generate_digest_section(
    section_name: str,
    daily_entries: list[str],
    client,
    runtime: dict,
    usage_tracker: dict,
    days: int,
) -> str:
    """Generate a digest section from multiple days of data."""
    if not daily_entries:
        return f"No {section_name} data in the past {days} days."

    # Truncate if too many entries
    context = "\n\n".join(daily_entries[:20])

    prompt = f"""You are writing a {days}-day NFL digest. Synthesize the following daily {section_name} summaries into a single cohesive rollup.

Focus on:
- The biggest developments across the full period
- Patterns and trends (e.g., teams making multiple moves)
- The current state of things as of the most recent day

Do NOT list each day separately. Synthesize into themes and narratives.
Keep the response under 400 words.

Daily summaries:
{context}"""

    return _call_model(
        client, prompt, runtime,
        usage_tracker=usage_tracker,
        usage_label=f"digest:{section_name}",
    )


def _generate_team_digest(
    team: str,
    daily_summaries: list[str],
    client,
    runtime: dict,
    usage_tracker: dict,
) -> str:
    """Generate a team digest from multiple days."""
    context = "\n".join(daily_summaries[:10])

    prompt = f"""Synthesize these daily summaries for {team} into one cohesive team update.
Connect related developments into a narrative. Keep it under 150 words.

Daily summaries:
{context}"""

    return _call_model(
        client, prompt, runtime,
        max_tokens=400,
        usage_tracker=usage_tracker,
        usage_label=f"digest:team:{team}",
        reasoning_effort="low",
    )


def run_digest(days: int = 7):
    """Generate a multi-day digest report."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    available = list_available_reports()
    dates = available[:days]

    if len(dates) < 2:
        logger.error("Need at least 2 daily reports to generate a digest.")
        return

    logger.info("Generating %d-day digest from %s to %s...", len(dates), dates[-1], dates[0])

    data = _collect_digest_data(dates)
    runtime = _get_runtime_config()
    client = _get_client(runtime["provider"])
    usage_tracker = _init_usage_tracker(runtime)

    sections = {}
    for section_key in ("transactions", "injuries", "press_conferences", "analysis"):
        logger.info("Generating digest: %s...", section_key)
        sections[section_key] = _generate_digest_section(
            section_key,
            data[section_key],
            client, runtime, usage_tracker,
            days=len(dates),
        )

    # Generate team digests for teams mentioned 2+ times
    logger.info("Generating team digests...")
    team_highlights = {}
    top_teams = sorted(
        data["team_mentions"].items(),
        key=lambda x: x[1],
        reverse=True,
    )
    for team, count in top_teams:
        if count < 2:
            continue
        summaries = data["team_summaries"].get(team, [])
        if not summaries:
            continue
        team_highlights[team] = _generate_team_digest(
            team, summaries, client, runtime, usage_tracker,
        )

    # Build digest report
    digest = {
        "type": "digest",
        "days": len(dates),
        "date_range": {"start": dates[-1], "end": dates[0]},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sections": {
            k: {"summary": v} for k, v in sections.items()
        },
        "team_highlights": {
            k: {"summary": v} for k, v in team_highlights.items()
        },
        "team_mention_counts": dict(top_teams),
        "llm_usage": usage_tracker,
        "daily_cost_total": data["total_cost"],
    }

    # Save
    digest_dir = get_data_dir("reports")
    filename = f"digest_{dates[-1]}_to_{dates[0]}.json"
    path = digest_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(digest, f, indent=2, ensure_ascii=False)

    logger.info("Digest saved: %s", path)
    logger.info(
        "Covered %d days, %d team highlights, est. cost $%.4f",
        len(dates),
        len(team_highlights),
        float(usage_tracker.get("estimated_cost_usd", 0)),
    )

    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a multi-day NFL digest")
    parser.add_argument("--days", type=int, default=7, help="Number of days to cover")
    args = parser.parse_args()
    run_digest(args.days)
