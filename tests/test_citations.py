"""Tests for dashboard.citations.build_citation_linker.

Pure function (no streamlit), shared by the Daily Report and YouTube
Report pages to turn [N] markers into links to the numbered source URLs.
"""

from dashboard.citations import build_citation_linker

SOURCES = [
    {"num": 1, "url": "https://youtu.be/aaa", "title": "KC presser"},
    {"num": 2, "url": "https://youtu.be/bbb", "title": "PHI presser"},
]


def test_none_when_no_sources():
    assert build_citation_linker([]) is None
    assert build_citation_linker(None) is None


def test_linkifies_single_citation():
    linkify = build_citation_linker(SOURCES)
    out = linkify("Mahoney is starting [1].")
    assert "[[1]](https://youtu.be/aaa)" in out


def test_linkifies_multi_citation():
    linkify = build_citation_linker(SOURCES)
    out = linkify("Both teams confirmed [1, 2].")
    assert "[[1]](https://youtu.be/aaa)" in out
    assert "[[2]](https://youtu.be/bbb)" in out


def test_unknown_citation_passes_through():
    linkify = build_citation_linker(SOURCES)
    out = linkify("Mystery source [9].")
    assert "[9]" in out
    assert "](https" not in out  # no link produced for an unmapped number


def test_num_coerced_to_string():
    # Sources carry int `num` (as the summarizer emits); citations are text.
    linkify = build_citation_linker([{"num": 3, "url": "https://x/y"}])
    assert "[[3]](https://x/y)" in linkify("see [3]")


def test_text_without_citations_unchanged():
    linkify = build_citation_linker(SOURCES)
    assert linkify("No citations here.") == "No citations here."
