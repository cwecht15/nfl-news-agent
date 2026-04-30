"""Shared password gate for the dashboard.

Streamlit's multi-page navigation runs each page in `dashboard/pages/`
as an independent script, so the gate can't live only in `app.py` —
otherwise visitors bypass it by clicking any sidebar link. Every page
calls `require_password()` right after `st.set_page_config()` to enforce
the same password check everywhere.

The gate reads `dashboard_password` from `st.secrets`. If no password is
configured (e.g. local dev with no `.streamlit/secrets.toml`), access is
allowed — Streamlit Cloud deploys MUST set the secret to enforce auth.
"""

import streamlit as st


def require_password() -> None:
    try:
        expected = st.secrets["dashboard_password"]
    except (KeyError, FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return

    if not expected:
        return

    if st.session_state.get("authenticated"):
        return

    st.title("🏈 NFL News Agent")
    st.markdown("Private dashboard — enter the access password to continue.")
    with st.form("login_form", clear_on_submit=False):
        pwd = st.text_input("Password", type="password")
        if st.form_submit_button("Enter"):
            if pwd == expected:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    st.stop()
