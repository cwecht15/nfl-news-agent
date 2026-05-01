"""Shared helpers for dashboard pages."""

import os

import streamlit as st


def running_locally() -> bool:
    """True only when the dashboard is running on the user's local machine.

    Uses the presence of `OPENAI_API_KEY` in the environment as the signal:
    locally it's loaded from .env at import time, on Streamlit Cloud it's
    not present in the dashboard process. Pages that need to write files
    or invoke the local pipeline should gate on this — visitors to the
    public site can't do either, and clicking the controls would just
    surface auth/credential errors.
    """
    return bool(os.environ.get("OPENAI_API_KEY"))


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
