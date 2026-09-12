"""In-season afternoon run — roster, injury report, projection audit.

The 6 AM ET daily pipeline runs before the day's practice reports post
(~4 PM ET) and before Saturday's practice-squad elevations. This light run
(no Team Notes / no LLM calls) refreshes the roster-facing pieces and
updates the day's report in place:

1. NFL.com transactions (36h lookback → data/raw/<date>/web_pm.json; the
   morning web.json is untouched)
2. nflverse roster snapshot + OurLads re-scrape (reserve-list crossings)
3. roster events / state  →  4. injury report tracker  →  5. projection audit
   (shared with the morning run via scripts.run_daily.run_in_season_steps)
6. re-read the active weekly projections sheet (no changelog rows)
7. load data/reports/<date>.json, replace the three in-season sections,
   stamp ``pm_updated_at`` and save (JSON + HTML). If the morning run never
   produced a report, the full afternoon run writes a skeleton so the sections
   still show; ``--inactives-only`` does not — a game-day poll is an update,
   not a producer, and the inactives are already saved under data/inactives/.

Exits 0 immediately when ``season.phase`` is ``offseason``.

Usage:
    python scripts/run_afternoon.py [--date YYYY-MM-DD] [--skip-ourlads] [--skip-transactions]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_data_dir, get_settings, get_teams_by_abbr
from models import DailyReport
from processing.season import get_season_context, load_schedule, today_et
from reports.report_builder import (
    _build_audit_section,
    _build_inactives_section,
    _build_injury_changes_section,
    _build_roster_moves_section,
    _trim_odds_payload,
    build_report,
    load_report,
    save_report,
)
from scripts.run_daily import (
    _inactive_rows, clear_status, run_in_season_steps, run_odds_step, setup_logging,
    write_status,
)


def _collect_pm_transactions(date_str: str, logger: logging.Logger, lookback_hours: int = 36) -> list:
    """Scrape NFL.com transactions again (today's rows) without touching web.json."""
    import json

    from collectors.web_scraper import _get_session, scrape_transactions_category_pages

    settings = get_settings()
    session = _get_session(settings)
    items = scrape_transactions_category_pages(
        session, settings, get_teams_by_abbr(), lookback_hours=lookback_hours,
    )
    out = get_data_dir("raw", date_str) / "web_pm.json"
    out.write_text(json.dumps([i.to_dict() for i in items], indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("PM transactions: %d rows -> %s", len(items), out)
    return items


def _rescrape_depth_charts(date_str: str, logger: logging.Logger) -> list[dict]:
    """Re-scrape OurLads and return today's reserve-list crossings."""
    from collectors.depth_chart_collector import (
        diff_depth_charts, load_latest_depth_charts, save_depth_charts,
        scrape_all_teams, split_reserve_changes,
    )

    prev = load_latest_depth_charts(before_date=date_str)
    cur = scrape_all_teams(delay=1.5)
    if not cur:
        logger.warning("OurLads re-scrape returned nothing; keeping the morning snapshot")
        return []
    save_depth_charts(cur, date_str)
    if not prev:
        return []
    _depth, status = split_reserve_changes(diff_depth_charts(cur, prev))
    logger.info("OurLads PM: %d reserve-list crossings vs previous snapshot", len(status))
    return status


def _refresh_active_sheet(date_str: str, ctx, logger: logging.Logger):
    """Re-read the active weekly sheet so the audit sees this afternoon's edits."""
    from processing.season import get_in_season_projection_settings
    from processing.weekly_projections import snapshot_sheet, write_active_pointer
    from scripts.snapshot_projections import _get_client

    label = ctx.active_sheet or "primary"
    sheets = get_in_season_projection_settings().get("sheets", {})
    sheet_id = sheets.get(label)
    if not sheet_id:
        logger.warning("No spreadsheet id configured for %s sheet", label)
        return ctx
    gc = _get_client()
    load_schedule(gc)
    snap = snapshot_sheet(gc, label, sheet_id, date_str, run="pm")
    meta = snap.get("meta") or {}
    write_active_pointer(ctx.season, {
        "date": date_str, "sheet": label, "week": meta.get("week"),
        "dir": meta.get("dir") or "", "snapshot_at": meta.get("snapshot_at"), "run": "pm",
    })
    logger.info("Refreshed %s sheet snapshot (week %s)", label, meta.get("week"))
    if meta.get("week"):
        ctx = get_season_context(today=date_str, sheet_metas={label: {"week": meta["week"]}})
    return ctx


def _odds_section(odds_week: dict | None, ctx, inactives_week: dict | None,
                  logger: logging.Logger, injury_changes: list | None = None,
                  roster_events: list | None = None) -> dict | None:
    """Line Movement section for the afternoon run — deterministic, no LLM.

    The PM run makes no model calls by design, so the paired-news lede is
    skipped and the section renders from the movers alone.
    """
    if not odds_week:
        return None
    try:
        from processing.odds_section import build_odds_section

        return build_odds_section(
            odds_week, [],
            injury_changes=injury_changes,
            roster_events=roster_events,
            inactives=_inactive_rows(ctx, inactives_week),
            use_llm=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Line Movement section failed (non-fatal): %s", e)
        return None


def _update_report(date_str: str, ctx, roster_events, injury_changes, audit_alerts, logger: logging.Logger,
                   inactives: dict | None = None, create_missing: bool = True,
                   line_movement: dict | None = None, odds: dict | None = None):
    """Fold this run's in-season results into ``data/reports/<date>.json``.

    ``None`` for any of ``roster_events`` / ``injury_changes`` / ``audit_alerts``
    / ``inactives`` / ``line_movement`` means "this run did not look at that,
    leave it alone" — an empty list means "we looked and found nothing". Both
    branches honour that distinction, so a section is only ever written when
    the run actually has something to say about it.

    ``create_missing=False`` refuses to author a report that does not exist yet.
    Game-day inactives polls pass it: an inactives poll is an update, not a
    producer, and on 2026-09-10 one authored a whole phantom report.
    """
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        report = load_report(date_str)
    except FileNotFoundError:
        if not create_missing:
            logger.warning(
                "No report for %s and this is an inactives-only run — not writing a "
                "skeleton. The inactives themselves are saved under data/inactives/. "
                "Run scripts/run_daily.py --date %s to produce the morning report.",
                date_str, date_str,
            )
            return None
        logger.warning("No morning report for %s — writing a skeleton with the in-season sections", date_str)
        report = build_report(
            date_str=date_str, sections={}, team_highlights={}, news_items=[],
            roster_events=roster_events, injury_changes=injury_changes,
            audit_alerts=audit_alerts, season_meta=ctx.to_dict(), inactives=inactives,
            line_movement=line_movement, odds=odds,
        )
        report.pm_updated_at = stamp
        save_report(report)
        return report

    sections = dict(report.sections)
    if roster_events is not None:
        merged = _merge_events(report.roster_events, roster_events)
        sections["roster_moves"] = _with_sources(_build_roster_moves_section(merged), sections.get("roster_moves"))
        report.roster_events = merged
    if injury_changes is not None:
        merged_inj = _merge_by_keys(report.injury_changes, injury_changes, ("team", "name", "type", "new"))
        sections["injury_report_changes"] = _with_sources(_build_injury_changes_section(merged_inj), sections.get("injury_report_changes"))
        report.injury_changes = merged_inj
    if audit_alerts is not None:
        sections["projection_audit"] = _with_sources(_build_audit_section(audit_alerts), sections.get("projection_audit"))
        report.audit_alerts = audit_alerts
    if inactives is not None:
        sections["game_day_inactives"] = _with_sources(_build_inactives_section(inactives), sections.get("game_day_inactives"))
        report.inactives = inactives
    if line_movement is not None:
        # Latest wins: the lines themselves are a snapshot, not an accumulation.
        sections["line_movement"] = _with_sources(line_movement, sections.get("line_movement"))
    if odds is not None:
        # Gated on the odds, not the section: `_odds_section` swallows a render
        # failure and returns None, and leaving the previous run's lines on a
        # report stamped with a fresh pm_updated_at would be a silent lie.
        report.odds = _trim_odds_payload(odds)

    from reports.report_builder import _ordered_sections
    report.sections = _ordered_sections(sections)
    report.season_meta = ctx.to_dict()
    report.pm_updated_at = stamp
    save_report(report)
    logger.info("Report %s updated in place (pm_updated_at=%s)", date_str, stamp)
    return report


def _with_sources(section: dict, previous: dict | None) -> dict:
    section = dict(section)
    section.setdefault("sources", (previous or {}).get("sources", []) if previous else [])
    return section


def _merge_events(existing: list[dict], new: list[dict]) -> list[dict]:
    seen = {e.get("event_id") for e in existing if e.get("event_id")}
    out = list(existing)
    for e in new:
        if e.get("event_id") and e["event_id"] in seen:
            continue
        out.append(e)
    return out


def _merge_by_keys(existing: list[dict], new: list[dict], keys: tuple[str, ...]) -> list[dict]:
    def k(d: dict):
        return tuple(str(d.get(x) or "") for x in keys)
    seen = {k(d) for d in existing}
    out = list(existing)
    for d in new:
        if k(d) in seen:
            continue
        out.append(d)
    return out


def run_pm(date_override: str | None = None, skip_ourlads: bool = False, skip_transactions: bool = False,
           backfill_from: str | None = None, inactives_only: bool = False) -> int:
    date_str = date_override or today_et()
    setup_logging(date_str)
    logger = logging.getLogger("afternoon")

    ctx = get_season_context(today=date_str)
    if not ctx.in_season:
        logger.info("season.phase is offseason — afternoon run has nothing to do.")
        return 0

    mode = "game-day inactives" if inactives_only else "Afternoon in-season update"
    write_status("PM", "running", mode)
    logger.info("=" * 60)
    logger.info("NFL News Agent - %s: %s (week %s, %s)", mode, date_str, ctx.week, ctx.weekday)
    logger.info("=" * 60)

    try:
        if inactives_only:
            # Game-day cron: poll ESPN for inactives near kickoff, re-run the
            # audit against the current sheet snapshot, refresh the report.
            # Lines move hardest on game day, so the inactives poll reads them too.
            odds_week = run_odds_step(date_str, ctx, logger)
            _, _, audit_alerts, inactives_week = run_in_season_steps(
                date_str=date_str, season_ctx=ctx, news_items=[], dc_status_changes=[],
                logger=logger, run="gameday", skip={"roster", "injuries"},
            )
            write_status("PM 4", "running", "Updating daily report")
            _update_report(date_str, ctx, None, None, audit_alerts, logger,
                           inactives=inactives_week, create_missing=False,
                           line_movement=_odds_section(odds_week, ctx, inactives_week, logger),
                           odds=odds_week)
            logger.info("Game-day inactives run complete.")
            return 0

        if backfill_from:
            # One-shot: seed the roster ledger from the raw NFL.com transaction
            # files already on disk (IR dates -> earliest return weeks).
            write_status("PM 0", "running", f"Backfilling roster events from {backfill_from}")
            try:
                from processing.roster_events import backfill_nflcom_events
                n = backfill_nflcom_events(backfill_from, date_str)
                logger.info("Roster ledger backfill from %s: %d events appended", backfill_from, n)
            except Exception as e:  # noqa: BLE001
                logger.warning("Roster backfill failed (non-fatal): %s", e)

        news_items: list = []
        if not skip_transactions:
            write_status("PM 1", "running", "Collecting NFL.com transactions")
            try:
                news_items = _collect_pm_transactions(date_str, logger)
            except Exception as e:  # noqa: BLE001
                logger.warning("PM transactions failed (non-fatal): %s", e)

        dc_status: list[dict] = []
        if not skip_ourlads:
            write_status("PM 2", "running", "Re-scraping depth charts")
            try:
                dc_status = _rescrape_depth_charts(date_str, logger)
            except Exception as e:  # noqa: BLE001
                logger.warning("OurLads re-scrape failed (non-fatal): %s", e)

        write_status("PM 3", "running", "Refreshing weekly sheet snapshot")
        try:
            ctx = _refresh_active_sheet(date_str, ctx, logger)
        except Exception as e:  # noqa: BLE001
            logger.warning("Weekly sheet refresh failed (non-fatal): %s", e)

        odds_week = run_odds_step(date_str, ctx, logger)

        roster_events, injury_changes, audit_alerts, inactives_week = run_in_season_steps(
            date_str=date_str, season_ctx=ctx, news_items=news_items,
            dc_status_changes=dc_status, logger=logger, run="pm",
        )

        write_status("PM 4", "running", "Updating daily report")
        _update_report(date_str, ctx, roster_events, injury_changes, audit_alerts, logger,
                       inactives=inactives_week,
                       line_movement=_odds_section(odds_week, ctx, inactives_week, logger,
                                                   injury_changes=injury_changes,
                                                   roster_events=roster_events),
                       odds=odds_week)
        logger.info("Afternoon run complete.")
    finally:
        clear_status()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="In-season afternoon update (roster / injuries / audit).")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--skip-ourlads", action="store_true")
    ap.add_argument("--skip-transactions", action="store_true")
    ap.add_argument("--backfill-from", default=None, metavar="YYYY-MM-DD",
                    help="one-shot: seed the roster ledger from data/raw/<date>/web.json since this date")
    ap.add_argument("--inactives-only", action="store_true",
                    help="game-day mode: ESPN inactives + projection audit + report refresh only")
    args = ap.parse_args()
    try:
        sys.exit(run_pm(args.date, args.skip_ourlads, args.skip_transactions, args.backfill_from,
                        inactives_only=args.inactives_only))
    except Exception:
        clear_status()
        raise
