"""Line Movement page — market lines and player-prop movement (in-season only).

Reads ``data/odds/<season>/wkNN.json``, written by
``collectors.odds_collector`` from the sheets the NFL Odds project publishes.
Never touches Google Sheets itself: a page rerun must not spend a Sheets read.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Line Movement", page_icon="📉", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard import in_season_data as isd
from dashboard.helpers import to_et_display
from processing import line_insights as li

st.header("Line Movement")
isd.require_in_season()

_settings, ctx, _schedule, _week = isd.context()

weeks = isd.odds_weeks(ctx.season)
if not weeks:
    st.info(
        "No market lines yet. They are read from the NFL Odds project's sheets "
        "during the daily pipeline — nothing is pulled from a betting API here."
    )
    st.stop()

week = st.selectbox("Week", weeks, index=0, key="odds_week")
data = isd.odds_week(ctx.season, week) or {}
pull = data.get("pull") or {}

if pull.get("stale_reason"):
    st.warning(f"Lines are stale — {pull['stale_reason']}.")
else:
    age = pull.get("age_hours")
    st.caption(
        f"Odds pulled {pull.get('pulled_at') or 'unknown'}"
        + (f" ({age:g}h ago)" if age is not None else "")
        + f" · updated {to_et_display(data.get('updated_at'))}"
    )

games = data.get("games") or {}


NUM = st.column_config.NumberColumn


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


def _render_games() -> None:
    if not games:
        st.info("No game lines stored for this week yet.")
        return
    played = _played_teams()
    rows = []
    for key, g in games.items():
        home = li.game_card(data, g["home"])
        away = li.game_card(data, g["away"])
        if not home:
            continue
        sp_move = (home["spread_now"] - home["spread_open"]) if None not in (home["spread_now"], home["spread_open"]) else None
        tot_move = (home["total_now"] - home["total_open"]) if None not in (home["total_now"], home["total_open"]) else None
        rows.append({
            "Game": key, "Kickoff (ET)": g.get("kickoff_et", ""),
            "_final": g.get("home") in played,
            "Home spread": _pair(home["spread_open"], home["spread_now"], signed=True),
            "Spread move": sp_move,
            "Total": _pair(home["total_open"], home["total_now"]),
            "Total move": tot_move,
            "Away implied": _pair(away["implied_open"], away["implied_now"]),
            "Home implied": _pair(home["implied_open"], home["implied_now"]),
            "Your sheet": (f"{home['sheet_spread']:+g} / {home['sheet_total']:g}"
                           if None not in (home["sheet_spread"], home["sheet_total"]) else ""),
            "Sharp": (f"{home['sharp_spread']:+g} / {home['sharp_total']:g}"
                      if None not in (home["sharp_spread"], home["sharp_total"]) else ""),
            "Flag": home["fp_flag"],
            "_size": max(abs(sp_move or 0), abs(tot_move or 0) / 2),
        })
    # Upcoming games first, biggest movers first; a finished game's line is history.
    rows.sort(key=lambda r: (r["_final"], -r["_size"]))
    st.dataframe(
        [{**{k: v for k, v in r.items() if not k.startswith("_")},
          "Game": r["Game"] + (" (final)" if r["_final"] else "")} for r in rows],
        use_container_width=True, hide_index=True,
        column_config={"Spread move": NUM(format="%+.1f", help="Home spread now minus at open"),
                       "Total move": NUM(format="%+.1f", help="Total now minus at open")},
    )
    st.caption(
        "Open → now for every line; **Home spread** is negative when the home team is favored. "
        "Implied totals are what the spread and total say each team scores. **Your sheet** and "
        "**Sharp** (Pinnacle) are spread / total. **Flag** is the NFL Odds project's FP-SPREAD / "
        "FP-TOTAL verdict that your sheet's line has drifted from the market."
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


PROP_COLUMNS = {
    "Your proj": NUM(format="%.1f"), "Book line": NUM(format="%.1f"),
    "Market open": NUM(format="%.1f"), "Market now": NUM(format="%.1f"),
    "Move %": NUM(format="%+.0f%%", help="Market now vs where the line opened, in percent — "
                                         "comparable across stats (a catch and 15 yards read alike)"),
    "Last pull": NUM(format="%+.1f", help="Change in the most recent pull, in the stat's own units"),
    "Market vs you %": NUM(format="%+.0f%%", help="Market now vs your projection"),
}

PROP_CAPTION = (
    "**Market** = the betting consensus's implied average for the stat — not the posted O/U line, "
    "which is **Book line**. Anytime TD is shown as the chance of scoring (%). **Move %** and "
    "**Market vs you %** are relative, so receptions and yards compare on one scale."
)


def _prop_table_rows(rows: list[dict]) -> list[dict]:
    return [{"Player": r["player"], "Pos": r["pos"], "Team": r["team"], "Opp": r["opp"],
             "Stat": r["stat_label"], "Your proj": r["you"], "Book line": r["book_line"],
             "Market open": r["open"], "Market now": r["now"], "Move %": r["move_pct"],
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
    o1, o2 = st.columns(2)
    only_moved = o1.checkbox("Only lines that moved past their threshold since open", value=True,
                             key="odds_moved")
    thin = o2.checkbox("Include thin markets (1 book)", value=False, key="odds_thin")
    rows = li.prop_table(data, _settings, played=_played_teams(), only_moved=only_moved,
                         include_thin=thin)
    if not rows:
        st.info("No player line has moved past its threshold since open.")
        return
    table = _filters(_prop_table_rows(rows), "movers")
    st.caption(f"{len(table)} player lines · biggest moves first (relative to each stat's threshold) · "
               "finished games left out")
    st.dataframe(table[:500], use_container_width=True, hide_index=True, column_config=PROP_COLUMNS)
    st.caption(PROP_CAPTION)


def _render_divergence() -> None:
    order = {"RED": 0, "MKT-ONLY": 1, "AMBER": 2}
    rows = [r for r in li.prop_table(data, _settings, played=_played_teams(), only_moved=False)
            if r["flag"] in order]
    if not rows:
        st.info("Nothing flagged: the market agrees with the sheet everywhere it quotes.")
        return
    rows.sort(key=lambda r: (order[r["flag"]], -abs(r["vs_you_pct"] or 0)))
    table = _filters(_prop_table_rows(rows), "diverge")
    st.dataframe(table[:500], use_container_width=True, hide_index=True, column_config=PROP_COLUMNS)
    st.caption(
        "Flags come from the NFL Odds project's Market_Check, computed against "
        "calibrated per-stat bands. **RED** / **AMBER** = the market's implied mean "
        "sits outside the band around our projection. **MKT-ONLY** = the market quotes "
        "this stat and the sheet projects none — which includes a projected zero, so "
        "only the Projection Audit's `market_only_player` alert means the player is "
        "missing from the sheet entirely."
    )


def _fmt(v, nd=1):
    if v is None:
        return ""
    return f"{v:.{nd}f}" if abs(v) < 1000 else f"{v:,.0f}"


def _signed(v, nd=1):
    if v is None:
        return ""
    return f"{v:+.{nd}f}" if round(v, nd) else f"{0:.{nd}f}"


def _change_rows(rows: list[dict]) -> list[dict]:
    return [{"Type": c.get("type"), "Team": c.get("team"), "Player": c.get("player"),
             "Game": c.get("game"), "Message": (c.get("message") or "").replace("**", "")}
            for c in rows]


def _render_what_matters() -> None:
    played = _played_teams()
    counts = li.summary_counts(data, _settings, played=played)
    c1, c2, c3 = st.columns(3)
    c1.metric("Team totals moved 1+ pt", counts["teams_notable"])
    c2.metric("Your sheet off market 1+ pt", counts["sheet_off"])
    c3.metric("Player lines to re-check", counts["players_recheck"],
              help="RED flags plus lines that moved away from your projection since they opened.")

    st.subheader("Game environment")
    st.caption(
        "Implied team total = what the market's spread and total say a team scores. Your sheet's "
        "spread / O-U drives every player projection in that game, so **Sheet − market** is the "
        "number to fix; **Move** is how far the market has travelled since the line opened."
    )
    teams = li.team_environment(data, _settings, played=played)
    notable = [t for t in teams if t["notable"]]

    def _team_row(t: dict) -> dict:
        return {
            "Team": t["team"], "Opp": ("vs " if t["home"] else "@ ") + (t["opp"] or ""),
            "Kickoff (ET)": t["kickoff_et"],
            "Implied open": _fmt(t["implied_open"]), "Implied now": _fmt(t["implied_now"]),
            "Move": _signed(t["implied_move"]),
            "Your sheet": _fmt(t["sheet_implied"]), "Sheet − market": _signed(t["sheet_gap"]),
            "Spread (home) open → now": f"{_fmt(t['spread_open'])} → {_fmt(t['spread_now'])}",
            "Total open → now": f"{_fmt(t['total_open'])} → {_fmt(t['total_now'])}",
        }

    if played:
        st.caption(f"Already played this week (left out): {', '.join(sorted(played))}.")
    if notable:
        st.dataframe([_team_row(t) for t in notable], use_container_width=True, hide_index=True)
    else:
        st.success("No team total has moved a point since open, and your sheet is within a point "
                   "of the market in every game.")
    with st.expander(f"All {len(teams)} teams"):
        st.dataframe([_team_row(t) for t in teams], use_container_width=True, hide_index=True)

    st.subheader("Player lines vs your projection")
    st.caption(
        "**Away** = since it opened, the market moved further from your number (by at least the "
        "stat's movement threshold) — the lines worth re-checking. **RED** / **AMBER** are the NFL "
        "Odds project's calibrated verdicts on the gap itself. A line moving *toward* you confirms "
        "your projection and is left out unless you ask for it."
    )
    o1, o2 = st.columns(2)
    wider = o1.checkbox("Include AMBER and market-only flags", value=False, key="wm_wider")
    toward = o2.checkbox("Include lines moving toward you", value=False, key="wm_toward")
    rows = li.player_watchlist(data, _settings, include_toward=toward, played=played)
    trends = {"away", "toward"} if toward else {"away"}
    if not wider:
        rows = [r for r in rows if r["flag"] == "RED" or r["trend"] in trends]
    elif not toward:
        rows = [r for r in rows if r["trend"] != "toward"]
    table = []
    for r in rows:
        you, now = li.display_value(r["stat"], r["ours"]), li.display_value(r["stat"], r["market_now"])
        table.append({
            "Player": r["player"], "Pos": r["pos"], "Team": r["team"], "Opp": r["opp"],
            "Stat": li.stat_display_label(r["stat"]), "Your proj": you,
            "Market open": li.display_value(r["stat"], r["market_open"]), "Market now": now,
            "Market vs you %": ((now - you) / you * 100.0) if you else None,
            "Trend": {"away": "↗ away from you", "toward": "↘ toward you"}.get(r["trend"], ""),
            "Flag": r["flag"],
        })
    table = _filters(table, "wm")
    if table:
        st.dataframe(table[:300], use_container_width=True, hide_index=True, column_config=PROP_COLUMNS)
    else:
        st.success("No player line is flagged RED or moving away from your projection.")
    st.caption(PROP_CAPTION)


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


tab_wm, tab_games, tab_movers, tab_div, tab_pulls = st.tabs(
    ["What matters", "Game lines", "Prop movers", "Market vs projections", "Recent pulls"]
)
with tab_wm:
    _render_what_matters()
with tab_games:
    _render_games()
with tab_movers:
    _render_movers()
with tab_div:
    _render_divergence()
with tab_pulls:
    _render_recent_pulls()
