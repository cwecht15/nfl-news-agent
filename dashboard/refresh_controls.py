"""The Refresh button that in-season pages put next to their data.

Rosters, transactions, practice-squad elevations, injury designations and
game-day inactives change hour to hour; the crons that collect them run on a
fixed schedule and GitHub fires them late. This control dispatches
``.github/workflows/refresh.yml`` for just the sources a page shows, so you can
pull the current state instead of waiting for the next run.

**The last-updated timestamp is the completion signal**, not the button. The
workflow commits to master, Streamlit Cloud redeploys, the container restarts
and the caption re-renders with a newer time. That restart is also why this
does not poll in a loop the way ``pipeline_runner`` does for a local run: the
redeploy destroys the session mid-poll, so a spinner tracking the run to
completion would be theatre. There is a one-shot "Check status" button instead.

**Offseason:** unreachable rather than guarded. ``nav.py`` hides the in-season
pages, ``require_in_season()`` stops a direct URL hit and the Team page stops
before its in-season content. Worth knowing anyway: an offseason dispatch would
be a green run that collects nothing, commits nothing and moves no timestamp —
indistinguishable from a failure.
"""

from __future__ import annotations

import time
from typing import Mapping, Optional, Sequence

import streamlit as st

from dashboard import _workflow_dispatch as wd
from dashboard.helpers import to_et_display

#: Optional second gate. When ``st.secrets["refresh_password"]`` is set, a
#: visitor must enter it once per session before the button works; when it is
#: unset (the default) anyone past the dashboard password can refresh.
_UNLOCK_SECRET = "refresh_password"


def _unlock_required() -> Optional[str]:
    try:
        return st.secrets.get(_UNLOCK_SECRET)
    except Exception:  # noqa: BLE001 — no secrets backend at all (local dev)
        return None


def _unlocked() -> bool:
    secret = _unlock_required()
    if not secret:
        return True
    return st.session_state.get("refresh_unlocked") is True


def _render_unlock() -> None:
    with st.expander("Unlock refresh"):
        entered = st.text_input("Refresh password", type="password", key="refresh_unlock_input")
        if entered and entered == _unlock_required():
            st.session_state["refresh_unlocked"] = True
            st.rerun()
        elif entered:
            st.error("Not that one.")


@st.cache_data(ttl=15, show_spinner=False)
def _run_status(nonce: str, since: float) -> Optional[dict]:
    """One GET, cached briefly so several controls on a page share it."""
    return wd.find_run(nonce, since_epoch=since)


def _stamp_caption(targets: Sequence[str], stamps: Optional[Mapping[str, str]]) -> str:
    if not stamps:
        return ""
    parts = []
    for t in targets:
        when = stamps.get(t)
        if when:
            parts.append(f"{wd.TARGETS.get(t, {}).get('label', t)} {to_et_display(when)}")
    return " · ".join(parts)


def render_refresh(targets: Sequence[str], *, key: str, label: str = "Refresh",
                   stamps: Optional[Mapping[str, str]] = None, help_note: str = "",
                   layout: tuple[int, int] = (4, 1),
                   gate: Optional[tuple[bool, str]] = None) -> None:
    """Draw the Refresh control for ``targets`` (keys of ``_workflow_dispatch.TARGETS``).

    ``gate=(False, reason)`` disables the button and captions why — the paid
    Odds API buttons pass the ledger budget here. It mirrors, never replaces,
    the server-side check in ``collectors/odds_api.py``.
    """
    targets = list(targets)
    gated = bool(gate) and not gate[0]
    paid = [t for t in targets if (wd.TARGETS.get(t) or {}).get("paid")]
    nonce_key, at_key, msg_key = f"_rf_{key}_nonce", f"_rf_{key}_at", f"_rf_{key}_msg"

    left, right = st.columns(list(layout))

    # --- resolve the current state -----------------------------------------
    transport_ok = wd.can_dispatch()
    nonce = st.session_state.get(nonce_key, "")
    since = float(st.session_state.get(at_key, 0.0) or 0.0)
    run = None
    state, line = "", ""
    # A dispatch older than 15 minutes is no longer this page's business: the
    # redeploy it triggered has long since restarted the container.
    if nonce and since and (time.time() - since) < 900:
        run = _run_status(nonce, since)
        state, line = wd.describe_run(run)
    cooling = wd.cooldown_remaining(targets)
    busy = state in ("queued", "running", "pending")
    disabled = (not transport_ok) or (not _unlocked()) or busy or cooling > 0 or gated

    with left:
        caption = _stamp_caption(targets, stamps)
        if caption:
            st.caption(f"Last updated — {caption}")
        if not transport_ok:
            st.caption(
                "Refresh needs a `GITHUB_PAT` secret with **Actions: Read and write** "
                "(the Save-to-repo buttons' token only has Contents)."
            )
        elif line:
            # Whatever the run is doing now outranks the "dispatched" message.
            url = (run or {}).get("html_url") or wd.actions_html_url()
            st.caption(f"{line} [Open the run]({url})")
        elif st.session_state.get(msg_key):
            st.caption(st.session_state[msg_key])
        elif gated:
            st.caption(f"Unavailable — {gate[1]}.")
        elif cooling > 0:
            st.caption(f"Just refreshed — available again in {int(cooling)}s.")
        elif help_note:
            st.caption(help_note)

    with right:
        cost = " ".join(wd.TARGETS[t].get("blurb", "") for t in paid)
        if st.button(f"🔄 {label}", key=f"{key}_btn", disabled=disabled,
                     use_container_width=True,
                     help=("Spends Odds API credits. " + cost + " " if paid else "")
                          + "Runs the collectors on GitHub Actions and commits the result. "
                          "The site reloads itself when it lands (2-4 minutes); the "
                          "timestamp on the left is what moves."):
            ok, message, new_nonce = wd.dispatch_refresh(targets)
            if ok:
                st.session_state[nonce_key] = new_nonce
                st.session_state[at_key] = time.time()
                st.session_state[msg_key] = message
                st.rerun()
            else:
                st.session_state[msg_key] = ""
                st.error(message)
        if busy and st.button("Check status", key=f"{key}_chk", use_container_width=True):
            _run_status.clear()
            st.rerun()

    if not _unlocked():
        with left:
            _render_unlock()


def render_paid_odds_controls(season: int, week: Optional[int], *, key: str,
                              stamps: Optional[Mapping[str, str]] = None) -> None:
    """The two credit-spending buttons: game lines (9 credits) and player props (~550).

    Both gates are computed from committed files only — the ledger
    (``data/odds/api_usage.json``) and the week file — so a page rerun makes
    no network call. They mirror the server-side budget in
    ``collectors/odds_api.py``, which is what actually decides: a stale page
    can at worst dispatch a run that then declines and says why in its log.
    """
    from collectors import odds_api
    from dashboard import in_season_data as isd

    ledger = isd.api_usage()
    quota = ledger.get("quota") or {}
    week_data = (isd.odds_week(season, week) or {}) if week else {}
    lines_gate = odds_api.lines_budget(quota)
    budget = odds_api.props_budget(
        ledger, (week_data.get("pull") or {}).get("pulled_at"),
        season_week=(season, week) if week else None,
    )

    bits = []
    if quota.get("remaining") is not None:
        bits.append(f"{int(quota['remaining']):,} Odds API credits left"
                    + (f" (as of {to_et_display(quota.get('at'))})" if quota.get("at") else ""))
    bits.append(f"props pulls: {budget['today']} of {budget['max_per_day']} today, "
                f"{budget['this_week']} of {budget['max_per_week']} this week")
    st.caption(" · ".join(bits))

    c_lines, c_props = st.columns(2)
    with c_lines:
        render_refresh(("odds_lines",), key=f"{key}_lines", label="Pull game lines (9 credits)",
                       stamps=stamps, layout=(3, 2), gate=lines_gate,
                       help_note="Spreads / totals / moneylines straight from The Odds API; "
                                 "props stay as last published.")
    with c_props:
        render_refresh(("odds_props",), key=f"{key}_props",
                       label="Pull player props (~550 credits)",
                       stamps=stamps, layout=(3, 2),
                       gate=(budget["allowed"], budget["reason"]),
                       help_note=f"Runs the NFL Odds project's full pull, then re-reads it. "
                                 f"{budget['reason']}.")
