"""Projection Audit page — the interactive view of the latest audit run
(filters, per-alert dismiss with note, restore), in-season only."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Projection Audit", page_icon="✅", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard import in_season_data as isd
from dashboard._repo_sync import has_pat_configured, push_audit_dismissals_to_repo
from dashboard.helpers import running_locally
from processing.projection_audit import (
    dismiss as audit_dismiss,
    latest_audit,
    load_dismissals as load_audit_dismissals,
    undismiss as audit_undismiss,
)

st.header("Projection Audit")
isd.require_in_season()

audit = latest_audit()
if not audit:
    st.info("No audit yet (data/audit/). It runs at the end of every in-season pipeline run.")
    st.stop()

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
                audit_dismiss(a["key"], note or "Dismissed from Projection Audit page")
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
