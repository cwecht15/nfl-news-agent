"""Shared helpers for dashboard pages."""

import os
from datetime import datetime, timezone

import streamlit as st

try:
    from zoneinfo import ZoneInfo
    _ET_ZONE = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — fallback for stripped envs
    from datetime import timedelta as _td
    _ET_ZONE = timezone(_td(hours=-5), name="ET")


def to_et_display(iso_str: str | None) -> str:
    """Render an ISO timestamp string as Eastern time with an "ET" suffix.

    Naive timestamps are treated as UTC because the cloud cron writes
    `datetime.now()` on a UTC Linux runner. Already-aware timestamps
    are converted to America/New_York (which handles DST automatically
    — i.e., displays as EST in winter, EDT in summer; the suffix stays
    "ET" so callers don't have to reason about the season).
    Empty / unparseable input passes through unchanged.
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(_ET_ZONE)
    return et.strftime("%Y-%m-%d %I:%M %p ET")


def running_locally() -> bool:
    """True only when the dashboard is running on the user's local machine.

    Detection: the cloud deployment has `dashboard_password` configured in
    Streamlit secrets (auth.py enforces it on every page). Local runs
    don't bother setting up `.streamlit/secrets.toml`. So:
      - st.secrets has dashboard_password → cloud → running_locally = False
      - st.secrets is missing or has no password → local

    The earlier signal (OPENAI_API_KEY in os.environ) became unreliable
    once `bootstrap_secrets()` started bridging Streamlit secrets into
    os.environ for the YT Report page — the env var ends up populated on
    cloud too, which made every running_locally() check flip to True
    after the user navigated through that page. The dashboard_password
    signal is independent of any bridging and hews to the deployment
    contract documented in dashboard/auth.py.
    """
    try:
        password = st.secrets.get("dashboard_password")
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return True
    except Exception:
        return True
    return not password


def stop_if_not_local(page_name: str = "This page") -> None:
    """Show a 'local-only' notice and halt rendering when on cloud."""
    if running_locally():
        return
    st.title(page_name)
    st.info(
        f"**{page_name}** is only available when running the dashboard "
        "locally. The public site at "
        "[nfl-news-agent.streamlit.app](https://nfl-news-agent.streamlit.app/) "
        "renders the **Daily Report** and **YouTube Report** tabs from data "
        "the local tool publishes — it can't run YouTube collection or push "
        "back to the repo itself.\n\n"
        "Run `Launch_Dashboard.bat` on the project machine to use this page."
    )
    st.stop()


# Names we may need to bridge from `st.secrets` into `os.environ` so the
# pipeline-side modules (which read os.environ directly via dotenv) work
# inside the Streamlit Cloud process. Locally, .env already populates
# os.environ before this runs, so the bridge is a no-op.
_SECRET_BRIDGE_KEYS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
)


def bootstrap_secrets() -> list[str]:
    """Copy known secrets from `st.secrets` into `os.environ` if missing.

    Streamlit Cloud doesn't write your `.env` file — visitors hit the
    dashboard inside a clean container that only sees `st.secrets`. The
    summarizer + collectors read `os.environ` via python-dotenv, so we
    bridge any secret-dict keys we know about into the environment on
    page load. Returns the list of keys that ended up populated, for
    pages that want to surface a friendly error when nothing is set.
    """
    populated: list[str] = []
    for key in _SECRET_BRIDGE_KEYS:
        if os.environ.get(key):
            populated.append(key)
            continue
        # st.secrets raises if the secrets backend isn't configured (no
        # secrets.toml locally, for example). Treat any failure as
        # "secret not present" rather than crashing the page.
        try:
            value = st.secrets.get(key)
        except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
            value = None
        except Exception:
            value = None
        if value:
            os.environ[key] = str(value)
            populated.append(key)
    return populated


def highlight_summary(highlight) -> str:
    """Return highlight text for both old and new report formats."""
    if isinstance(highlight, dict):
        return highlight.get("summary", "")
    return str(highlight or "")


def highlight_sources(highlight) -> list[dict]:
    """Return highlight sources for both old and new report formats."""
    if isinstance(highlight, dict):
        return highlight.get("sources", []) or []
    return []


def highlight_numbered_sources(highlight) -> list[dict]:
    """Return cited sources (with [N] inline references), if any."""
    if isinstance(highlight, dict):
        return highlight.get("numbered_sources", []) or []
    return []


def render_sources(sources: list[dict]):
    """Render a compact sources list."""
    if not sources:
        return

    st.caption("Sources")
    lines = []
    for source in sources:
        title = source.get("title") or source.get("url") or "Source"
        url = source.get("url", "")
        source_name = source.get("source", "")

        suffix = f" ({source_name})" if source_name else ""
        if url:
            lines.append(f"- [{title}]({url}){suffix}")
        else:
            lines.append(f"- {title}{suffix}")

    if lines:
        st.markdown("\n".join(lines))
