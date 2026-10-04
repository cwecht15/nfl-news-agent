"""Freeze the Week-1 depth baseline used by the positional attrition view.

Reads (dates/snapshot from ``config/settings.yaml → attrition``):
  * data/depth_charts/<baseline_depth_date>.json           OL / DL / LB / DB slots
  * data/weekly_projections/<season>/<baseline_proj_snapshot>/players.json   QB/RB/WR/TE ranks
  * data/roster/nflverse/<baseline_nflverse_date>.json     53-man filter + gsis ids
  * data/attrition/<season>/baseline_overrides.json        optional {team: {label: name}}

Writes data/attrition/<season>/baseline.json. Re-runnable; commit the result.

    python scripts/build_attrition_baseline.py
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.depth_chart_collector import load_depth_chart_by_date
from config_loader import get_data_dir, get_settings
from processing import attrition
from processing.season import get_season_year

logger = logging.getLogger("build_attrition_baseline")


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    cfg = attrition.config(settings)
    season = get_season_year(settings)

    depth = load_depth_chart_by_date(cfg["baseline_depth_date"])
    proj = _read(get_data_dir("weekly_projections") / str(season) / cfg["baseline_proj_snapshot"] / "players.json")
    nflverse = _read(get_data_dir("roster") / "nflverse" / f"{cfg['baseline_nflverse_date']}.json")
    for label, val in (("depth chart", depth), ("Week-1 sheet", proj), ("nflverse", nflverse)):
        if not val:
            logger.error("Missing %s input — check the attrition.* dates in settings.yaml", label)
            return 1

    out_dir = get_data_dir("attrition") / str(season)
    out_dir.mkdir(parents=True, exist_ok=True)
    overrides = _read(out_dir / "baseline_overrides.json") or {}
    meta = {
        "season": season,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {"depth_chart": cfg["baseline_depth_date"],
                    "projection_sheet": cfg["baseline_proj_snapshot"],
                    "nflverse": cfg["baseline_nflverse_date"]},
    }
    base = attrition.build_baseline(depth, proj, nflverse, overrides, settings, meta)

    path = out_dir / "baseline.json"
    path.write_text(json.dumps(base, indent=1, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote %s (%d teams)", path, len(base["teams"]))
    for r in base["repairs"]:
        logger.info("  repaired: %s", r)

    # Sanity: starters per unit per team; anything unusual is worth a look.
    expect = {"OL": (5, 5), "CB": (2, 3), "S": (2, 2), "QB": (1, 1), "WR": (3, 3), "TE": (1, 1), "RB": (1, 1)}
    for team, entries in base["teams"].items():
        c = Counter(e["unit"] for e in entries if e["starter"])
        odd = [f"{u}={c.get(u, 0)}" for u, (lo, hi) in expect.items() if not lo <= c.get(u, 0) <= hi]
        front = c.get("IDL", 0) + c.get("EDGE", 0)
        if not 3 <= front <= 5:
            odd.append(f"DL={front}")
        if odd:
            logger.warning("  check %s: %s", team, ", ".join(odd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
