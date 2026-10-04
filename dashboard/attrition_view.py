"""Shared rendering for positional attrition (Depth Attrition page + Team page).

Data comes from ``processing.attrition`` via ``in_season_data.attrition``.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from processing.attrition import UNITS, slot_status_text, unit_cell

# Level → cell background. Semi-transparent so the same colors read in the
# light and dark Streamlit themes.
LEVEL_BG = {
    "severe": "rgba(220, 53, 69, 0.55)",
    "notable": "rgba(253, 126, 20, 0.45)",
    "mild": "rgba(255, 193, 7, 0.22)",
}
LEVEL_RANK = {"none": 0, "mild": 1, "notable": 2, "severe": 3}
CAPTION = ("Slots are frozen from the depth chart just before Week 1 (QB/RB/WR/TE from your "
           "Week-1 sheet, everything else from OurLads), so a starter lost in Week 2 still "
           "reads as **CB1 — IR since Wk2** even after the backup moved up. "
           "IR/PUP/NFI/SUS, inactive and Out count fully, Doubtful ¾, a DNP with no "
           "designation yet ½, Questionable about ⅓; backups count half. Players traded, "
           "released or sent to the practice squad are shown greyed but not scored.")


def heat_frame(data: dict, teams: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(display text, level) frames, one row per team, one column per unit."""
    text, level = {}, {}
    for t in teams:
        units = data.get(t) or {}
        text[t] = {u: unit_cell(units.get(u)) for u in UNITS}
        level[t] = {u: (units.get(u) or {}).get("level", "none") for u in UNITS}
    return pd.DataFrame.from_dict(text, orient="index")[UNITS], pd.DataFrame.from_dict(level, orient="index")[UNITS]


def styled_heat(text: pd.DataFrame, level: pd.DataFrame):
    def _bg(_col):
        return level.map(lambda lv: f"background-color: {LEVEL_BG[lv]}" if lv in LEVEL_BG else "")
    return text.style.apply(lambda _: _bg(None), axis=None)


def team_score(units: dict) -> float:
    return round(sum(u.get("score", 0) for u in (units or {}).values()), 2)


def detail_rows(units: dict, include_departed: bool = True) -> list[dict]:
    rows = []
    for unit in UNITS:
        u = (units or {}).get(unit)
        if not u:
            continue
        for s in u.get("down") or []:
            if s.get("departed") and not include_departed:
                continue
            rows.append({
                "Unit": unit,
                "Level": u.get("level", ""),
                "Slot": s["label"] + ("" if s.get("starter") else " (backup)"),
                "Player": s["name"],
                "Status": slot_status_text(s),
                "Injury / note": "" if s.get("departed") else (s.get("detail") or ""),
                "Back by": f"Wk{s['return_week']}" if s.get("return_week") else "",
                "Missed": ", ".join(f"wk{w}" for w in s.get("missed_weeks") or []),
                "Next man up": s.get("next_up") or "",
                "Impact": s.get("impact", 0.0),
                "_departed": bool(s.get("departed")),
            })
    return rows


def render_detail(units: dict, *, key: str, include_departed: bool = True, empty: str = "Nobody down.") -> None:
    rows = detail_rows(units, include_departed)
    if not rows:
        st.caption(empty)
        return
    df = pd.DataFrame(rows)
    departed = df.pop("_departed")

    def _row_style(r):
        if departed.loc[r.name]:
            return ["color: #888; font-style: italic"] * len(r)
        bg = LEVEL_BG.get(r["Level"], "")
        return [f"background-color: {bg}" if c == "Unit" and bg else "" for c in r.index]

    st.dataframe(df.style.apply(_row_style, axis=1), use_container_width=True, hide_index=True, key=key,
                 column_config={"Impact": st.column_config.NumberColumn(format="%.2f")})


def unit_summary(units: dict) -> str:
    """"CB severe (2/3 starters) · OL notable (1/5)" for the units at notable+."""
    bits = []
    for unit in UNITS:
        u = (units or {}).get(unit)
        if u and u.get("level") in ("notable", "severe"):
            down = u["starters_down"]
            down_txt = f"{down:g}" if down == int(down) else f"{down:.1f}"
            bits.append(f"**{unit}** {u['level']} ({down_txt}/{u['starters']} starters)")
    return " · ".join(bits)
