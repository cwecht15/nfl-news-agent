"""Shared loaders for the in-season pages (Home, Roster State, Injury
Report, Inactives, Projection Audit).

Everything here reads files the in-season pipeline steps write
(data/weekly_projections, data/roster, data/injuries, data/inactives,
data/audit). No Sheets access.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from config_loader import get_data_dir, get_settings
from processing import season as season_mod


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def context():
    """Return ``(settings, ctx, schedule, week)`` for the current season.

    ``week`` prefers the active weekly-snapshot pointer (what the sheet
    says) and falls back to the schedule-derived week in ``ctx``.
    """
    settings = get_settings()
    ctx = season_mod.get_season_context(settings=settings)
    schedule = season_mod.load_schedule(settings=settings, season=ctx.season)
    pointer = active_pointer(ctx.season) or {}
    week = pointer.get("week") or ctx.week
    return settings, ctx, schedule, week


def require_in_season() -> None:
    """Banner + stop when ``season.phase`` is offseason.

    The sidebar (dashboard/nav.py) already hides the in-season pages in the
    offseason; this guards direct URL hits.
    """
    if season_mod.is_in_season(get_settings()):
        return
    st.info(
        "`season.phase` is **offseason**. Set `season.phase: in_season` in "
        "`config/settings.yaml` to enable weekly projection snapshots, roster "
        "state, the injury report tracker and the projection audit."
    )
    st.stop()


def active_pointer(season: int):
    return load_json(get_data_dir("weekly_projections") / str(season) / "active.json")


def roster_state():
    return load_json(get_data_dir("roster") / "state.json")


def events(limit: int = 400) -> list[dict]:
    p = get_data_dir("roster") / "events.jsonl"
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.sort(key=lambda e: (e.get("date", ""), e.get("observed_at", "")), reverse=True)
    return rows[:limit]


def _week_files(subdir: str, season: int) -> list[int]:
    d = get_data_dir(subdir) / str(season)
    weeks = []
    for p in d.glob("wk*.json"):
        try:
            weeks.append(int(p.stem[2:]))
        except ValueError:
            pass
    return sorted(weeks, reverse=True)


def injury_weeks(season: int) -> list[int]:
    return _week_files("injuries", season)


def injury_week(season: int, week: int):
    return load_json(get_data_dir("injuries") / str(season) / f"wk{week:02d}.json")


def inactives_weeks(season: int) -> list[int]:
    return _week_files("inactives", season)


def inactives_week(season: int, week: int):
    return load_json(get_data_dir("inactives") / str(season) / f"wk{week:02d}.json")


def odds_weeks(season: int) -> list[int]:
    return _week_files("odds", season)


def odds_week(season: int, week: int):
    return load_json(get_data_dir("odds") / str(season) / f"wk{week:02d}.json")


def source_stamps(season: int, week: int | None) -> dict[str, str]:
    """ISO "last updated" per refresh target, for the Refresh controls' captions.

    The timestamp is the only honest signal that a dispatched refresh landed:
    the workflow commits, Streamlit Cloud redeploys, and this value moves. Every
    entry comes from the data file itself, never from a file mtime — on a fresh
    cloud container an mtime is the checkout time, not the collection time.
    """
    stamps: dict[str, str] = {}
    state = roster_state() or {}
    if state.get("updated_at"):
        # run_roster_step writes the ledger and the state together, so one
        # timestamp covers both rosters and the elevations read into them.
        stamps["roster"] = state["updated_at"]
        stamps["elevations"] = state["updated_at"]

    nflverse_dir = get_data_dir("roster") / "nflverse"
    snaps = sorted(nflverse_dir.glob("*.json"), reverse=True) if nflverse_dir.exists() else []
    if snaps:
        snap = load_json(snaps[0]) or {}
        if snap.get("fetched_at"):
            stamps["nflverse"] = snap["fetched_at"]

    if week:
        inj = injury_week(season, week) or {}
        if inj.get("updated_at"):
            stamps["injuries"] = inj["updated_at"]
        ina = inactives_week(season, week) or {}
        if ina.get("updated_at"):
            stamps["inactives"] = ina["updated_at"]
        odds = odds_week(season, week) or {}
        if odds.get("updated_at"):
            stamps["odds"] = odds["updated_at"]

    # NFL.com transactions have no file of their own — the newest official
    # roster event is when they were last read.
    latest_tx = ""
    for e in events(2000):
        if e.get("source") == "nflcom_transactions":
            seen = str(e.get("observed_at") or "")
            if seen > latest_tx:
                latest_tx = seen
    stamps["transactions"] = latest_tx or state.get("updated_at", "")
    return {k: v for k, v in stamps.items() if v}
