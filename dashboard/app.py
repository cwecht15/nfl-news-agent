"""NFL News Agent — Streamlit Dashboard entrypoint.

Run with: streamlit run dashboard/app.py

This file is a thin router: page config, the password gate, then the grouped
sidebar built by ``dashboard/nav.py`` (``st.navigation``). Everything that
used to live here — the Home content, the local pipeline runner, the PDF
export — moved to ``pages/home.py`` and ``dashboard/pipeline_runner.py``,
because anything rendered in the entrypoint shows on every page.
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load local .env (no-op on cloud, where the file doesn't exist).
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

import streamlit as st

st.set_page_config(
    page_title="NFL News Agent",
    page_icon="🏈",
    layout="wide",
    initial_sidebar_state="expanded",
)

from dashboard.auth import is_authenticated, require_password
from dashboard.nav import build_navigation

# st.navigation must run before anything can st.stop() the script: until it
# has executed, Streamlit lists the pages/ directory in the sidebar (the old
# alphabetical menu). So build the nav first — hidden while the login form
# is showing — then gate, then run the selected page.
page = build_navigation(hidden=not is_authenticated())
require_password()
page.run()
