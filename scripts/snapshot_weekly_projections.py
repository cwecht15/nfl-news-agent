"""Weekly (in-season) projection snapshot CLI.

Thin wrapper over ``processing.weekly_projections`` — the offseason
``scripts/snapshot_projections.py`` is untouched and still owns
``data/projections/``. This one writes
``data/weekly_projections/<season>/wk<NN>/<sheet>/<date>/`` and keeps its
own ``changelog.csv`` + ``active.json`` pointer.

Usage:
    python scripts/snapshot_weekly_projections.py                  # snapshot + diff (weekday rule)
    python scripts/snapshot_weekly_projections.py --sheet both     # force both sheets
    python scripts/snapshot_weekly_projections.py --dry-run        # read + parse only, write nothing
    python scripts/snapshot_weekly_projections.py --diff           # last diff summary for the active sheet
    python scripts/snapshot_weekly_projections.py --date 2026-09-10

Sheet rule (no --sheet): primary always; secondary only on the weekdays in
``season.secondary_weekdays`` (Tuesday — next week's projections start there
while MNF finishes). Active working sheet = higher Current Week, tie → primary.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_settings
from processing import season as season_mod
from processing import weekly_projections as wp
from scripts.snapshot_projections import _get_client

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    # Metric labels include non-cp1252 characters (e.g. "Nudge TD Δ"); make
    # sure printing/logging them never crashes a Windows console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _resolve_sheets(arg: str | None, date_str: str, settings: dict) -> list[str]:
    if arg == "both":
        return ["primary", "secondary"]
    if arg in ("primary", "secondary"):
        return [arg]
    read_secondary = season_mod.read_secondary_today(date_str, settings)
    return ["primary"] + (["secondary"] if read_secondary else [])


def dry_run(sheets: list[str], date_str: str, settings: dict) -> int:
    """Read + parse each sheet and print what a snapshot would contain."""
    cfg = season_mod.get_in_season_projection_settings(settings)
    sheet_ids = cfg.get("sheets", {}) or {}
    gc = _get_client()
    metas: dict[str, dict] = {}
    print(f"Dry run for {date_str} ({season_mod.weekday_name(date_str)}) — nothing will be written\n")
    for label in sheets:
        sid = sheet_ids.get(label)
        if not sid:
            print(f"[{label}] not configured — skipped")
            continue
        try:
            parsed = wp.fetch_sheet(gc, label, sid, settings)
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] FAILED: {e}")
            continue
        meta = parsed["meta"]
        metas[label] = meta
        print(f"[{label}] season {meta.get('season')}  week {meta.get('week')}  ({sid})")
        for kind in wp.KINDS:
            print(f"    {kind:<8} {len(parsed[kind]):>5} rows")
        pos_counts = Counter(p.get("pos") for p in parsed["players"].values())
        status_counts = Counter(p.get("status") for p in parsed["players"].values())
        print(f"    players by pos: {dict(sorted(pos_counts.items()))}")
        print(f"    players by status: {dict(sorted(status_counts.items()))}")
        sample = next(iter(parsed["players"].values()), None)
        if sample:
            n_adj = sum(1 for k in sample["metrics"] if "adj" in k.lower())
            print(f"    metric columns: {len(sample['metrics'])} ({n_adj} Adj)")
        g_sample = next(iter(parsed["games"].values()), None)
        if g_sample:
            adj = [k for k in g_sample["metrics"] if "adj" in k.lower()]
            print(f"    game metric columns: {len(g_sample['metrics'])} (Adj: {', '.join(adj)})")
        print(f"    would write: {wp.snapshot_dir(meta.get('season'), meta.get('week') or 0, label, date_str)}")
        print()
    if not metas:
        print("No sheet could be read.")
        return 1
    week, active = season_mod.resolve_current_week(metas, None, date_str)
    print(f"Active sheet: {active} (week {week}); sheet weeks: "
          + ", ".join(f"{k}=wk{v.get('week')}" for k, v in metas.items()))
    return 0


def show_diff(settings: dict) -> int:
    """Print the most recent diff for the active sheet from the changelog."""
    season = season_mod.get_season_year(settings)
    ptr = wp.load_active_pointer(season)
    if not ptr:
        print(f"No active pointer for {season} — run a snapshot first.")
        return 1
    sheet, week = ptr.get("sheet"), ptr.get("week")
    rows = [r for r in wp.read_weekly_changelog()
            if r.get("sheet") == sheet and str(r.get("week")) == str(week)]
    if not rows:
        print(f"No changelog rows yet for {sheet} wk{int(week):02d} (baseline only).")
        return 0
    latest = max(r["date"] for r in rows)
    rows = [r for r in rows if r["date"] == latest]
    print(f"=== {sheet} sheet, week {week}, changes logged on {latest} ===")
    by_kind = Counter(r["kind"] for r in rows)
    print("by kind:", dict(by_kind))
    by_type = Counter((r["kind"], r["type"]) for r in rows)
    print("by type:", {f"{k}/{t}": n for (k, t), n in sorted(by_type.items())})

    adj = [r for r in rows if r["type"] == "metric_change" and "adj" in r["metric"].lower()]
    if adj:
        print(f"\n--- Adjustment tweaks ({len(adj)}) ---")
        for r in adj[:60]:
            print(f"  {r['kind']:<8} {r['label']:<24} {r['metric']}: {r['old_value']} -> {r['new_value']}")
        if len(adj) > 60:
            print(f"  ... and {len(adj) - 60} more")

    roster = [r for r in rows if r["type"] in ("added", "removed", "team_change")]
    if roster:
        print(f"\n--- Adds / removes / team changes ({len(roster)}) ---")
        for r in roster[:60]:
            extra = f"{r['old_value']} -> {r['new_value']}" if r["type"] == "team_change" else r["details"]
            print(f"  {r['kind']:<8} {r['label']:<24} {r['type']} {extra}")

    ranks = [r for r in rows if r["kind"] == "output" and r["metric"] == "POS Rank"]
    if ranks:
        print(f"\n--- Rank movers (adjusted players) ({len(ranks)}) ---")
        for r in ranks[:60]:
            print(f"  {r['label']:<24} {r['old_value']} -> {r['new_value']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly in-season projection snapshot")
    parser.add_argument("--sheet", choices=["primary", "secondary", "both"], default=None,
                        help="Which sheet(s) to read (default: primary, plus secondary on Tuesday)")
    parser.add_argument("--date", type=str, default=None, help="Snapshot date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Read + parse, print counts, write nothing")
    parser.add_argument("--diff", action="store_true", help="Show the last logged diff for the active sheet")
    parser.add_argument("--run", choices=["am", "pm"], default="am", help="Tag stored in meta/pointer")
    args = parser.parse_args()

    _setup_logging()
    settings = get_settings()
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    if args.diff:
        return show_diff(settings)

    sheets = _resolve_sheets(args.sheet, date_str, settings)
    if args.dry_run:
        return dry_run(sheets, date_str, settings)

    logger.info("Connecting to Google Sheets...")
    gc = _get_client()
    result = wp.run_weekly_snapshot(gc, date_str, settings=settings, sheets=sheets, run=args.run)

    print(f"\nWeek {result['week']} — active sheet: {result['active_sheet']} "
          f"({', '.join(f'{k}=wk{v}' for k, v in result['sheet_weeks'].items())})")
    for label, counts in result["changes"].items():
        basis = result["diff_basis"].get(label, {})
        basis_note = ""
        players_basis = basis.get("players") if isinstance(basis, dict) else None
        if players_basis:
            basis_note = f"  vs {players_basis['basis_sheet']} {players_basis['basis_date']}"
            if players_basis.get("fallback"):
                basis_note += " (other-sheet fallback)"
        print(f"  [{label}] changes: " + ", ".join(f"{k}={v}" for k, v in counts.items()) + basis_note)
        ctx = result["context_changes"].get(label, {})
        ctx_counts = {k: len(v) for k, v in ctx.items() if v}
        if ctx_counts:
            print(f"           context: {ctx_counts}")
    if result["errors"]:
        print(f"  errors: {result['errors']}")
    if result["rank_movers"]:
        print(f"  rank movers (active sheet): {len(result['rank_movers'])}")
    print(f"  active pointer -> {wp._active_path(result['snapshots'][result['active_sheet']]['season'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
