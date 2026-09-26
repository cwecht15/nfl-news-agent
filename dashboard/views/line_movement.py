"""Line Movement page — market lines and player-prop movement (in-season only).

Reads ``data/odds/<season>/wkNN.json``, written by
``collectors.odds_collector`` from the sheets the NFL Odds project publishes.
Never touches Google Sheets itself: a page rerun must not spend a Sheets read.
The Refresh button dispatches ``refresh.yml --only odds``, which does the
sheet read on GitHub Actions and commits the week file.

"What moved" and "is my sheet off the market" are separate sections on
purpose: folded into one "notable" list, a team could be listed without having
moved, and a move under a point disappeared into a collapsed expander.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Line Movement", page_icon="📉", layout="wide")

from dashboard.auth import require_password
require_password()

import altair as alt
import pandas as pd

from dashboard import in_season_data as isd
from dashboard import refresh_controls as rc
from dashboard.helpers import to_et_display
from dashboard.prop_view import PROP_CAPTION, render_prop_table
from processing import line_insights as li

st.header("Line Movement")
isd.require_in_season()

_settings, ctx, _schedule, _week = isd.context()

rc.render_refresh(
    ("odds",), key="line_movement", label="Refresh lines",
    stamps=isd.source_stamps(ctx.season, ctx.week),
    help_note="Re-reads what the NFL Odds project last published — no Odds API call, no credits. "
              "New prices exist only after that project pulls (Tue 9a · Thu 4p · Sat 9p · "
              "Sun 11:45a + 7:30p · Mon 7:30p ET).",
)

weeks = isd.odds_weeks(ctx.season)
if not weeks:
    st.info(
        "No market lines yet. They are read from the NFL Odds project's sheets "
        "during the daily pipeline — nothing is pulled from a betting API here."
    )
    st.stop()

c_week, c_base = st.columns([1, 3])
week = c_week.selectbox("Week", weeks, index=0, key="odds_week")
data = isd.odds_week(ctx.season, week) or {}
pull = data.get("pull") or {}

BASELINE_LABELS = {"open": "Open", "window": "Yesterday's report", "last": "Latest pull"}
baseline = c_base.radio(
    "Moved since", list(BASELINE_LABELS), format_func=BASELINE_LABELS.get, horizontal=True,
    key="odds_baseline",
    help="Open = this week's opening line. Yesterday's report = what was known when the "
         "previous day's report ran. Latest pull = only what the most recent odds read brought in.",
)
since = None
if baseline == "window":
    from processing.odds_section import report_window_start
    since = report_window_start(ctx.today)

if pull.get("stale_reason"):
    st.warning(f"Lines are stale — {pull['stale_reason']}.")
else:
    age = pull.get("age_hours")
    st.caption(
        f"Odds pulled {pull.get('pulled_at') or 'unknown'}"
        + (f" ({age:g}h ago)" if age is not None else "")
        + f" · updated {to_et_display(data.get('updated_at'))}"
    )


def _baseline_note() -> str:
    if baseline == "window":
        return (f"Measured from what was known at yesterday's report ({to_et_display(since)})."
                if since else "No earlier report on file — measured from open.")
    if baseline == "last":
        log = data.get("pull_log") or []
        return (f"Only what the latest odds read brought in (first read {to_et_display(log[-1].get('seen_at'))})."
                if log else "No pull log in this week's file — measured from open.")
    return "Measured from each line's opening price this week."


games = data.get("games") or {}

NUM = st.column_config.NumberColumn
UP, DOWN = "#2e9e5b", "#d1495b"


def _played_teams() -> set[str]:
    """News-style abbreviations of teams whose game this week is already over."""
    if week != ctx.week:
        return set()
    from processing.projection_audit import teams_already_played
    from processing.team_abbr import to_news
    return {to_news(t, "proj") for t in teams_already_played(_schedule, week, ctx.today)}


def _pair(a, b, signed=False):
    """'-4.5 → -5.5' (or just the value when it never moved)."""
    fmt = (lambda v: f"{v:+g}") if signed else (lambda v: f"{v:g}")
    if a is None and b is None:
        return ""
    if a is None or b is None or a == b:
        return fmt(b if b is not None else a)
    return f"{fmt(a)} → {fmt(b)}"


def _num(v):
    """25.25 / 24 — implied totals land on quarter points."""
    return "" if v is None or pd.isna(v) else f"{round(v, 2):g}"


def _color_move(v):
    if v is None or pd.isna(v) or not v:
        return ""
    return f"color: {UP if v > 0 else DOWN}; font-weight: 600"


def _styled(df: pd.DataFrame, move_cols: list[str], num_cols: list[str]):
    sty = df.style
    painter = getattr(sty, "map", None) or sty.applymap      # pandas < 2.1 has only applymap
    sty = painter(_color_move, subset=move_cols)
    sty = sty.format(lambda v: "" if v is None or pd.isna(v) else f"{round(v, 2):+g}", subset=move_cols)
    return sty.format(_num, subset=num_cols)


# ---------------------------------------------------------------------------
# Movement tab
# ---------------------------------------------------------------------------


def _team_chart(rows: list[dict]) -> None:
    df = pd.DataFrame([{
        "Team": r["team"], "Move": r["move"],
        "Label": f"{r['move']:+g}",
        "Game": ("vs " if r["home"] else "@ ") + (r["opp"] or ""),
        "Implied": f"{_num(r['base'])} → {_num(r['now'])}",
        "Why": li.why_text(r),
    } for r in rows])
    order = df.sort_values("Move", ascending=False)["Team"].tolist()
    y = alt.Y("Team:N", sort=order, title=None)
    base = alt.Chart(df)
    bars = base.mark_bar().encode(
        y=y, x=alt.X("Move:Q", title="Implied team total, points moved"),
        color=alt.condition("datum.Move > 0", alt.value(UP), alt.value(DOWN)),
        tooltip=["Team", "Game", "Implied", "Move", "Why"],
    )
    right = base.transform_filter("datum.Move > 0").mark_text(align="left", dx=4).encode(
        y=y, x="Move:Q", text="Label:N")
    left = base.transform_filter("datum.Move < 0").mark_text(align="right", dx=-4).encode(
        y=y, x="Move:Q", text="Label:N")
    st.altair_chart((bars + right + left).properties(height=max(120, 24 * len(df))),
                    use_container_width=True)


def _render_teams(played: set[str]) -> None:
    st.subheader("Team implied totals")
    rows = li.team_movement(data, _settings, baseline=baseline, since=since, played=played)
    moved = [r for r in rows if r["move"]]
    up = sum(1 for r in moved if r["move"] >= 0.5)
    down = sum(1 for r in moved if r["move"] <= -0.5)
    c1, c2, c3 = st.columns(3)
    c1.metric("Teams up 0.5+ pts", up)
    c2.metric("Teams down 0.5+ pts", down)
    c3.metric("Biggest move", f"{moved[0]['team']} {moved[0]['move']:+g}" if moved else "—")

    if not moved:
        st.info("No team's implied total has moved over this window.")
    else:
        _team_chart(moved)
    unchanged = sorted(r["team"] for r in rows if not r["move"])
    notes = [_baseline_note()]
    if unchanged and moved:
        notes.append(f"Unchanged: {', '.join(unchanged)}.")
    if played:
        notes.append(f"Already played (left out): {', '.join(sorted(played))}.")
    st.caption(" ".join(notes))

    show_all = st.checkbox("Include unchanged teams in the table", value=False, key="lm_all_teams")
    table = rows if show_all else moved
    if table:
        df = pd.DataFrame([{
            "Team": r["team"], "Opp": ("vs " if r["home"] else "@ ") + (r["opp"] or ""),
            "Kickoff (ET)": r["kickoff_et"],
            "Implied then": r["base"], "Implied now": r["now"], "Move": r["move"],
            "Why": li.why_text(r),
            "Spread (team)": _pair(r["spread_base"], r["spread_now"], signed=True),
            "Total": _pair(r["total_base"], r["total_now"]),
        } for r in table])
        st.dataframe(_styled(df, ["Move"], ["Implied then", "Implied now"]),
                     use_container_width=True, hide_index=True,
                     column_config={"Why": st.column_config.Column(
                         help="The move split into its parts. An implied total is total/2 minus "
                              "spread/2 from the team's side: a total dropping 2 costs both "
                              "teams 1; a spread moving 1 toward a team gives it 0.5.")})


def _render_sheet_gap(played: set[str]) -> None:
    st.subheader("Your sheet vs the market")
    st.caption("Your sheet's spread / O-U drives every player projection in the game, so a team "
               "here is the one to fix. This is where things stand now, not a movement.")
    rows = li.sheet_vs_market(data, _settings, played=played)
    if not rows:
        st.success("Your sheet is within a point of the market's implied total for every team.")
        return
    df = pd.DataFrame([{
        "Team": r["team"], "Opp": ("vs " if r["home"] else "@ ") + (r["opp"] or ""),
        "Kickoff (ET)": r["kickoff_et"], "Market implied": r["implied_now"],
        "Your sheet": r["sheet_implied"], "Sheet − market": r["sheet_gap"], "Flag": r["fp_flag"],
    } for r in rows])
    st.dataframe(_styled(df, ["Sheet − market"], ["Market implied", "Your sheet"]),
                 use_container_width=True, hide_index=True)


_DIR = {"up": "▲", "down": "▼", "mixed": "↕"}
_VS = {"away": "↗ away from you", "toward": "↘ toward you", "mixed": "mixed"}
_SIZES = {"½× threshold": 0.5, "1× threshold": 1.0, "2× threshold": 2.0}


def _render_players(played: set[str]) -> None:
    st.subheader("Player lines that moved")
    f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
    size_label = f3.selectbox(
        "Minimum move", list(_SIZES), index=1, key="lm_size",
        help="In units of each stat's movement threshold (odds.thresholds) — e.g. 6 receiving "
             "yards, half a catch, 0.1 implied TDs — so different stats compare.")
    away_only = f4.checkbox("Only moves away from your projection", value=False, key="lm_away")
    rows = li.player_movement(data, _settings, baseline=baseline, since=since, played=played,
                              min_size=_SIZES[size_label])
    teams = f1.multiselect("Team", sorted({r["team"] for r in rows if r["team"]}), key="lm_team")
    positions = f2.multiselect("Position", sorted({r["pos"] for r in rows if r["pos"]}), key="lm_pos")
    if teams:
        rows = [r for r in rows if r["team"] in teams]
    if positions:
        rows = [r for r in rows if r["pos"] in positions]
    if away_only:
        rows = [r for r in rows if r["vs_you"] in ("away", "mixed")]
    if not rows:
        st.info("No player line moved that far over this window.")
        return

    st.caption(f"{len(rows)} players · biggest move first · {_baseline_note()}")
    st.dataframe(
        [{"Player": r["player"], "Pos": r["pos"], "Team": r["team"], "Opp": r["opp"],
          "Dir": _DIR[r["direction"]], "Moves": r["summary"], "Size": r["size"],
          "vs you": _VS.get(r["vs_you"], ""), "Flag": r["flag"]} for r in rows[:300]],
        use_container_width=True, hide_index=True,
        column_config={
            "Moves": st.column_config.Column(width="large",
                                             help="Each moved stat: market then → now (change)."),
            "Size": NUM(format="%.1f×", help="The largest move, in units of that stat's threshold."),
            "vs you": st.column_config.Column(
                help="Away = the move took the market further from your projection — the "
                     "lines to re-check. Toward = it confirms your number."),
        },
    )
    with st.expander("Per-stat detail"):
        detail = [{"Player": r["player"], "Pos": r["pos"], "Team": r["team"], "Opp": r["opp"],
                   "Stat": m["stat_label"], "_stat": m["stat"], "Your proj": m["ours"],
                   "Market then": m["base"], "Market now": m["now"], "Move %": m["move_pct"],
                   "Market vs you %": ((m["now"] - m["ours"]) / m["ours"] * 100.0) if m["ours"] else None,
                   "Flag": m["flag"]}
                  for r in rows[:300] for m in r["stats"]]
        render_prop_table(detail, caption=PROP_CAPTION)


def _render_movement() -> None:
    played = _played_teams()
    _render_teams(played)
    _render_sheet_gap(played)
    _render_players(played)


# ---------------------------------------------------------------------------
# Other tabs
# ---------------------------------------------------------------------------


def _render_games() -> None:
    if not games:
        st.info("No game lines stored for this week yet.")
        return
    played = _played_teams()
    moves = {(r["game"], r["home"]): r
             for r in li.team_movement(data, _settings, baseline=baseline, since=since)}
    rows = []
    for key, g in games.items():
        home, away = moves.get((key, True)), moves.get((key, False))
        card = li.game_card(data, g["home"])
        if not home or not card:
            continue
        sp_move = (home["spread_now"] - home["spread_base"]) if None not in (home["spread_now"], home["spread_base"]) else None
        tot_move = (home["total_now"] - home["total_base"]) if None not in (home["total_now"], home["total_base"]) else None
        rows.append({
            "Game": key, "Kickoff (ET)": g.get("kickoff_et", ""),
            "_final": g.get("home") in played,
            "Home spread": _pair(home["spread_base"], home["spread_now"], signed=True),
            "Spread move": sp_move,
            "Total": _pair(home["total_base"], home["total_now"]),
            "Total move": tot_move,
            "Away implied": _pair(_r2(away and away["base"]), _r2(away and away["now"])),
            "Home implied": _pair(_r2(home["base"]), _r2(home["now"])),
            "Your sheet": (f"{card['sheet_spread']:+g} / {card['sheet_total']:g}"
                           if None not in (card["sheet_spread"], card["sheet_total"]) else ""),
            "Sharp": (f"{card['sharp_spread']:+g} / {card['sharp_total']:g}"
                      if None not in (card["sharp_spread"], card["sharp_total"]) else ""),
            "Flag": card["fp_flag"],
            "_size": max(abs(sp_move or 0), abs(tot_move or 0) / 2),
        })
    # Upcoming games first, biggest movers first; a finished game's line is history.
    rows.sort(key=lambda r: (r["_final"], -r["_size"]))
    st.dataframe(
        [{**{k: v for k, v in r.items() if not k.startswith("_")},
          "Game": r["Game"] + (" (final)" if r["_final"] else "")} for r in rows],
        use_container_width=True, hide_index=True,
        column_config={"Spread move": NUM(format="%+.1f", help="Home spread now minus at the baseline"),
                       "Total move": NUM(format="%+.1f", help="Total now minus at the baseline")},
    )
    st.caption(
        f"Then → now for every line. {_baseline_note()} **Home spread** is negative when the home "
        "team is favored. Implied totals are what the spread and total say each team scores. "
        "**Your sheet** and **Sharp** (Pinnacle) are spread / total. **Flag** is the NFL Odds "
        "project's FP-SPREAD / FP-TOTAL verdict that your sheet's line has drifted from the market."
    )

    with st.expander("Line history for one game"):
        pick = st.selectbox("Game", sorted(games), key="odds_game_series")
        card = li.game_card(data, (games.get(pick) or {}).get("home", ""))
        st.dataframe(
            [{"Pull": str(h["at"] or "").replace("T", " "), "Home spread": h["spread"],
              "Total": h["total"], "Home implied": h["implied"], "Away implied": h["opp_implied"],
              "Home ML": h["ml"]} for h in (card or {}).get("history") or []],
            use_container_width=True, hide_index=True,
        )
        st.caption("One row per change in the posted line, at the NFL Odds project's pull cadence "
                   "(about six pulls a week).")


def _r2(v):
    return None if v is None else round(v, 2)


def _prop_table_rows(rows: list[dict], then_label: str = "Market open") -> list[dict]:
    return [{"Player": r["player"], "Pos": r["pos"], "Team": r["team"], "Opp": r["opp"],
             "Stat": r["stat_label"], "_stat": r["stat"], "Your proj": r["you"], "Book line": r["book_line"],
             then_label: r["open"], "Market now": r["now"], "Move %": r["move_pct"],
             "Last pull": r["last_pull"], "Market vs you %": r["vs_you_pct"], "Flag": r["flag"]}
            for r in rows]


def _filters(rows: list[dict], key: str) -> list[dict]:
    c1, c2, c3 = st.columns(3)
    teams = sorted({r["Team"] for r in rows if r["Team"]})
    positions = sorted({r["Pos"] for r in rows if r["Pos"]})
    stats = sorted({r["Stat"] for r in rows if r["Stat"]})
    t = c1.multiselect("Team", teams, key=f"{key}_team")
    p = c2.multiselect("Position", positions, key=f"{key}_pos")
    s = c3.multiselect("Stat", stats, key=f"{key}_stat")
    out = rows
    if t:
        out = [r for r in out if r["Team"] in t]
    if p:
        out = [r for r in out if r["Pos"] in p]
    if s:
        out = [r for r in out if r["Stat"] in s]
    return out


def _render_movers() -> None:
    choices = {"All lines": 0.0, "Moved 5%+": 5.0, "Moved 10%+": 10.0, "Moved 20%+": 20.0}
    o1, o2 = st.columns([3, 1])
    show = o1.radio("Show", list(choices), index=1, horizontal=True, key="odds_moved",
                    help="Move since the baseline chosen above, in percent. Implied TDs also need a 0.02 move.")
    thin = o2.checkbox("Include thin markets (1 book)", value=False, key="odds_thin")
    rows = li.prop_table(data, _settings, played=_played_teams(), min_move_pct=choices[show],
                         include_thin=thin, baseline=baseline, since=since)
    if not rows:
        st.info(f"No player line has moved {choices[show]:g}% or more over this window.")
        return
    table = _filters(_prop_table_rows(rows, "Market then"), "movers")
    st.caption(f"{len(table)} player lines · biggest moves first (relative to each stat's threshold) · "
               f"finished games left out · {_baseline_note()}")
    render_prop_table(table[:500], caption=PROP_CAPTION)


def _render_divergence() -> None:
    order = {"RED": 0, "MKT-ONLY": 1, "AMBER": 2}
    rows = [r for r in li.prop_table(data, _settings, played=_played_teams(), only_moved=False)
            if r["flag"] in order]
    if not rows:
        st.info("Nothing flagged: the market agrees with the sheet everywhere it quotes.")
        return
    rows.sort(key=lambda r: (order[r["flag"]], -abs(r["vs_you_pct"] or 0)))
    table = _filters(_prop_table_rows(rows), "diverge")
    render_prop_table(table[:500])
    st.caption(
        "Flags come from the NFL Odds project's Market_Check, computed against "
        "calibrated per-stat bands. **RED** / **AMBER** = the market's implied mean "
        "sits outside the band around our projection. **MKT-ONLY** = the market quotes "
        "this stat and the sheet projects none — which includes a projected zero, so "
        "only the Projection Audit's `market_only_player` alert means the player is "
        "missing from the sheet entirely."
    )


def _change_rows(rows: list[dict]) -> list[dict]:
    return [{"Type": c.get("type"), "Team": c.get("team"), "Player": c.get("player"),
             "Game": c.get("game"), "Message": (c.get("message") or "").replace("**", "")}
            for c in rows]


def _render_recent_pulls() -> None:
    batches = li.pull_batches(data)
    if batches:
        for i, b in enumerate(batches):
            chg = b.get("changes") or []
            label = (f"Pull {str(b.get('pulled_at') or '?').replace('T', ' ')} · first seen "
                     f"{to_et_display(b.get('seen_at'))} · {len(chg)} change{'s' if len(chg) != 1 else ''}")
            with st.expander(label, expanded=(i == 0)):
                if chg:
                    st.dataframe(_change_rows(chg), use_container_width=True, hide_index=True)
                else:
                    st.write("Nothing moved past the reporting thresholds in this pull.")
        st.caption("Each odds pull's movement versus the pull before it. The daily report's Line "
                   "Movement section covers every pull since the previous report.")
        return
    # A week file from before pulls were logged: derive the latest pull's moves
    # from stored state instead of showing nothing.
    moves = li.latest_pull_moves(data, _settings)
    at = str(pull.get("pulled_at") or "?").replace("T", " ")
    if moves:
        st.markdown(f"**Latest pull ({at})** vs the pull before it — {len(moves)} moves")
        st.dataframe(_change_rows(moves), use_container_width=True, hide_index=True)
    else:
        st.info(f"Nothing moved past the reporting thresholds in the latest pull ({at}).")


tab_move, tab_games, tab_movers, tab_div, tab_pulls = st.tabs(
    ["Movement", "Game lines", "All player lines", "Market vs projections", "Recent pulls"]
)
with tab_move:
    _render_movement()
with tab_games:
    _render_games()
with tab_movers:
    _render_movers()
with tab_div:
    _render_divergence()
with tab_pulls:
    _render_recent_pulls()
