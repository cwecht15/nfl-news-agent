"""In Season page — week overview, roster state, injury report, projection audit.

Everything here reads files the in-season pipeline steps write
(data/weekly_projections, data/roster, data/injuries, data/audit). When
``season.phase`` is ``offseason`` the page only shows a banner.
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="In Season", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from config_loader import get_data_dir, get_settings
from processing import season as season_mod
from processing.projection_audit import (
    dismiss as audit_dismiss,
    latest_audit,
    load_dismissals as load_audit_dismissals,
    undismiss as audit_undismiss,
)

st.title("In Season")

settings = get_settings()
if not season_mod.is_in_season(settings):
    st.info(
        "`season.phase` is **offseason**. Set `season.phase: in_season` in "
        "`config/settings.yaml` to enable weekly projection snapshots, roster "
        "state, the injury report tracker and the projection audit."
    )
    st.stop()

ctx = season_mod.get_season_context(settings=settings)
schedule = season_mod.load_schedule(settings=settings, season=ctx.season)


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _active_pointer():
    return _load_json(get_data_dir("weekly_projections") / str(ctx.season) / "active.json")


def _roster_state():
    return _load_json(get_data_dir("roster") / "state.json")


def _events(limit: int = 400) -> list[dict]:
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


def _injury_weeks() -> list[int]:
    d = get_data_dir("injuries") / str(ctx.season)
    weeks = []
    for p in d.glob("wk*.json"):
        try:
            weeks.append(int(p.stem[2:]))
        except ValueError:
            pass
    return sorted(weeks, reverse=True)


def _injury_week(week: int):
    return _load_json(get_data_dir("injuries") / str(ctx.season) / f"wk{week:02d}.json")


pointer = _active_pointer() or {}
week = pointer.get("week") or ctx.week

tab_week, tab_roster, tab_injury, tab_audit = st.tabs(
    ["Week", "Roster State", "Injury Report", "Projection Audit"]
)

# ─── Week ───
with tab_week:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Week", week or "—")
    c2.metric("Working sheet", (pointer.get("sheet") or ctx.active_sheet or "—").title())
    c3.metric("Today", f"{ctx.today} ({ctx.weekday})")
    c4.metric("Secondary read today", "yes" if ctx.read_secondary else "no")
    st.caption(season_mod.day_role(ctx.today))
    if pointer:
        st.caption(f"Latest sheet snapshot: {pointer.get('date')} ({pointer.get('run', 'am')}) · {pointer.get('snapshot_at', '')}")

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

    audit_latest = latest_audit()
    if audit_latest:
        st.subheader("Last audit")
        st.markdown(
            f"{audit_latest.get('date')} ({audit_latest.get('run')}) · "
            f"{len(audit_latest.get('alerts') or [])} open · "
            f"{len(audit_latest.get('dismissed') or [])} dismissed"
        )

# ─── Roster State ───
with tab_roster:
    state = _roster_state()
    if not state:
        st.info("No roster state yet (data/roster/state.json). It is built by the daily pipeline in-season.")
    else:
        players = state.get("players") or {}
        st.caption(
            f"Updated {state.get('updated_at', '')} · baseline {state.get('baseline', {}).get('source', '')} "
            f"{state.get('baseline', {}).get('date', '')} · {len(players)} players"
        )
        all_teams = sorted({p.get("team", "") for p in players.values() if p.get("team")})
        all_status = sorted({p.get("status", "") for p in players.values() if p.get("status")})
        f1, f2, f3 = st.columns(3)
        team_f = f1.multiselect("Team", all_teams, key="rs_team")
        status_f = f2.multiselect("Status", all_status, default=[s for s in ("IR", "PUP", "NFI", "SUS", "PS") if s in all_status], key="rs_status")
        pos_f = f3.multiselect("Position", ["QB", "RB", "WR", "TE", "K"], default=["QB", "RB", "WR", "TE", "K"], key="rs_pos")

        rows = []
        for gid, p in players.items():
            if team_f and p.get("team") not in team_f:
                continue
            if status_f and p.get("status") not in status_f:
                continue
            if pos_f and p.get("pos") not in pos_f:
                continue
            rows.append({
                "Player": p.get("name", ""), "Team": p.get("team", ""), "Pos": p.get("pos", ""),
                "Status": p.get("status", ""), "Since": p.get("status_since", "") or "",
                "Source": p.get("status_source", "") or "",
                "IR date": p.get("ir_date", "") or "",
                "Eligible Wk": p.get("earliest_return_week", "") or "",
                "Designated": (p.get("designated_return_date") or "")[:10],
                "Elev used": p.get("elevations_used", 0),
                "Pending": len(p.get("pending") or []),
            })
        rows.sort(key=lambda r: (r["Team"], r["Status"], r["Player"]))
        st.markdown(f"**{len(rows)}** players")
        st.dataframe(rows, use_container_width=True, hide_index=True)

        st.subheader("Recent events")
        ev_rows = []
        for e in _events(300):
            if team_f and e.get("team") not in team_f:
                continue
            ev_rows.append({
                "Date": e.get("date", ""), "Player": e.get("name", ""), "Team": e.get("team", ""),
                "Pos": e.get("pos", ""), "Event": (e.get("event_type") or "").replace("_", " "),
                "Detail": (e.get("detail") or "")[:60],
                "Source": (e.get("source") or "").replace("news:", ""),
                "Confidence": e.get("confidence", ""),
            })
        st.dataframe(ev_rows[:200], use_container_width=True, hide_index=True)

# ─── Injury Report ───
with tab_injury:
    weeks = _injury_weeks()
    if not weeks:
        st.info("No injury report files yet (data/injuries/<season>/wkNN.json).")
    else:
        wk = st.selectbox("Week", weeks, index=0, key="ir_week")
        data = _injury_week(wk) or {}
        st.caption(
            f"Updated {data.get('updated_at', '')} · sources "
            f"{', '.join(f'{k}={v}' for k, v in sorted((data.get('sources_used') or {}).items()))}"
        )
        teams = data.get("teams") or {}
        team_pick = st.multiselect("Team", sorted(teams), key="ir_team")
        practice_dates = sorted({d for t in teams.values() for p in (t.get("players") or {}).values() for d in (p.get("practice") or {})})
        rows = []
        for team, t in sorted(teams.items()):
            if team_pick and team not in team_pick:
                continue
            for nk, p in (t.get("players") or {}).items():
                row = {"Team": team, "Opp": t.get("opp") or "bye", "Player": p.get("name", ""),
                       "Pos": p.get("pos", ""), "Injury": p.get("injury", "")}
                for d in practice_dates:
                    row[d[5:]] = (p.get("practice") or {}).get(d, "")
                row["Game status"] = p.get("game_status", "")
                row["Source"] = p.get("source", "")
                rows.append(row)
        st.markdown(f"**{len(rows)}** listed players")
        st.dataframe(rows, use_container_width=True, hide_index=True)
        conflicts = data.get("conflicts") or []
        if conflicts:
            with st.expander(f"Source conflicts ({len(conflicts)})"):
                st.dataframe(conflicts, use_container_width=True, hide_index=True)

# ─── Projection Audit ───
with tab_audit:
    from dashboard._repo_sync import has_pat_configured, push_audit_dismissals_to_repo
    from dashboard.helpers import running_locally

    audit = latest_audit()
    if not audit:
        st.info("No audit yet (data/audit/). It runs at the end of every in-season pipeline run.")
    else:
        st.caption(
            f"{audit.get('date')} ({audit.get('run')}) · week {audit.get('week')} · sheet {audit.get('sheet')} · "
            f"generated {audit.get('generated_at', '')}"
        )
        if audit.get("errors"):
            st.warning("Inputs missing: " + "; ".join(audit["errors"]))

        if not running_locally():
            pat_ok = has_pat_configured()
            left, right = st.columns([4, 1])
            with left:
                if pat_ok:
                    st.info("Cloud dismissals are wiped on redeploy — click **Save dismissals to repo** after dismissing.")
                else:
                    st.warning("Add a `GITHUB_PAT` secret with Contents:write to persist dismissals from the cloud.")
            with right:
                st.write("")
                if st.button("💾 Save dismissals to repo", key="audit_save", disabled=not pat_ok):
                    ok, message = push_audit_dismissals_to_repo()
                    (st.success if ok else st.error)(message)

        alerts = audit.get("alerts") or []
        sev_pick = st.multiselect("Severity", ["error", "warning", "info"], default=["error", "warning"], key="audit_sev")
        types = sorted({a.get("type", "") for a in alerts})
        type_pick = st.multiselect("Type", types, key="audit_type")
        shown = [a for a in alerts if (not sev_pick or a.get("severity") in sev_pick) and (not type_pick or a.get("type") in type_pick)]
        st.markdown(f"**{len(shown)}** of {len(alerts)} open alerts")
        for i, a in enumerate(shown):
            with st.container():
                c_info, c_act = st.columns([5, 1])
                with c_info:
                    st.markdown(f"**[{a.get('severity', '').upper()}] {a.get('type', '').replace('_', ' ')}** — {a.get('message', '')}")
                    ev = a.get("evidence") or {}
                    if ev:
                        st.caption(", ".join(f"{k}={v}" for k, v in ev.items() if v not in (None, "", {}, []))[:300])
                with c_act:
                    note = st.text_input("Note", key=f"audit_note_{i}", label_visibility="collapsed", placeholder="reason")
                    if st.button("Dismiss", key=f"audit_dismiss_{i}"):
                        audit_dismiss(a["key"], note or "Dismissed from In Season page")
                        st.rerun()
                st.divider()

        dismissed = load_audit_dismissals()
        if dismissed:
            st.subheader(f"Dismissed ({len(dismissed)})")
            for key, info in sorted(dismissed.items(), key=lambda kv: kv[1].get("dismissed_at", ""), reverse=True):
                c1, c2, c3 = st.columns([4, 3, 1])
                c1.code(key, language=None)
                c2.caption(f"{info.get('dismissed_at', '')} — {info.get('note', '') or 'no note'}")
                if c3.button("Restore", key=f"audit_restore_{key}"):
                    audit_undismiss(key)
                    st.rerun()
