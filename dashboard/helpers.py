"""Shared helpers for dashboard pages."""

import streamlit as st


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
