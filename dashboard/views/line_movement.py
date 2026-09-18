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

from collectors.odds_collector import STAT_LABEL
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
props = data.get("props") or {}
changes = data.get("changes") or []


def _delta(new, old):
    if new is None or old is None:
        return None
    return round(new - old, 2)


def _arrow(d):
    if not d:
        return ""
    return f" {'▲' if d > 0 else '▼'}{abs(d):g}"


def _render_games() -> None:
    if not games:
        st.info("No game lines stored for this week yet.")
        return
    rows = []
    for key, g in sorted(games.items(), key=lambda kv: kv[1].get("kickoff_et", "")):
        cur, opened = g.get("current") or {}, g.get("opened") or {}
        sharp, sheet = g.get("sharp") or {}, g.get("sheet") or {}
        implied = g.get("implied") or {}
        sp_d = _delta(cur.get("spread_home"), opened.get("spread_home"))
        tot_d = _delta(cur.get("total"), opened.get("total"))
        rows.append({
            "Game": key,
            "Kickoff (ET)": g.get("kickoff_et", ""),
            "Spread (home)": f"{cur.get('spread_home')}{_arrow(sp_d)}",
            "Total": f"{cur.get('total')}{_arrow(tot_d)}",
            "Home ML": cur.get("home_ml"),
            "Away ML": cur.get("away_ml"),
            "Implied home": implied.get("home"),
            "Implied away": implied.get("away"),
            "Sharp spread": sharp.get("spread_home"),
            "Sharp total": sharp.get("total"),
            "Sheet spread": sheet.get("spread_home"),
            "Sheet O/U": sheet.get("ou"),
            "Flag": sheet.get("fp_flag") or "",
            "Books": cur.get("n_books"),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(
        "Arrows compare the current consensus with the line as first stored this week. "
        "**Flag** is the NFL Odds project's own FP-SPREAD / FP-TOTAL verdict: the "
        "projection sheet's line has drifted from the market."
    )

    with st.expander("Price series for one game"):
        pick = st.selectbox("Game", sorted(games), key="odds_game_series")
        hist = (games.get(pick) or {}).get("history") or []
        st.dataframe(
            [{"At": h.get("at"), "Spread (home)": h.get("spread_home"),
              "Total": h.get("total"), "Home ML": h.get("home_ml"),
              "Away ML": h.get("away_ml")} for h in hist],
            use_container_width=True, hide_index=True,
        )
        st.caption(
            "One row per change in the posted line. The NFL Odds project pulls "
            "about six times a week, so this series is that cadence, not the "
            "news agent's."
        )


_PROP_THR = ((_settings.get("odds", {}) or {}).get("thresholds") or {}).get("props") or {}


def _prop_rows() -> list[dict]:
    rows = []
    for p in props.values():
        cur, opened = p.get("current") or {}, p.get("opened") or {}
        prev = p.get("previous") or {}
        d_open = _delta(cur.get("mkt_mu"), opened.get("mkt_mu"))
        thr = float(_PROP_THR.get(p.get("stat", ""), 0) or 0)
        rows.append({
            # Move in units of the stat's threshold: ranks a 10-yard passing
            # move and a one-catch move on the same scale.
            "_size": abs(d_open or 0) / thr if thr else 0.0,
            "Player": p.get("player", ""),
            "Team": p.get("team", ""),
            "Opp": p.get("opp", ""),
            "Pos": p.get("pos", ""),
            "Stat": STAT_LABEL.get(p.get("stat", ""), p.get("stat", "")),
            "_stat": p.get("stat", ""),
            "Ours": p.get("ours"),
            "Line": cur.get("cons_line"),
            "Market": cur.get("mkt_mu"),
            "Δ vs open": _delta(cur.get("mkt_mu"), opened.get("mkt_mu")),
            "Δ last pull": _delta(cur.get("mkt_mu"), prev.get("mkt_mu")),
            "Flag": p.get("flag") or "",
            "Books": cur.get("n_books"),
            "Pulls": p.get("pulls"),
        })
    return rows


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
    rows = _prop_rows()
    if not rows:
        st.info("No player props stored for this week yet.")
        return
    rows = _filters(rows, "movers")
    only_moved = st.checkbox("Only lines that moved since open", value=True, key="odds_moved")
    if only_moved:
        rows = [r for r in rows if r["Δ vs open"]]
    rows.sort(key=lambda r: -r["_size"])
    st.caption(f"{len(rows)} player-stats")
    st.dataframe([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:500]],
                 use_container_width=True, hide_index=True)
    st.caption(
        "**Market** is the consensus-implied mean, not the posted line. "
        "Anytime TD is an expected-TD *rate* (1.20 = 1.2 expected TDs), not a probability."
    )


def _render_divergence() -> None:
    rows = [r for r in _prop_rows() if r["Flag"] in ("RED", "AMBER", "MKT-ONLY")]
    if not rows:
        st.info("Nothing flagged: the market agrees with the sheet everywhere it quotes.")
        return
    rows = _filters(rows, "diverge")
    order = {"RED": 0, "MKT-ONLY": 1, "AMBER": 2}
    rows.sort(key=lambda r: (order.get(r["Flag"], 9), -abs((r["Market"] or 0) - (r["Ours"] or 0))))
    st.dataframe([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:500]],
                 use_container_width=True, hide_index=True)
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


def _played_teams() -> set[str]:
    """News-style abbreviations of teams whose game this week is already over."""
    if week != ctx.week:
        return set()
    from processing.projection_audit import teams_already_played
    from processing.team_abbr import to_news
    return {to_news(t, "proj") for t in teams_already_played(_schedule, week, ctx.today)}


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
    table = [{
        "Player": r["player"], "Pos": r["pos"], "Team": r["team"], "Opp": r["opp"],
        "Stat": r["stat_label"], "You": r["ours"],
        "Market open": _fmt(r["market_open"], 2), "Market now": _fmt(r["market_now"], 2),
        "Gap (mkt − you)": _signed(r["gap_now"], 2),
        "Trend": {"away": "↗ away from you", "toward": "↘ toward you"}.get(r["trend"], ""),
        "Flag": r["flag"],
    } for r in rows]
    table = _filters(table, "wm")
    if table:
        st.dataframe(table[:300], use_container_width=True, hide_index=True)
    else:
        st.success("No player line is flagged RED or moving away from your projection.")
    st.caption("Anytime TD is an expected-TD *rate*, not a probability.")


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
