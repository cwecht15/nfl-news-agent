"""Shared citation linkifier for `[N]` markers in markdown summaries.

Kept free of streamlit (and any page-level `st.set_page_config`) so multiple
dashboard pages can import it without side effects, and so it can be unit
tested directly. Both the Daily Report and YouTube Report pages use it to turn
`[3]` / `[1, 4]` citations into links to the numbered source URLs.
"""

import re


def build_citation_linker(numbered_sources):
    """Return a function that linkifies `[N]` citations in markdown text.

    `numbered_sources` is a list of `{"num": ..., "url": ...}` dicts (the
    same shape stored on report sections and YT team notes). Returns None
    when there are no sources, so callers can fall back to plain markdown.
    """
    if not numbered_sources:
        return None

    url_map: dict[str, str] = {}
    for src in numbered_sources:
        num = str(src.get("num", ""))
        url = src.get("url", "")
        if num and url:
            url_map[num] = url

    def _linkify(text: str) -> str:
        def _sub(m):
            parts = []
            for n in re.split(r"[,\s]+", m.group(1)):
                n = n.strip()
                if n in url_map:
                    parts.append(f"[[{n}]]({url_map[n]})")
                elif n:
                    parts.append(f"[{n}]")
            return " ".join(parts)

        return re.sub(r"\[(\d+(?:[,\s]+\d+)*)\]", _sub, text)

    return _linkify
