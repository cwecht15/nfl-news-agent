"""Shared password gate for the dashboard.

`dashboard/app.py` calls `require_password()` before `st.navigation` runs
the selected page, so every page is gated by the entrypoint. The pages in
`dashboard/views/` still call it right after `st.set_page_config()` as a
belt-and-braces measure (it is a no-op once the session is authenticated).

The gate reads `dashboard_password` from `st.secrets`. If no password is
configured (e.g. local dev with no `.streamlit/secrets.toml`), access is
allowed — Streamlit Cloud deploys MUST set the secret to enforce auth.
"""

import streamlit as st


def _expected_password() -> str | None:
    try:
        expected = st.secrets["dashboard_password"]
    except (KeyError, FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return None
    return expected or None


def is_authenticated() -> bool:
    """True when no password is configured or this session has entered it."""
    if _expected_password() is None:
        return True
    return bool(st.session_state.get("authenticated"))


def require_password() -> None:
    expected = _expected_password()
    if expected is None or st.session_state.get("authenticated"):
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
