"""Sidebar navigation for the dashboard (``st.navigation``).

Streamlit's automatic ``pages/`` discovery sorts pages alphabetically and
can't hide or group anything. This module builds the grouped, phase-aware
sidebar instead:

* **This Week** — Daily Report, plus the in-season working pages
  (Injury Report, Inactives, Roster State, Projection Audit) that only
  exist when ``season.phase == in_season``.
* **Sources** — the on-demand LLM report tabs (Twitter / YouTube / Podcast).
* **Projections & Depth** — Projections, Depth Charts, Team View.
* **Tools** — Flagged, Trends, Digest; Transcripts + Config (local only);
  Depth Chart Manager (offseason only — its in-season work is deferred, see
  docs/depth_chart_manager_in_season.md); FantasyPoints (only while the
  collector is actually producing articles).

Once ``st.navigation`` runs, Streamlit ignores the ``pages/`` directory, so
only pages listed here are routable. ``url_path`` values match the old
filename stems so bookmarks keep working.
"""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from config_loader import get_data_dir
from dashboard.helpers import running_locally
from processing import season as season_mod

# Script paths are relative to the entrypoint (dashboard/app.py). They are
# also what ``st.page_link`` accepts, so pages can link to each other by
# these same strings.
HOME = "pages/home.py"
DAILY_REPORT = "pages/daily_report.py"
INJURY_REPORT = "pages/injury_report.py"
INACTIVES = "pages/inactives.py"
ROSTER_STATE = "pages/roster_state.py"
PROJECTION_AUDIT = "pages/projection_audit.py"
TWITTER_REPORT = "pages/twitter_report.py"
YT_REPORT = "pages/yt_report.py"
PODCAST_REPORT = "pages/podcast_report.py"
PROJECTIONS = "pages/projections.py"
DEPTH_CHARTS = "pages/depth_charts.py"
TEAM_VIEW = "pages/team_view.py"
FLAGGED = "pages/flagged.py"
TRENDS = "pages/trends.py"
DIGEST = "pages/digest.py"
TRANSCRIPTS = "pages/transcripts.py"
CONFIG = "pages/config.py"
DEPTH_CHART_MANAGER = "pages/depth_chart_manager.py"
FANTASYPOINTS = "pages/fantasypoints.py"


@st.cache_data(ttl=3600, show_spinner=False)
def _fantasypoints_has_recent_data(days: int = 14) -> bool:
    """True when any ``data/raw/<date>/fantasypoints.json`` in the last
    ``days`` days holds more than an empty list.

    The FantasyPoints collector has been returning ``[]`` since early
    August 2026 (expired auth, most likely). Hiding the page while the
    archive is empty keeps a dead tab out of the sidebar; it reappears on
    its own once articles flow again.
    """
    raw_dir = get_data_dir("raw")
    cutoff = date.today() - timedelta(days=days)
    for p in raw_dir.glob("*/fantasypoints.json"):
        try:
            d = date.fromisoformat(p.parent.name)
        except ValueError:
            continue
        if d < cutoff:
            continue
        try:
            if p.stat().st_size > 2:
                return True
        except OSError:
            continue
    return False


def _page(path: str, title: str, icon: str, *, default: bool = False):
    stem = path.rsplit("/", 1)[-1].removesuffix(".py")
    return st.Page(path, title=title, icon=icon, url_path=stem, default=default)


def build_navigation(hidden: bool = False):
    """Build the grouped sidebar and return the selected page (call ``.run()``).

    ``hidden`` keeps the sidebar menu off-screen (login screen). The call must
    still happen on every run — including the one that stops at the password
    form — because Streamlit keeps listing the ``pages/`` directory in the
    sidebar until ``st.navigation`` has executed.
    """
    in_season = season_mod.is_in_season()
    local = running_locally()

    this_week = [
        _page(HOME, "Home", "🏈", default=True),
        _page(DAILY_REPORT, "Daily Report", "📰"),
    ]
    if in_season:
        this_week += [
            _page(INJURY_REPORT, "Injury Report", "🩹"),
            _page(INACTIVES, "Inactives", "🚫"),
            _page(ROSTER_STATE, "Roster State", "🔁"),
            _page(PROJECTION_AUDIT, "Projection Audit", "✅"),
        ]

    sources = [
        _page(TWITTER_REPORT, "Twitter Report", "🐦"),
        _page(YT_REPORT, "YouTube Report", "📺"),
        _page(PODCAST_REPORT, "Podcast Report", "🎧"),
    ]

    projections = [
        _page(PROJECTIONS, "Projections", "📊"),
        _page(DEPTH_CHARTS, "Depth Charts", "📋"),
        _page(TEAM_VIEW, "Team View", "🏟️"),
    ]

    tools = [
        _page(FLAGGED, "Flagged", "🚩"),
        _page(TRENDS, "Trends", "📈"),
        _page(DIGEST, "Digest", "📚"),
    ]
    if local:
        tools += [
            _page(TRANSCRIPTS, "Transcripts", "📝"),
            _page(CONFIG, "Config", "⚙️"),
        ]
    if not in_season:
        tools.append(_page(DEPTH_CHART_MANAGER, "Depth Chart Manager", "🗂️"))
    if _fantasypoints_has_recent_data():
        tools.append(_page(FANTASYPOINTS, "FantasyPoints", "📄"))

    sections = {
        "This Week": this_week,
        "Sources": sources,
        "Projections & Depth": projections,
        "Tools": tools,
    }
    return st.navigation(
        sections, position="hidden" if hidden else "sidebar", expanded=True,
    )
