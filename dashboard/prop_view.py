"""Render a player-lines table — shared by the Team and Line Movement pages.

The rows mix stats with very different scales in the same columns (220.5
passing yards, 4.5 receptions, 0.05 implied TDs), so formatting is per row
through a pandas Styler: the columns stay numeric (sorting works on the real
number) while each cell reads at its stat's precision, and a missing value
says why it is missing instead of "None".
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from processing.line_insights import FINE_STATS

VALUE_COLS = ("Your proj", "Book line", "Market open", "Market then", "Market now")
HELP = {
    "Book line": "The posted over/under. Anytime TD is a yes/no bet, so it has none.",
    "Market open": "The betting consensus's implied average when the line opened.",
    "Market then": "The betting consensus's implied average at the \"Moved since\" baseline.",
    "Market now": "The betting consensus's implied average at the latest pull.",
    "Move %": "Market now vs where the line started (open, or the page's \"Moved since\" "
              "baseline), in percent — comparable across stats.",
    "Last pull": "Change in the most recent pull, in the stat's own units.",
    "Market vs you %": "Market now vs your projection; \"you: 0\" when your sheet projects none.",
}


def render_prop_table(rows: list[dict], caption: str | None = None) -> None:
    """``rows``: dicts keyed by the display column names plus ``_stat``."""
    if not rows:
        return
    df = pd.DataFrame(rows)
    stats = df.pop("_stat") if "_stat" in df else pd.Series([""] * len(df))
    fine = stats.isin(FINE_STATS).to_numpy()
    td = (stats == "anytime_td").to_numpy()
    values = [c for c in VALUE_COLS if c in df]

    sty = df.style
    for mask, spec in ((~fine, "{:.1f}"), (fine, "{:.2f}")):
        if mask.any() and values:
            sty = sty.format(spec, subset=pd.IndexSlice[mask, values], na_rep="")
    if "Last pull" in df:
        for mask, spec in ((~fine, "{:+.1f}"), (fine, "{:+.2f}")):
            if mask.any():
                sty = sty.format(spec, subset=pd.IndexSlice[mask, ["Last pull"]], na_rep="")
    if "Book line" in df and td.any():
        sty = sty.format("{:.1f}", subset=pd.IndexSlice[td, ["Book line"]], na_rep="yes/no")
    for col in ("Move %", "Market vs you %"):
        if col in df:
            sty = sty.format("{:+.0f}%", subset=[col], na_rep="")
    if "Market vs you %" in df and "Your proj" in df:
        zero = (df["Your proj"].fillna(0) == 0).to_numpy()
        if zero.any():
            sty = sty.format("{:+.0f}%", subset=pd.IndexSlice[zero, ["Market vs you %"]],
                             na_rep="you: 0")

    st.dataframe(sty, use_container_width=True, hide_index=True,
                 column_config={c: st.column_config.Column(help=h) for c, h in HELP.items() if c in df})
    if caption:
        st.caption(caption)


PROP_CAPTION = (
    "**Market** = the betting consensus's implied average for the stat — not the posted over/under, "
    "which is **Book line**. Anytime TD is in **implied TDs** (expected touchdowns, the unit your "
    "sheet projects), and is a yes/no bet with no book line. **Move %** and **Market vs you %** are "
    "relative, so receptions and yards compare on one scale."
)
