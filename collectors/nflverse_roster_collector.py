"""nflverse roster snapshots — the GSIS-keyed daily roster baseline.

Why a fourth roster source: NFL.com's transaction feed never posts standard
practice-squad elevations, IR activations or designated-to-return rows, and
OurLads only shows the reserve lists as pseudo-positions. nflverse publishes
a daily ``roster_{year}.csv`` (one row per player, keyed by ``gsis_id``) that
carries the league's own status codes:

* ``status``: ACT (active), DEV (practice squad), RES (any reserve list),
  CUT (waived/released — the row stays with the *prior* ``status_description_abbr``),
  RET (retired), EXE (exempt).
* ``status_description_abbr``: A01 active; P01/P03/P06/P07 practice-squad
  variants; R01 / R48 injured reserve; R04 PUP; R05 NFI; R40 suspended;
  R02 retired; E02 exempt; W03 waived. R27 / R49 are rare reserve codes we
  treat as IR until proven otherwise.

Diffing two consecutive snapshots is the cheapest reliable way to see every
status flip league-wide; ``processing/roster_events.py`` turns those
transitions into ledger events. Snapshots live in
``data/roster/nflverse/<date>.json`` (gitignored locally, force-added on CI
like the other in-season data dirs).

Run directly to fetch + save today's snapshot and print status counts plus
the diff against the previous snapshot::

    python collectors/nflverse_roster_collector.py
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests

from config_loader import get_data_dir, get_settings
from processing.season import get_season_year
from processing.sheet_reconciliation import _normalize_name
from processing.team_abbr import to_news

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{year}.csv"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
FETCH_TIMEOUT = 60

# status_description_abbr -> roster-state vocabulary
# (ACT | PS | IR | PUP | NFI | SUS | EXE | RET | FA | UNKNOWN).
NFLVERSE_STATUS_LABELS: dict[str, str] = {
    "A01": "ACT",
    "P01": "PS",
    "P02": "PS",
    "P03": "PS",
    "P06": "PS",
    "P07": "PS",
    "R01": "IR",
    "R48": "IR",   # IR variant (seen on vested vets placed after cutdown) — IR either way
    "R27": "IR",   # rare reserve code; treated as IR until we see otherwise
    "R49": "IR",   # rare reserve code; treated as IR until we see otherwise
    "R04": "PUP",
    "R05": "NFI",
    "R02": "RET",
    "R40": "SUS",
    "R06": "SUS",
    "E02": "EXE",
    "W03": "FA",
}

# Coarse ``status`` -> label, used when the abbr is missing or when the
# status itself is decisive (CUT rows keep their pre-cut abbr).
_STATUS_FALLBACK: dict[str, str] = {
    "ACT": "ACT",
    "DEV": "PS",
    "CUT": "FA",
    "RET": "RET",
    "EXE": "EXE",
    "RES": "IR",
}

_RESERVE_LABELS = {"IR", "PUP", "NFI", "SUS", "RET", "EXE"}

_warned_abbrs: set[str] = set()


def status_label(status: str, abbr: str) -> str:
    """Resolve nflverse ``status`` + ``status_description_abbr`` to the
    roster-state vocabulary.

    ``status`` wins for the unambiguous buckets (CUT rows keep their old
    abbr, e.g. ``CUT/P01`` is a released practice-squad player); the abbr
    only disambiguates the reserve lists.
    """
    s = (status or "").strip().upper()
    a = (abbr or "").strip().upper()
    if s in ("ACT", "DEV", "CUT", "RET", "EXE"):
        return _STATUS_FALLBACK[s]
    if s == "RES":
        lab = NFLVERSE_STATUS_LABELS.get(a)
        if lab in _RESERVE_LABELS:
            return lab
        if a == "W03":
            return "IR"   # waived/injured reverting to IR — the abbr lags the status
        if a and a not in _warned_abbrs:
            _warned_abbrs.add(a)
            logger.warning("nflverse: unknown reserve abbr %r (status RES) — treating as IR", a)
        return "IR"
    lab = NFLVERSE_STATUS_LABELS.get(a)
    if lab:
        return lab
    if s and s not in _warned_abbrs:
        _warned_abbrs.add(s)
        logger.warning("nflverse: unknown status %r / abbr %r", s, a)
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Fetch + normalize
# ---------------------------------------------------------------------------


def _settings(settings: Optional[dict]) -> dict:
    return settings if settings is not None else get_settings()


def name_key(name: str) -> str:
    """Cross-source join key: ``sheet_reconciliation._normalize_name`` after
    folding curly apostrophes (tweets write D’Anthony, nflverse D'Anthony)."""
    return _normalize_name((name or "").replace("\u2019", "'").replace("\u2018", "'"))


def fetch_nflverse_roster(
    year: Optional[int] = None,
    settings: Optional[dict] = None,
    session: Optional[requests.Session] = None,
) -> list[dict]:
    """Download ``roster_{year}.csv`` and return the raw ``DictReader`` rows."""
    settings = _settings(settings)
    year = year or get_season_year(settings)
    url = str(settings.get("roster", {}).get("nflverse_url") or DEFAULT_URL).format(year=year)
    sess = session or requests.Session()
    logger.info("Fetching nflverse roster: %s", url)
    resp = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    logger.info("nflverse roster: %d rows (%d bytes)", len(rows), len(resp.content))
    return rows


def normalize_roster(rows: list[dict]) -> dict[str, dict]:
    """Raw CSV rows -> ``{gsis_id: player}``.

    Team abbreviations are converted to the news-style dialect used across
    this repo (nflverse ``LA`` -> ``LAR``). Rows without a ``gsis_id`` are
    skipped (they can't be joined to anything). Each player carries a
    resolved ``label`` (see :func:`status_label`) next to the raw
    ``status`` / ``status_abbr`` so downstream code never re-derives it.
    """
    players: dict[str, dict] = {}
    skipped = 0
    dupes = 0
    for row in rows:
        gsis = (row.get("gsis_id") or "").strip()
        if not gsis:
            skipped += 1
            continue
        if gsis in players:
            dupes += 1
            continue
        name = (row.get("full_name") or "").strip()
        status = (row.get("status") or "").strip().upper()
        abbr = (row.get("status_description_abbr") or "").strip().upper()
        week_raw = (row.get("week") or "").strip()
        players[gsis] = {
            "gsis_id": gsis,
            "esb_id": (row.get("esb_id") or "").strip(),
            "espn_id": (row.get("espn_id") or "").strip(),   # joins ESPN game-day rosters (inactives)
            "name": name,
            "name_key": name_key(name) if name else "",
            "team": to_news((row.get("team") or "").strip(), "nflverse"),
            "pos": (row.get("position") or "").strip().upper(),
            "depth_chart_position": (row.get("depth_chart_position") or "").strip().upper(),
            "status": status,
            "status_abbr": abbr,
            "label": status_label(status, abbr),
            "week": int(week_raw) if week_raw.isdigit() else None,
        }
    if skipped or dupes:
        logger.info("nflverse normalize: %d rows without gsis_id skipped, %d duplicate gsis rows",
                    skipped, dupes)
    return players


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _nflverse_dir() -> Path:
    d = get_data_dir("roster") / "nflverse"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_nflverse_snapshot(players: dict[str, dict], date_str: str) -> Path:
    path = _nflverse_dir() / f"{date_str}.json"
    payload = {
        "date": date_str,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(players),
        "players": players,
    }
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved nflverse snapshot: %d players -> %s", len(players), path)
    return path


def _read_snapshot(path: Path) -> Optional[dict[str, dict]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Unreadable nflverse snapshot %s: %s", path, e)
        return None
    if isinstance(payload, dict) and "players" in payload:
        return payload["players"]
    return payload if isinstance(payload, dict) else None


def load_nflverse_snapshot(date_str: str) -> Optional[dict[str, dict]]:
    path = _nflverse_dir() / f"{date_str}.json"
    if not path.exists():
        return None
    return _read_snapshot(path)


def latest_nflverse_snapshot(
    before_date: Optional[str] = None,
) -> tuple[Optional[dict[str, dict]], Optional[str]]:
    """``(players, date)`` for the newest snapshot — strictly older than
    ``before_date`` when given (so a same-day re-run diffs against yesterday,
    not against itself). ``(None, None)`` when nothing is on disk."""
    files = sorted(_nflverse_dir().glob("????-??-??.json"), reverse=True)
    if before_date:
        files = [f for f in files if f.stem < before_date]
    for f in files:
        players = _read_snapshot(f)
        if players is not None:
            return players, f.stem
    return None, None


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _transition(gsis: str, cur: Optional[dict], prev: Optional[dict], kind: str) -> dict:
    src = cur or prev or {}
    return {
        "gsis_id": gsis,
        "name": src.get("name", ""),
        "name_key": src.get("name_key", ""),
        "pos": src.get("pos", ""),
        "old_team": (prev or {}).get("team"),
        "new_team": (cur or {}).get("team"),
        "old_status": (prev or {}).get("status"),
        "new_status": (cur or {}).get("status"),
        "old_abbr": (prev or {}).get("status_abbr"),
        "new_abbr": (cur or {}).get("status_abbr"),
        "old_label": (prev or {}).get("label") if prev else None,
        "new_label": (cur or {}).get("label") if cur else None,
        "kind": kind,
    }


def diff_nflverse(cur: dict[str, dict], prev: dict[str, dict]) -> list[dict]:
    """Player-level transitions between two snapshots.

    ``kind`` is one of ``status`` (same team, status/abbr changed), ``team``
    (team changed — status fields are still populated so a combined
    move like CUT@LV -> DEV@DEN is visible in one record), ``added``
    (new gsis) or ``removed`` (gsis dropped from the file).
    """
    out: list[dict] = []
    for gsis, c in cur.items():
        p = prev.get(gsis)
        if p is None:
            out.append(_transition(gsis, c, None, "added"))
            continue
        if (c.get("team") or "") != (p.get("team") or ""):
            out.append(_transition(gsis, c, p, "team"))
        elif (c.get("status"), c.get("status_abbr")) != (p.get("status"), p.get("status_abbr")):
            out.append(_transition(gsis, c, p, "status"))
    for gsis, p in prev.items():
        if gsis not in cur:
            out.append(_transition(gsis, None, p, "removed"))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Fetch + save today's nflverse roster snapshot")
    ap.add_argument("--date", default=date.today().isoformat(), help="snapshot date (YYYY-MM-DD)")
    ap.add_argument("--year", type=int, default=None)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    rows = fetch_nflverse_roster(args.year)
    players = normalize_roster(rows)
    path = save_nflverse_snapshot(players, args.date)
    print(f"Saved {len(players)} players -> {path}")

    labels = Counter(p["label"] for p in players.values())
    print("Status counts:", ", ".join(f"{k}={v}" for k, v in sorted(labels.items())))
    raw = Counter(f"{p['status']}/{p['status_abbr']}" for p in players.values())
    print("Raw status/abbr:", ", ".join(f"{k}={v}" for k, v in sorted(raw.items())))

    prev, prev_date = latest_nflverse_snapshot(before_date=args.date)
    if prev is None:
        print("No previous snapshot to diff against.")
        return 0
    transitions = diff_nflverse(players, prev)
    kinds = Counter(t["kind"] for t in transitions)
    print(f"Diff vs {prev_date}: {len(transitions)} transitions "
          + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    for t in transitions[:40]:
        print(f"  [{t['kind']}] {t['name']} {t['old_team']}->{t['new_team']} "
              f"{t['old_status']}/{t['old_abbr']} -> {t['new_status']}/{t['new_abbr']}")
    if len(transitions) > 40:
        print(f"  ... {len(transitions) - 40} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
