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
                   layout: tuple[int, int] = (4, 1)) -> None:
    """Draw the Refresh control for ``targets`` (keys of ``_workflow_dispatch.TARGETS``)."""
    targets = list(targets)
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
    cooling = wd.cooldown_remaining()
    busy = state in ("queued", "running", "pending")
    disabled = (not transport_ok) or (not _unlocked()) or busy or cooling > 0

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
        elif cooling > 0:
            st.caption(f"Just refreshed — available again in {int(cooling)}s.")
        elif help_note:
            st.caption(help_note)

    with right:
        if st.button(f"🔄 {label}", key=f"{key}_btn", disabled=disabled,
                     use_container_width=True,
                     help="Runs the collectors on GitHub Actions and commits the result. "
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
