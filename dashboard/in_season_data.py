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
