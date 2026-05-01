"""PDF exporter for the daily report.

Builds a single PDF that combines the daily news report with the same
projection and depth-chart data exposed on the dashboard, so a user can
download a full day-in-review file from the landing page.
"""

import csv
import json
import logging
import re
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from config_loader import get_data_dir
from reports.report_builder import load_report

logger = logging.getLogger(__name__)

SECTION_TITLES = {
    "transactions": "Transactions & Signings",
    "injuries": "Injury Reports",
    "press_conferences": "Press Conference Highlights",
    "analysis": "Analysis & What to Watch",
}

# FantasyPoints brand palette per BrandStyleGuide_v6. PDFs are
# light-layout documents → secondary logo variant rules: white #F0F0F0
# dominant background, black #111111 text/structure, red #CC3333 used
# only as 10% accent (section-header chip, footer ticker, dividers).
BRAND_RED = colors.HexColor("#CC3333")
BRAND_BLACK = colors.HexColor("#111111")
BRAND_WHITE = colors.HexColor("#F0F0F0")
GREY = colors.HexColor("#666666")
LIGHT_GREY = colors.HexColor("#F0F0F0")
MID_GREY = colors.HexColor("#CCCCCC")

# Legacy aliases — old code paths still reference these. Map them onto
# the brand palette so existing call sites keep rendering, just with
# the new colors.
NAVY = BRAND_BLACK
RED = BRAND_RED


# Brand fonts. Kanit Extrabold Italic for headlines/display (always
# uppercase per the style guide); Mulish for all body, captions,
# labels. Registered once at module import so ReportLab can resolve
# them by name in every Paragraph style.
_BRAND_DIR = Path(__file__).parent.parent / "assets" / "brand"
_FONT_KANIT_PATH = _BRAND_DIR / "Kanit-ExtraBoldItalic.ttf"
_FONT_MULISH_PATH = _BRAND_DIR / "Mulish-VariableFont_wght.ttf"

KANIT = "Kanit-ExtraBoldItalic"
MULISH = "Mulish"
_BRAND_FONTS_READY = False


def _ensure_brand_fonts() -> bool:
    """Register Kanit + Mulish on first call; cheap no-op afterwards.

    Returns True when both fonts are available and Paragraph styles
    can use them. Falls back to Helvetica on registration failure
    (e.g. font file missing in a stripped deploy) so PDFs still
    render — just without the brand typography.
    """
    global _BRAND_FONTS_READY
    if _BRAND_FONTS_READY:
        return True
    try:
        if KANIT not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(KANIT, str(_FONT_KANIT_PATH)))
        if MULISH not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(MULISH, str(_FONT_MULISH_PATH)))
        # Map <b>/<i> markup in Paragraphs onto the same TTF — Mulish
        # is a variable font but ReportLab loads a single weight, so
        # synthetic bold is the trade-off. Visual hierarchy comes
        # from Kanit vs Mulish, not from bold body.
        pdfmetrics.registerFontFamily(
            MULISH, normal=MULISH, bold=MULISH, italic=MULISH, boldItalic=MULISH,
        )
        pdfmetrics.registerFontFamily(
            KANIT, normal=KANIT, bold=KANIT, italic=KANIT, boldItalic=KANIT,
        )
        _BRAND_FONTS_READY = True
    except Exception as e:
        logger.warning(
            "Brand font registration failed (%s); falling back to Helvetica.",
            e,
        )
        _BRAND_FONTS_READY = False
    return _BRAND_FONTS_READY


def _font(name: str) -> str:
    """Return the brand font when registered, else a safe Helvetica
    fallback. Lets every style declaration stay uniform."""
    if _ensure_brand_fonts():
        return name
    return "Helvetica" if name != KANIT else "Helvetica-BoldOblique"


def _styles() -> dict[str, ParagraphStyle]:
    """Build the set of paragraph styles used across the PDF.

    Per the FantasyPoints brand: Kanit Extrabold Italic for headlines
    (uppercase, never used for body); Mulish for all body, captions,
    labels. Brand black for primary text, brand red exclusively as
    accent (section-bar chips, dividers, footer ticker).
    """
    _ensure_brand_fonts()

    return {
        "title": ParagraphStyle(
            "Title",
            fontName=_font(KANIT),
            fontSize=26, leading=30,
            textColor=BRAND_BLACK,
            spaceAfter=6,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle",
            fontName=_font(MULISH),
            fontSize=10, leading=14,
            textColor=GREY,
            spaceAfter=12,
        ),
        "section": ParagraphStyle(
            "Section",
            fontName=_font(KANIT),
            fontSize=14, leading=18,
            textColor=BRAND_WHITE,  # used inside the black section bar
            spaceBefore=14, spaceAfter=6,
            alignment=TA_LEFT,
        ),
        "section_inline": ParagraphStyle(
            "SectionInline",
            fontName=_font(KANIT),
            fontSize=14, leading=18,
            textColor=BRAND_BLACK,
            spaceBefore=10, spaceAfter=4,
        ),
        "subsection": ParagraphStyle(
            "Subsection",
            fontName=_font(KANIT),
            fontSize=12, leading=16,
            textColor=BRAND_BLACK,
            spaceBefore=10, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "Body",
            fontName=_font(MULISH),
            fontSize=10, leading=14,
            textColor=BRAND_BLACK,
            alignment=TA_LEFT,
        ),
        "source": ParagraphStyle(
            "Source",
            fontName=_font(MULISH),
            fontSize=8.5, leading=11,
            textColor=GREY,
            leftIndent=10,
        ),
        "caption": ParagraphStyle(
            "Caption",
            fontName=_font(MULISH),
            fontSize=8, leading=11,
            textColor=GREY,
        ),
    }


def _brand_section_bar(title: str, number: str = "") -> Table:
    """Render a section header in the brand's '01 / TITLE' style:
    a small red chip (with an optional number) followed by a black
    bar holding the white-on-black uppercase title. Honors the brand's
    60/30/10 rule — red is the chip accent, black dominates the bar."""
    _ensure_brand_fonts()
    chip_w = 0.55 * inch if number else 0.18 * inch
    bar_w = 7.0 * inch - chip_w
    cells = [[number or "", title.upper()]]
    table = Table(cells, colWidths=[chip_w, bar_w], rowHeights=[0.36 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), BRAND_RED),
        ("BACKGROUND", (1, 0), (1, 0), BRAND_BLACK),
        ("TEXTCOLOR", (0, 0), (-1, 0), BRAND_WHITE),
        ("FONTNAME", (0, 0), (-1, 0), _font(KANIT)),
        ("FONTSIZE", (0, 0), (-1, 0), 13),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "CENTER"),
        ("ALIGN", (1, 0), (1, 0), "LEFT"),
        ("LEFTPADDING", (1, 0), (1, 0), 12),
        ("RIGHTPADDING", (-1, 0), (-1, 0), 8),
    ]))
    return table


def _draw_brand_footer(canvas, doc):
    """Red ticker bar at the bottom of every page reading
    "FANTASYPOINTS.COM · FANTASYPOINTS.COM · FANTASYPOINTS.COM" plus
    a page number on the right. Drawn via canvas.drawString rather
    than as a flowable so it never disrupts the story layout."""
    _ensure_brand_fonts()
    page_w, _ = LETTER
    bar_h = 0.22 * inch
    canvas.saveState()
    canvas.setFillColor(BRAND_RED)
    canvas.rect(0, 0, page_w, bar_h, stroke=0, fill=1)
    canvas.setFillColor(BRAND_WHITE)
    canvas.setFont(_font(KANIT), 8)
    ticker = "FANTASYPOINTS.COM  ·  FANTASYPOINTS.COM  ·  FANTASYPOINTS.COM"
    canvas.drawString(0.6 * inch, bar_h / 2 - 3, ticker)
    page_no = f"PAGE {canvas.getPageNumber()}"
    canvas.drawRightString(page_w - 0.6 * inch, bar_h / 2 - 3, page_no)
    canvas.restoreState()


def _doc_template(buf: BytesIO, title: str) -> SimpleDocTemplate:
    """Brand-styled document template: 0.6in margins on three sides
    plus extra bottom margin to clear the red footer ticker."""
    return SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.55 * inch,
        title=title,
    )


def _brand_title_block(title: str, subtitle: str = "") -> list[Any]:
    """Top-of-document title block in the brand style: small red
    'FANTASY POINTS' wordmark line, then the document title in big
    Kanit caps, then an optional Mulish subtitle (date, item count,
    etc). Replaces the old plain-text Heading1 + subtitle pair."""
    styles = _styles()
    out: list[Any] = []
    out.append(Paragraph(
        '<font color="#CC3333">FANTASY</font>'
        '<font color="#111111">  POINTS</font>',
        ParagraphStyle(
            "Wordmark", fontName=_font(KANIT),
            fontSize=11, leading=14, textColor=BRAND_BLACK,
        ),
    ))
    out.append(Spacer(1, 4))
    out.append(Paragraph(title.upper(), styles["title"]))
    out.append(HRFlowable(
        width="100%", thickness=2, color=BRAND_RED, spaceAfter=8,
    ))
    if subtitle:
        out.append(Paragraph(subtitle, styles["subtitle"]))
    return out


_SAFE_RE = re.compile(r"[&<>]")
_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_CITE_RE = re.compile(r"\[(\d+(?:[,\s]+\d+)*)\]")


def _escape(text: str) -> str:
    """Escape ReportLab-inline-tag special characters."""
    if not text:
        return ""
    return _SAFE_RE.sub(lambda m: _ESCAPE[m.group(0)], str(text))


def _linkify_citations(
    text: str,
    source_url_map: Optional[dict[str, str]],
) -> str:
    """Replace `[N]` / `[N, M]` citation markers with clickable links."""
    if not source_url_map:
        return text

    def _sub(match):
        nums = match.group(1)
        parts = []
        for n in re.split(r"[,\s]+", nums):
            n = n.strip()
            if not n:
                continue
            url = source_url_map.get(n)
            if url:
                parts.append(
                    f'<link href="{_escape(url)}" color="blue">[{n}]</link>'
                )
            else:
                parts.append(f"[{n}]")
        return " ".join(parts)

    return _CITE_RE.sub(_sub, text)


def _inline_markdown(
    text: str,
    source_url_map: Optional[dict[str, str]] = None,
) -> str:
    """Escape, then apply inline markdown (bold / italic / citation links)."""
    text = _escape(text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    text = _linkify_citations(text, source_url_map)
    return text


def _render_markdown(
    summary: str,
    styles: dict[str, ParagraphStyle],
    source_url_map: Optional[dict[str, str]] = None,
) -> list[Any]:
    """Convert a markdown-ish summary into a list of reportlab flowables.

    Supports `##` headings, `-`/`*` bullets (including wrapped lines),
    `**bold**` / `*italic*`, and `[N]` citation linking when a
    source_url_map is provided.
    """
    flows: list[Any] = []
    if not summary:
        return flows

    bullet_style = ParagraphStyle(
        "Bullet",
        parent=styles["body"],
        leftIndent=14,
        bulletIndent=2,
        spaceBefore=0,
        spaceAfter=3,
    )
    heading_style = styles["subsection"]

    blocks = [b for b in summary.split("\n\n") if b.strip()]
    for block in blocks:
        lines = block.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            if not line:
                i += 1
                continue

            heading_m = _HEADING_RE.match(line)
            bullet_m = _BULLET_RE.match(line)

            if heading_m:
                content = _inline_markdown(heading_m.group(2), source_url_map)
                flows.append(Paragraph(content, heading_style))
                i += 1
            elif bullet_m:
                bullets: list[str] = [bullet_m.group(1)]
                i += 1
                while i < len(lines):
                    nxt = lines[i].rstrip()
                    if not nxt:
                        i += 1
                        break
                    bm = _BULLET_RE.match(nxt)
                    if bm:
                        bullets.append(bm.group(1))
                    elif _HEADING_RE.match(nxt):
                        break
                    else:
                        # Continuation of the previous bullet
                        bullets[-1] += " " + nxt.strip()
                    i += 1
                for b in bullets:
                    content = _inline_markdown(b, source_url_map)
                    flows.append(Paragraph(f"• {content}", bullet_style))
                flows.append(Spacer(1, 3))
            else:
                para_lines = [line]
                i += 1
                while i < len(lines):
                    nxt = lines[i].rstrip()
                    if not nxt or _HEADING_RE.match(nxt) or _BULLET_RE.match(nxt):
                        break
                    para_lines.append(nxt)
                    i += 1
                content = _inline_markdown(" ".join(para_lines), source_url_map)
                flows.append(Paragraph(content, styles["body"]))
                flows.append(Spacer(1, 4))

    return flows


def _source_line(source: dict[str, Any]) -> str:
    """Format a single source dict as one line of text."""
    title = _escape(source.get("title", "Source") or "Source")
    url = source.get("url", "") or ""
    src = _escape(source.get("source", "") or "")
    suffix = f" ({src})" if src else ""
    if url:
        safe_url = _escape(url)
        return f'<link href="{safe_url}" color="blue">{title}</link>{suffix}'
    return f"{title}{suffix}"


def _render_sources(
    sources: list[dict[str, Any]],
    styles: dict[str, ParagraphStyle],
    header: str = "Sources",
    limit: int = 12,
) -> list[Any]:
    """Render a sources block as a list of flowables."""
    flows: list[Any] = []
    if not sources:
        return flows

    flows.append(Paragraph(f"<b>{header}</b>", styles["caption"]))
    for src in sources[:limit]:
        flows.append(Paragraph(f"• {_source_line(src)}", styles["source"]))
    if len(sources) > limit:
        flows.append(
            Paragraph(
                f"... and {len(sources) - limit} more",
                styles["caption"],
            )
        )
    flows.append(Spacer(1, 6))
    return flows


def _render_numbered_sources(
    numbered: list[dict[str, Any]],
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    """Render citation-style numbered sources used by the analysis section."""
    flows: list[Any] = [Paragraph("<b>Sources</b>", styles["caption"])]
    for src in numbered:
        num = src.get("num", "")
        title = _escape(src.get("title", "Source") or "Source")
        source_name = _escape(src.get("source", "") or "")
        url = src.get("url", "") or ""
        suffix = f" ({source_name})" if source_name else ""
        if url:
            safe_url = _escape(url)
            line = (
                f'[{num}] <link href="{safe_url}" color="blue">{title}</link>{suffix}'
            )
        else:
            line = f"[{num}] {title}{suffix}"
        flows.append(Paragraph(line, styles["source"]))
    flows.append(Spacer(1, 6))
    return flows


def _render_report_sections(
    report,
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    """Render the main news sections of the daily report."""
    flows: list[Any] = []
    for key, section in report.sections.items():
        title = SECTION_TITLES.get(key, key.replace("_", " ").title())
        count = section.get("count")
        badge = f" ({count} items)" if count else ""

        flows.append(Paragraph(f"{_escape(title)}{badge}", styles["section"]))

        numbered = section.get("numbered_sources") or []
        url_map: dict[str, str] = {
            str(src.get("num")): src.get("url", "")
            for src in numbered
            if src.get("url")
        }

        flows.extend(
            _render_markdown(
                section.get("summary", ""),
                styles,
                source_url_map=url_map or None,
            )
        )

        if numbered:
            flows.extend(_render_numbered_sources(numbered, styles))
        else:
            flows.extend(_render_sources(section.get("sources", []), styles))
    return flows


def _render_team_highlights(
    report,
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    """Render per-team highlight paragraphs."""
    flows: list[Any] = []
    if not report.team_highlights:
        return flows

    flows.append(Paragraph("Team Highlights", styles["section"]))
    for team, highlight in sorted(report.team_highlights.items()):
        if isinstance(highlight, dict):
            summary = highlight.get("summary", "")
            sources = highlight.get("sources", [])
        else:
            summary = str(highlight or "")
            sources = []

        flows.append(Paragraph(_escape(team), styles["subsection"]))
        flows.extend(_render_markdown(summary, styles))
        flows.extend(_render_sources(sources, styles, limit=6))
    return flows


def _load_projection_changes(date_str: str) -> dict[str, list[dict[str, str]]]:
    """Load changelog rows for the given date, grouped by kind (player/fantasy/team)."""
    path = get_data_dir("projections") / "changelog.csv"
    if not path.exists():
        return {}

    grouped: dict[str, list[dict[str, str]]] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date") != date_str:
                continue
            grouped.setdefault(row.get("kind", "other"), []).append(row)
    return grouped


def _projection_table(
    rows: list[dict[str, str]],
    columns: list[tuple[str, str]],
    styles: dict[str, ParagraphStyle],
    limit: int,
) -> Optional[Table]:
    """Build a small Table flowable from changelog rows."""
    if not rows:
        return None

    header = [col[1] for col in columns]
    body = []
    for row in rows[:limit]:
        body.append([
            Paragraph(_escape(row.get(col[0], "")), styles["caption"])
            for col in columns
        ])

    table = Table(
        [header] + body,
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (0, 0), (-1, 0), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GREY]),
                ("GRID", (0, 0), (-1, -1), 0.25, MID_GREY),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _render_projection_changes(
    date_str: str,
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    """Render projection change tables (players / fantasy / teams)."""
    grouped = _load_projection_changes(date_str)
    flows: list[Any] = [Paragraph("Projection Changes", styles["section"])]

    if not grouped:
        flows.append(
            Paragraph(
                "No projection changes recorded for this date.",
                styles["body"],
            )
        )
        return flows

    players = grouped.get("player", [])
    fantasy = grouped.get("fantasy", [])
    teams = grouped.get("team", [])

    flows.append(
        Paragraph(
            f"Players: {len(players)} changes &nbsp;·&nbsp; "
            f"Fantasy: {len(fantasy)} changes &nbsp;·&nbsp; "
            f"Teams: {len(teams)} changes",
            styles["caption"],
        )
    )
    flows.append(Spacer(1, 6))

    player_adj = [r for r in players if "Adj" in (r.get("metric") or "")]
    if player_adj:
        flows.append(Paragraph("Player Adjustments", styles["subsection"]))
        table = _projection_table(
            player_adj,
            [
                ("label", "Player"),
                ("metric", "Metric"),
                ("old_value", "From"),
                ("new_value", "To"),
            ],
            styles,
            limit=60,
        )
        if table:
            flows.append(table)
            if len(player_adj) > 60:
                flows.append(
                    Paragraph(
                        f"... and {len(player_adj) - 60} more adjustments",
                        styles["caption"],
                    )
                )
        flows.append(Spacer(1, 8))

    if fantasy:
        flows.append(Paragraph("Fantasy Changes", styles["subsection"]))
        table = _projection_table(
            fantasy,
            [
                ("label", "Player"),
                ("metric", "Metric"),
                ("old_value", "From"),
                ("new_value", "To"),
            ],
            styles,
            limit=60,
        )
        if table:
            flows.append(table)
            if len(fantasy) > 60:
                flows.append(
                    Paragraph(
                        f"... and {len(fantasy) - 60} more",
                        styles["caption"],
                    )
                )
        flows.append(Spacer(1, 8))

    if teams:
        flows.append(Paragraph("Team Changes", styles["subsection"]))
        table = _projection_table(
            teams,
            [
                ("label", "Team"),
                ("metric", "Metric"),
                ("old_value", "From"),
                ("new_value", "To"),
            ],
            styles,
            limit=60,
        )
        if table:
            flows.append(table)
            if len(teams) > 60:
                flows.append(
                    Paragraph(
                        f"... and {len(teams) - 60} more",
                        styles["caption"],
                    )
                )

    return flows


def _find_depth_chart_prior(date_str: str) -> Optional[str]:
    """Return the YYYY-MM-DD of the most recent depth chart before `date_str`."""
    dc_dir = get_data_dir("depth_charts")
    dates: list[str] = []
    for f in dc_dir.glob("*.json"):
        stem = f.stem
        if stem < date_str:
            try:
                datetime.strptime(stem, "%Y-%m-%d")
                dates.append(stem)
            except ValueError:
                continue
    return max(dates) if dates else None


def _render_depth_chart_changes(
    date_str: str,
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    """Render depth chart diff vs the prior available day."""
    flows: list[Any] = [Paragraph("Depth Chart Changes", styles["section"])]

    dc_dir = get_data_dir("depth_charts")
    current_path = dc_dir / f"{date_str}.json"
    if not current_path.exists():
        flows.append(
            Paragraph(
                "No depth chart snapshot available for this date.",
                styles["body"],
            )
        )
        return flows

    prior_date = _find_depth_chart_prior(date_str)
    if not prior_date:
        flows.append(
            Paragraph(
                "No prior snapshot available to diff against.",
                styles["body"],
            )
        )
        return flows

    try:
        from collectors.depth_chart_collector import (
            annotate_depth_changes,
            diff_depth_charts,
        )

        with open(current_path, encoding="utf-8") as f:
            cur = json.load(f)
        with open(dc_dir / f"{prior_date}.json", encoding="utf-8") as f:
            prev = json.load(f)
        changes = diff_depth_charts(cur, prev)

        # Gather news titles from today's report for context linkage.
        news_items: list[dict] = []
        try:
            report = load_report(date_str)
            for section in (report.sections or {}).values():
                if isinstance(section, dict):
                    news_items.extend(section.get("sources") or [])
                    news_items.extend(section.get("numbered_sources") or [])
            for highlight in (report.team_highlights or {}).values():
                if isinstance(highlight, dict):
                    news_items.extend(highlight.get("sources") or [])
        except Exception:
            pass

        annotate_depth_changes(changes, news_items)
    except Exception as e:
        logger.warning("Could not compute depth chart diff: %s", e)
        flows.append(
            Paragraph(
                f"Could not compute depth chart diff: {_escape(str(e))}",
                styles["body"],
            )
        )
        return flows

    if not changes:
        flows.append(
            Paragraph(
                f"No depth chart changes vs {prior_date}.",
                styles["body"],
            )
        )
        return flows

    flows.append(
        Paragraph(
            f"Compared vs {prior_date} · {len(changes)} total changes",
            styles["caption"],
        )
    )
    flows.append(Spacer(1, 6))

    buckets = {
        "promoted": "Promotions",
        "demoted": "Demotions",
        "added": "Added",
        "removed": "Removed",
        "team_change": "Team Changes",
        "position_change": "Position Changes",
    }
    for change_type, heading in buckets.items():
        group = [c for c in changes if c.get("type") == change_type]
        if not group:
            continue
        flows.append(
            Paragraph(f"{heading} ({len(group)})", styles["subsection"])
        )
        for c in group[:40]:
            message = _escape(c.get("message", ""))
            context = c.get("context", "")
            if context:
                message = (
                    f"{message}<br/>"
                    f"<i>Context: {_escape(context)}</i>"
                )
            flows.append(Paragraph(f"• {message}", styles["source"]))
        if len(group) > 40:
            flows.append(
                Paragraph(
                    f"... and {len(group) - 40} more",
                    styles["caption"],
                )
            )
        flows.append(Spacer(1, 4))
    return flows


def _render_stats(report, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """Render collection stats + LLM usage at the end of the PDF."""
    flows: list[Any] = []

    if report.collection_stats:
        flows.append(Paragraph("Collection Stats", styles["section"]))
        parts = [
            f"{src.upper()}: {count}"
            for src, count in report.collection_stats.items()
        ]
        flows.append(Paragraph(" &nbsp;·&nbsp; ".join(parts), styles["body"]))

    if report.llm_usage:
        usage = report.llm_usage
        flows.append(Paragraph("LLM Usage", styles["section"]))
        lines = [
            f"<b>Provider:</b> {_escape(usage.get('provider', 'unknown'))}",
            f"<b>Model:</b> {_escape(usage.get('model', 'unknown'))}",
            f"<b>Calls:</b> {usage.get('request_count', 0)}",
            f"<b>Input tokens:</b> {usage.get('input_tokens', 0):,}",
            f"<b>Output tokens:</b> {usage.get('output_tokens', 0):,}",
            f"<b>Est. cost:</b> ${float(usage.get('estimated_cost_usd', 0) or 0):.4f}",
        ]
        flows.append(Paragraph(" &nbsp;·&nbsp; ".join(lines), styles["body"]))

    flows.append(Spacer(1, 10))
    flows.append(
        Paragraph(
            f"Generated: {_escape(report.generated_at)}",
            styles["caption"],
        )
    )
    return flows


def build_daily_pdf(date_str: str) -> bytes:
    """Build a PDF for the given report date and return its bytes.

    Includes: news sections, team highlights, projection changes (from
    changelog.csv), depth chart diff vs the most recent prior snapshot,
    and collection/LLM stats.
    """
    report = load_report(date_str)
    styles = _styles()

    buf = BytesIO()
    doc = _doc_template(buf, f"NFL Daily Report — {date_str}")

    story: list[Any] = []
    story.extend(_brand_title_block(
        f"Daily Report  ·  {date_str}",
        f"Generated {report.generated_at}",
    ))

    story.extend(_render_report_sections(report, styles))
    story.extend(_render_team_highlights(report, styles))
    story.append(PageBreak())
    story.extend(_render_projection_changes(date_str, styles))
    story.append(Spacer(1, 10))
    story.extend(_render_depth_chart_changes(date_str, styles))
    story.append(Spacer(1, 10))
    story.extend(_render_stats(report, styles))

    doc.build(story, onFirstPage=_draw_brand_footer, onLaterPages=_draw_brand_footer)
    return buf.getvalue()


def build_flagged_pdf(flags: list[dict[str, Any]] | None = None) -> bytes:
    """Build a PDF of flagged findings, grouped by category.

    Pass `flags` to render a pre-filtered subset; pass None to load the
    full store from data/flagged_findings.json.
    """
    from reports.flagged_findings import load_flags

    if flags is None:
        flags = load_flags()

    # Cache lazily-resolved numbered_sources per (date, section) for legacy
    # flags that pre-date the on-flag source capture.
    _section_src_cache: dict[tuple[str, str], list[dict]] = {}

    def _resolve_sources(flag: dict) -> list[dict]:
        stored = flag.get("sources") or []
        if stored:
            return stored
        section = flag.get("section", "")
        date = flag.get("report_date", "")
        if not section or section.startswith("team:") or not date:
            return []
        cache_key = (date, section)
        if cache_key not in _section_src_cache:
            try:
                rep = load_report(date)
                sd = (rep.sections or {}).get(section, {}) or {}
                _section_src_cache[cache_key] = sd.get("numbered_sources") or []
            except Exception:
                _section_src_cache[cache_key] = []
        full = _section_src_cache[cache_key]
        if not full:
            return []
        nums = set()
        for m in _CITE_RE.finditer(flag.get("content", "")):
            for n in re.split(r"[,\s]+", m.group(1)):
                n = n.strip()
                if n:
                    nums.add(n)
        if not nums:
            return []
        return [s for s in full if str(s.get("num", "")) in nums]

    styles = _styles()
    buf = BytesIO()
    doc = _doc_template(buf, "NFL Flagged Findings — Internal FP Handbook")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    story: list[Any] = []
    story.extend(_brand_title_block(
        "Internal FP Handbook",
        f"Generated {generated_at}  ·  {len(flags)} item(s)",
    ))

    if not flags:
        story.append(Paragraph("No flagged items.", styles["body"]))
        doc.build(story, onFirstPage=_draw_brand_footer, onLaterPages=_draw_brand_footer)
        return buf.getvalue()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for f in flags:
        grouped.setdefault(f.get("category", "Uncategorized"), []).append(f)

    for idx, category in enumerate(sorted(grouped), start=1):
        story.append(Spacer(1, 6))
        story.append(_brand_section_bar(category, f"{idx:02d}"))
        story.append(Spacer(1, 6))

        items = sorted(grouped[category], key=lambda x: x.get("report_date", ""), reverse=True)
        for f in items:
            report_date = f.get("report_date", "")
            section_label = f.get("section_label", f.get("section", ""))
            team = f.get("team", "")
            content = f.get("content", "")
            note = f.get("note", "")
            sources = _resolve_sources(f)

            team_chunk = f" &nbsp;·&nbsp; <b>{_escape(team)}</b>" if team else ""
            header = (
                f"<b>{_escape(report_date)}</b>{team_chunk}"
                f" &nbsp;·&nbsp; <i>{_escape(section_label)}</i>"
            )
            story.append(Paragraph(header, styles["body"]))

            url_map = {str(s.get("num", "")): s.get("url", "") for s in sources if s.get("num") and s.get("url")}
            story.extend(_render_markdown(content, styles, url_map or None))

            if note:
                story.append(Paragraph(
                    f"<b>Note:</b> {_inline_markdown(note)}",
                    styles["source"],
                ))
            if sources:
                src_lines = []
                for s in sources:
                    title_text = s.get("title", "Source") or "Source"
                    source_name = s.get("source", "") or ""
                    url = s.get("url", "") or ""
                    num = s.get("num", "")
                    prefix = f"<b>[{num}]</b> " if num else "• "
                    suffix = f" ({_escape(source_name)})" if source_name else ""
                    if url:
                        src_lines.append(
                            f'{prefix}<link href="{_escape(url)}" color="blue">{_escape(title_text)}</link>{suffix}'
                        )
                    else:
                        src_lines.append(f"{prefix}{_escape(title_text)}{suffix}")
                story.append(Paragraph(
                    "Sources: " + " &nbsp; ".join(src_lines),
                    styles["source"],
                ))
            story.append(Spacer(1, 6))

    doc.build(story, onFirstPage=_draw_brand_footer, onLaterPages=_draw_brand_footer)
    return buf.getvalue()


def build_daily_site_pdf(flags: list[dict[str, Any]] | None = None) -> bytes:
    """Build a Daily Site Report PDF from per-day flagged items.

    DSR flags carry only team + note (no category, no attribution).
    Output groups by team and orders by report_date within each team
    so a reader can scan one team's daily-site notes top to bottom.
    Pass `flags` to render a pre-filtered subset; pass None to load
    the full store and filter to MODE_DAILY entries.
    """
    from reports.flagged_findings import MODE_DAILY, MODE_HANDBOOK, load_flags

    if flags is None:
        all_flags = load_flags()
        flags = [
            f for f in all_flags
            if (f.get("mode") or MODE_HANDBOOK) == MODE_DAILY
        ]

    _section_src_cache: dict[tuple[str, str], list[dict]] = {}

    def _resolve_sources(flag: dict) -> list[dict]:
        stored = flag.get("sources") or []
        if stored:
            return stored
        section = flag.get("section", "")
        date = flag.get("report_date", "")
        if not section or section.startswith("team:") or not date:
            return []
        cache_key = (date, section)
        if cache_key not in _section_src_cache:
            try:
                rep = load_report(date)
                sd = (rep.sections or {}).get(section, {}) or {}
                _section_src_cache[cache_key] = sd.get("numbered_sources") or []
            except Exception:
                _section_src_cache[cache_key] = []
        full = _section_src_cache[cache_key]
        if not full:
            return []
        nums = set()
        for m in _CITE_RE.finditer(flag.get("content", "")):
            for n in re.split(r"[,\s]+", m.group(1)):
                n = n.strip()
                if n:
                    nums.add(n)
        if not nums:
            return []
        return [s for s in full if str(s.get("num", "")) in nums]

    styles = _styles()
    buf = BytesIO()
    doc = _doc_template(buf, "NFL Daily Site Report")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    dates = sorted({f.get("report_date", "") for f in flags if f.get("report_date")})
    if len(dates) == 1:
        coverage = dates[0]
    elif dates:
        coverage = f"{dates[0]} → {dates[-1]} ({len(dates)} days)"
    else:
        coverage = "—"

    story: list[Any] = []
    story.extend(_brand_title_block(
        f"Daily Site Report  ·  {coverage}",
        f"Generated {generated_at}  ·  {len(flags)} item(s)",
    ))

    if not flags:
        story.append(Paragraph("No Daily Site Report items.", styles["body"]))
        doc.build(story, onFirstPage=_draw_brand_footer, onLaterPages=_draw_brand_footer)
        return buf.getvalue()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for f in flags:
        team_key = f.get("team") or "(no team)"
        grouped.setdefault(team_key, []).append(f)

    for idx, team_key in enumerate(sorted(grouped), start=1):
        story.append(Spacer(1, 6))
        story.append(_brand_section_bar(team_key, f"{idx:02d}"))
        story.append(Spacer(1, 6))

        items = sorted(
            grouped[team_key],
            key=lambda x: (x.get("report_date", ""), x.get("flagged_at", "")),
            reverse=True,
        )
        for f in items:
            report_date = f.get("report_date", "")
            section_label = f.get("section_label", f.get("section", ""))
            content = f.get("content", "")
            note = f.get("note", "")
            sources = _resolve_sources(f)

            header = (
                f"<b>{_escape(report_date)}</b> &nbsp;·&nbsp; "
                f"<i>{_escape(section_label)}</i>"
            )
            story.append(Paragraph(header, styles["body"]))

            url_map = {
                str(s.get("num", "")): s.get("url", "")
                for s in sources if s.get("num") and s.get("url")
            }
            story.extend(_render_markdown(content, styles, url_map or None))

            if note:
                story.append(Paragraph(
                    f"<b>Note:</b> {_inline_markdown(note)}",
                    styles["source"],
                ))
            if sources:
                src_lines = []
                for s in sources:
                    title_text = s.get("title", "Source") or "Source"
                    source_name = s.get("source", "") or ""
                    url = s.get("url", "") or ""
                    num = s.get("num", "")
                    prefix = f"<b>[{num}]</b> " if num else "• "
                    suffix = f" ({_escape(source_name)})" if source_name else ""
                    if url:
                        src_lines.append(
                            f'{prefix}<link href="{_escape(url)}" color="blue">{_escape(title_text)}</link>{suffix}'
                        )
                    else:
                        src_lines.append(f"{prefix}{_escape(title_text)}{suffix}")
                story.append(Paragraph(
                    "Sources: " + " &nbsp; ".join(src_lines),
                    styles["source"],
                ))
            story.append(Spacer(1, 6))

    doc.build(story, onFirstPage=_draw_brand_footer, onLaterPages=_draw_brand_footer)
    return buf.getvalue()


if __name__ == "__main__":
    # Quick smoke test from CLI
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("date", help="Report date (YYYY-MM-DD)")
    parser.add_argument("--out", default=None, help="Output PDF path")
    args = parser.parse_args()

    pdf_bytes = build_daily_pdf(args.date)
    out_path = Path(args.out or f"daily_{args.date}.pdf")
    out_path.write_bytes(pdf_bytes)
    print(f"Wrote {out_path} ({len(pdf_bytes):,} bytes)")
