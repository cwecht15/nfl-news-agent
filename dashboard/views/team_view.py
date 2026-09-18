"""Team page — everything about one team in one place.

In-season: this week's game and how its line moved, the team's audit alerts,
injury report, inactives, projections, player lines, roster moves and depth
chart, then the team notes. Offseason: the team notes history (what this page
always showed). Kept at ``team_view`` so old bookmarks and ``?team=BUF``
links land here.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Team", page_icon="🏟️", layout="wide")

from dashboard.auth import require_password
require_password()

from config_loader import get_teams
from dashboard import in_season_data as isd
from dashboard import team_data as td
from dashboard.citations import build_citation_linker
from dashboard.helpers import (
    highlight_numbered_sources,
    highlight_sources,
    highlight_summary,
    render_numbered_sources,
    render_sources,
)
from processing import line_insights as li
from processing import season as season_mod
from processing.team_abbr import to_news, to_proj
from reports.report_builder import list_available_reports, load_report

NUM = st.column_config.NumberColumn

# ---------------------------------------------------------------------------
# Team picker (?team=BUF selects it; picking a team updates the URL)
# ---------------------------------------------------------------------------

teams = sorted(get_teams(), key=lambda t: t["name"])
abbrs = [t["abbr"] for t in teams]
names = {t["abbr"]: t["name"] for t in teams}
wanted = str(st.query_params.get("team", "")).upper()
team = st.selectbox("Team", abbrs, index=abbrs.index(wanted) if wanted in abbrs else 0,
                    format_func=lambda a: f"{names[a]} ({a})", key="team_pick")
if st.query_params.get("team") != team:
    st.query_params["team"] = team

st.header(names[team])

settings, ctx, schedule, week = isd.context()
in_season = season_mod.is_in_season(settings)


def _signed(v, nd=1):
    if v is None:
        return "—"
    return f"{v:+.{nd}f}" if round(v, nd) else f"{0:.{nd}f}"


def _team_notes(days: int, first_expanded: bool, skip_latest: bool = False,
                limit: int | None = None) -> None:
    """Team notes from the last ``days`` reports, newest first. ``limit``
    caps how many days render; ``skip_latest`` drops the newest day that has
    notes (shown under \"Team notes\" already)."""
    shown = 0
    skipped = not skip_latest
    for date_str in list_available_reports()[:days]:
        if limit is not None and shown >= limit:
            break
        try:
            report = load_report(date_str)
        except Exception:  # noqa: BLE001 — one unreadable report must not sink the page
            continue
        highlight = (report.team_highlights or {}).get(team)
        if not highlight or not highlight_summary(highlight):
            continue
        if not skipped:          # already shown under "Team notes"
            skipped = True
            continue
        with st.expander(date_str, expanded=first_expanded and shown == 0):
            numbered = highlight_numbered_sources(highlight)
            linkify = build_citation_linker(numbered)
            summary = highlight_summary(highlight)
            if linkify:
                st.markdown(linkify(summary), unsafe_allow_html=True)
            else:
                st.markdown(summary)
            if numbered:
                render_numbered_sources(numbered)
            else:
                render_sources(highlight_sources(highlight))
        shown += 1
    if not shown:
        st.info(f"No team notes for {names[team]} in the last {days} reports.")


if not in_season:
    days_back = st.slider("Days to show", 1, 30, 7)
    _team_notes(days_back, first_expanded=True)
    st.stop()

# ---------------------------------------------------------------------------
# This week
# ---------------------------------------------------------------------------

game = season_mod.opponent(schedule, to_proj(team), week) if schedule and week else None
odds = isd.odds_week(ctx.season, week) or {}
card = li.game_card(odds, team)
today = ctx.today or date.today().isoformat()
final = bool(game and str(game.get("date") or "") < today)

if game:
    where = "vs" if game.get("home_away") == "Home" else "@"
    when = card["kickoff_et"] if card else game.get("date", "")
    st.markdown(f"**Week {week}** · {where} **{to_news(game['opp'], 'proj')}** · {when}"
                + (" · _final_" if final else ""))
else:
    st.markdown(f"**Week {week}** · on bye")

if card:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spread", f"{card['spread_now']:+g}" if card["spread_now"] is not None else "—",
              delta=_signed(card["spread_now"] - card["spread_open"])
              if None not in (card["spread_now"], card["spread_open"]) else None,
              delta_color="inverse", help="This team's spread (negative = favored). Delta = move since the line opened.")
    c2.metric("Total", card["total_now"],
              delta=_signed(card["total_now"] - card["total_open"])
              if None not in (card["total_now"], card["total_open"]) else None,
              delta_color="off", help="Game over/under. Delta = move since open.")
    c3.metric("Implied team total", card["implied_now"],
              delta=_signed(card["implied_now"] - card["implied_open"])
              if None not in (card["implied_now"], card["implied_open"]) else None,
              help="Points the market's spread and total imply for this team. Delta = move since open.")
    gap = (card["sheet_implied"] - card["implied_now"]) if None not in (card["sheet_implied"], card["implied_now"]) else None
    c4.metric("Your sheet's team total", card["sheet_implied"] if card["sheet_implied"] is not None else "—",
              delta=(_signed(gap) + " vs market") if gap is not None else None, delta_color="off",
              help="Implied by your sheet's spread / O-U, which drives every projection in this game.")
    pull = odds.get("pull") or {}
    st.caption(
        f"Opened {card['spread_open']:+g} / {card['total_open']:g} · "
        f"sharp (Pinnacle) {card['sharp_spread']:+g} / {card['sharp_total']:g} · "
        f"your sheet {card['sheet_spread']:+g} / {card['sheet_total']:g}"
        + (f" · **{card['fp_flag']}**" if card["fp_flag"] else "")
        + f" · odds pulled {str(pull.get('pulled_at') or '?').replace('T', ' ')}"
        if None not in (card["spread_open"], card["sharp_spread"], card["sheet_spread"],
                        card["total_open"], card["sharp_total"], card["sheet_total"])
        else f"Odds pulled {str(pull.get('pulled_at') or '?').replace('T', ' ')}"
    )
elif game:
    st.caption("No market line stored for this game yet.")

# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

from processing.projection_audit import latest_audit  # noqa: E402

audit = latest_audit() or {}
alerts = td.audit_alerts(audit, team)
if alerts:
    st.subheader(f"Projection alerts ({len(alerts)})")
    for a in alerts:
        icon = {"error": "🔴", "warning": "🟠", "info": "🔵"}.get(a.get("severity"), "•")
        st.markdown(f"{icon} **{a.get('type', '').replace('_', ' ')}** — {a.get('message', '')}")
    st.caption(f"From the {audit.get('date')} {audit.get('run')} audit · dismiss on the Projection Audit page.")

# ---------------------------------------------------------------------------
# Latest news
# ---------------------------------------------------------------------------

st.subheader("Team notes")
_team_notes(7, first_expanded=True, limit=1)   # the newest report that has notes for this team

# ---------------------------------------------------------------------------
# Injuries + inactives
# ---------------------------------------------------------------------------

st.subheader("Injury report")
day_cols, inj = td.injury_rows(isd.injury_week(ctx.season, week), team)
if inj:
    st.dataframe(inj, use_container_width=True, hide_index=True)
else:
    st.caption("Nobody listed yet this week." if game else "On bye — no report.")
inactive = td.inactive_rows(isd.inactives_week(ctx.season, week), team)
if inactive:
    st.markdown("**Game-day inactives**")
    st.dataframe(inactive, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Game line history
# ---------------------------------------------------------------------------

if card and card["history"]:
    with st.expander(f"Line history — {len(card['history'])} posted changes", expanded=False):
        st.dataframe(
            [{"Pull": str(h["at"] or "").replace("T", " "), "Spread": h["spread"], "Total": h["total"],
              f"{team} implied": h["implied"], f"{card['opp']} implied": h["opp_implied"],
              "Moneyline": h["ml"]} for h in card["history"]],
            use_container_width=True, hide_index=True,
        )

# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------

from dashboard.projection_data import get_projection_source  # noqa: E402

st.subheader("Projections")
src = get_projection_source(settings)
proj_rows: list[dict] = []
dates = [d for d in src.dates() if src.week_for_date(d) == week] if hasattr(src, "week_for_date") else []
if dates:
    latest = dates[0]
    earlier = [d for d in src.dates_same_week(latest) if d < latest]
    prev_out = src.load(earlier[-1], "fantasy") if earlier else None
    proj_rows = td.projection_rows(src.load(latest, "fantasy"), src.load(latest, "players"), team, prev_out)
if proj_rows:
    st.dataframe(
        proj_rows, use_container_width=True, hide_index=True,
        column_config={"PPR": NUM(format="%.1f"),
                       "Δ PPR": NUM(format="%+.1f", help="Change since the week's previous sheet snapshot")},
    )
    st.caption(f"Your {src.sheet_for_date(latest) if hasattr(src, 'sheet_for_date') else ''} sheet, "
               f"snapshot {latest}" + (f" · Δ vs {earlier[-1]}" if earlier else ""))
else:
    st.caption("No weekly projection snapshot for this week yet.")

# ---------------------------------------------------------------------------
# Player lines
# ---------------------------------------------------------------------------

st.subheader("Player lines")
only_moved = st.checkbox("Only lines that moved since open", value=False, key="team_props_moved")
props = li.prop_table(odds, settings, team=team, only_moved=only_moved)
if props:
    rank = {r["Player"]: i for i, r in enumerate(proj_rows)}   # biggest projections first
    props.sort(key=lambda r: (rank.get(r["player"], 999), li.POS_ORDER.get(r["pos"], 9), r["player"],
                              li.STAT_ORDER.get(r["stat"], 99)))
    st.dataframe(
        [{"Player": r["player"], "Pos": r["pos"], "Stat": r["stat_label"], "Your proj": r["you"],
          "Book line": r["book_line"], "Market open": r["open"], "Market now": r["now"],
          "Move %": r["move_pct"], "Market vs you %": r["vs_you_pct"], "Flag": r["flag"]}
         for r in props],
        use_container_width=True, hide_index=True,
        column_config={
            "Your proj": NUM(format="%.1f"), "Book line": NUM(format="%.1f"),
            "Market open": NUM(format="%.1f"), "Market now": NUM(format="%.1f"),
            "Move %": NUM(format="%+.0f%%", help="Market now vs where the line opened, in percent — "
                                                 "comparable across stats"),
            "Market vs you %": NUM(format="%+.0f%%", help="Market now vs your projection"),
        },
    )
    st.caption("**Market** = the betting consensus's implied average for the stat (not the posted O/U "
               "line, which is **Book line**). Anytime TD is shown as the chance of scoring. "
               "Flags are the NFL Odds project's verdicts on market vs your projection.")
else:
    st.caption("No player lines stored for this team this week." if not only_moved
               else "No line has moved past its threshold since open.")

# ---------------------------------------------------------------------------
# Roster + depth chart
# ---------------------------------------------------------------------------

st.subheader("Roster")
left, right = st.columns(2)
with left:
    skill_only = st.checkbox("Skill positions only", value=True, key="team_roster_skill")
    off53 = td.roster_rows(isd.roster_state(), team, skill_only=skill_only)
    st.markdown(f"**Off the active roster** ({len(off53)})")
    st.dataframe(off53, use_container_width=True, hide_index=True)
with right:
    from processing.projection_audit import _week_window  # noqa: E402
    week_start, _end = _week_window(schedule, week) if schedule else (None, None)
    moves = td.event_rows(isd.events(1500), team, since=week_start)
    st.markdown(f"**Moves this week** ({len(moves)})")
    st.dataframe(moves, use_container_width=True, hide_index=True)

from collectors.depth_chart_collector import get_depth_chart_dates, load_depth_chart_by_date  # noqa: E402

dc_dates = get_depth_chart_dates()
with st.expander(f"Depth chart (OurLads{', ' + dc_dates[0] if dc_dates else ''})"):
    depth_rows = td.depth_chart(load_depth_chart_by_date(dc_dates[0]) if dc_dates else None, team)
    if depth_rows:
        st.dataframe(depth_rows, use_container_width=True, hide_index=True)
    else:
        st.caption("No depth chart snapshot.")

# ---------------------------------------------------------------------------
# Earlier notes
# ---------------------------------------------------------------------------

st.subheader("Earlier team notes")
days_back = st.slider("Days to show", 2, 30, 7, key="team_days")
_team_notes(days_back, first_expanded=False, skip_latest=True)
