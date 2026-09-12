"""Home page.

In-season this is the week hub: week / day role / working sheet, this
week's games and byes, today's AM + evening run times, and the day's counts
(roster moves, injury changes, inactives, audit alerts) linking to the page
that works each one. In the offseason it falls back to the old landing
content (info line + PDF export).

The local pipeline runner lives in the sidebar of this page only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="NFL News Agent", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard import nav
from dashboard.helpers import to_et_display
from dashboard.pipeline_runner import render_pdf_export, render_sidebar_controls
from processing import season as season_mod

st.title("NFL News Agent")
st.markdown("Daily NFL news, transactions, press conferences, and analysis.")

# Sidebar "Run Pipeline" controls (local only). When a run is in progress
# or was just launched, the progress view owns the main pane.
if render_sidebar_controls():
    st.stop()


def _latest_report():
    from reports.report_builder import list_available_reports, load_report

    available = list_available_reports()
    if not available:
        return None
    try:
        return load_report(available[0])
    except Exception:  # noqa: BLE001 — a bad file shouldn't blank the hub
        return None


def _render_week_hub() -> None:
    from dashboard import in_season_data as isd
    from processing.projection_audit import latest_audit

    settings, ctx, schedule, week = isd.context()
    pointer = isd.active_pointer(ctx.season) or {}

    role = season_mod.day_role(ctx.today)
    st.subheader(f"Week {week or '—'} · {ctx.weekday} · {role}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Week", week or "—")
    c2.metric("Working sheet", (pointer.get("sheet") or ctx.active_sheet or "—").title())
    c3.metric("Today", f"{ctx.today} ({ctx.weekday})")
    c4.metric("Secondary read today", "yes" if ctx.read_secondary else "no")
    if pointer:
        st.caption(
            f"Latest sheet snapshot: {pointer.get('date')} ({pointer.get('run', 'am')}) · "
            f"{pointer.get('snapshot_at', '')}"
        )

    # ─── Today's report at a glance ───
    report = _latest_report()
    st.subheader("Today")
    if report is None:
        st.info("No daily report yet. The cloud pipeline runs at 10:00 UTC.")
    else:
        stamp = f"Report **{report.date}** · AM run {to_et_display(report.generated_at)}"
        if report.pm_updated_at:
            stamp += f" · evening update {to_et_display(report.pm_updated_at)}"
        else:
            stamp += " · evening update pending (22:00 UTC)"
        st.caption(stamp)

        inactives_count = (report.sections.get("game_day_inactives") or {}).get("count") or 0
        audit_alerts = report.audit_alerts or []
        audit_errors = sum(1 for a in audit_alerts if a.get("severity") == "error")
        audit_warnings = sum(1 for a in audit_alerts if a.get("severity") == "warning")

        line_moves = len((getattr(report, "odds", None) or {}).get("changes") or [])

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Roster moves", len(report.roster_events or []))
        m2.metric("Injury report changes", len(report.injury_changes or []))
        m3.metric("Inactives", inactives_count)
        m4.metric("Audit alerts", len(audit_alerts), delta=f"{audit_errors} errors · {audit_warnings} warnings", delta_color="off")
        m5.metric("Line moves", line_moves)

        l1, l2, l3, l4, l5, l6 = st.columns(6)
        l1.page_link(nav.DAILY_REPORT, label="Daily Report", icon="📰")
        l2.page_link(nav.ROSTER_STATE, label="Roster State", icon="🔁")
        l3.page_link(nav.INJURY_REPORT, label="Injury Report", icon="🩹")
        l4.page_link(nav.INACTIVES, label="Inactives", icon="🚫")
        l5.page_link(nav.PROJECTION_AUDIT, label="Projection Audit", icon="✅")
        l6.page_link(nav.LINE_MOVEMENT, label="Line Movement", icon="📉")

    audit_latest = latest_audit()
    if audit_latest:
        st.caption(
            f"Last audit: {audit_latest.get('date')} ({audit_latest.get('run')}) · "
            f"{len(audit_latest.get('alerts') or [])} open · "
            f"{len(audit_latest.get('dismissed') or [])} dismissed"
        )

    # ─── This week's games ───
    if week and schedule:
        games = season_mod.games_for_week(schedule, week)
        byes = sorted(season_mod.teams_on_bye(schedule, week))
        st.subheader(f"Week {week} games ({len(games)})")
        st.dataframe(
            [{"Date": g["date"], "Day": g.get("day", ""), "Time": g.get("time", ""),
              "Away": g["away"], "Home": g["home"], "Venue": g.get("venue", "")} for g in games],
            use_container_width=True, hide_index=True,
        )
        st.markdown(f"**Byes:** {', '.join(byes) if byes else 'none'}")
    else:
        st.info("Schedule not cached yet — the next pipeline run will fetch it from the Schedule tab.")


if season_mod.is_in_season():
    _render_week_hub()
else:
    st.info(
        "Select a page from the sidebar to get started. "
        "Reports are generated daily at 6:00 AM ET."
    )

st.divider()
render_pdf_export()
