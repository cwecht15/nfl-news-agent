"""Local pipeline runner + PDF export widgets for the Home page.

Moved out of ``dashboard/app.py`` when the entrypoint became a thin
``st.navigation`` router: anything rendered in the entrypoint shows on every
page, and the run button / live progress belong on Home only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from dashboard.helpers import running_locally

PROJECT_ROOT = Path(__file__).parent.parent
STATUS_FILE = PROJECT_ROOT / "data" / "pipeline_status.json"

# Markers written by scripts/run_daily.py via write_status(). The in-season
# sub-steps (5b/5c/5e/5d) sit between Step 5 and Step 6 in run order; market
# lines (2c) run before summarization so Team Notes can use them.
_STEPS = [
    ("Step 1", "Collecting from all sources"),
    ("Step 2", "Deduplicating stories"),
    ("Step 2c", "Reading market lines"),
    ("Step 3", "Summarizing"),
    ("Step 4", "Snapshotting projections"),
    ("Step 5", "Updating depth charts"),
    ("Step 5b", "Updating roster state"),
    ("Step 5c", "Collecting injury reports"),
    ("Step 5e", "Checking game-day inactives"),
    ("Step 5d", "Auditing weekly projections"),
    ("Step 6c", "Building Line Movement section"),
    ("Step 6", "Building daily report"),
    ("Step 7", "Cleaning up old data"),
]
_STEP_INDEX = {marker: i for i, (marker, _) in enumerate(_STEPS)}
# Whole-token match so "Step 5" never swallows "Step 5b".
_STEP_TOKEN = re.compile(r"\bStep (\d+[a-z]?)\b")

# Patterns to extract useful info from log lines
_HIGHLIGHT_PATTERNS = [
    (re.compile(r"Collected (\d+) items from RSS"), "RSS: {} items"),
    (re.compile(r"Collected (\d+) items from web"), "Web: {} items"),
    (re.compile(r"Collected (\d+) transcripts total"), "YouTube: {} transcripts"),
    (re.compile(r"Deduplicated: .* -> (\d+) unique"), "{} unique stories"),
    (re.compile(r"Collection complete: (\d+ news items, \d+ transcripts)"), "{}"),
    (re.compile(r"LLM summary: (.+)"), "LLM: {}"),
    (re.compile(r"Stats: (.+)"), "Final: {}"),
]


def _read_status() -> dict | None:
    """Read pipeline status file. Returns None if not running."""
    if not STATUS_FILE.exists():
        return None
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if pid:
            if _pid_alive(pid):
                return data
            # Process is gone — stale status file
            STATUS_FILE.unlink(missing_ok=True)
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    """Cross-platform check: is the given PID currently running?"""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — count as alive.
        return True
    except OSError:
        return False


def _step_index(step_name: str) -> int:
    """Return the 0-based index for a step name like 'Step 3' / 'Step 5b'."""
    m = _STEP_TOKEN.search(step_name or "")
    if not m:
        return -1
    return _STEP_INDEX.get(f"Step {m.group(1)}", -1)


def _current_step_from_line(line: str) -> int | None:
    """Return step index if the line announces a pipeline step."""
    idx = _step_index(line)
    return idx if idx >= 0 else None


def _extract_detail(line: str) -> str | None:
    """Pull a short detail string from a log line."""
    for pattern, fmt in _HIGHLIGHT_PATTERNS:
        m = pattern.search(line)
        if m:
            return fmt.format(m.group(1)) if fmt else None
    return None


def _show_pipeline_already_running(status: dict):
    """Show status for a pipeline that's already running (e.g. after page refresh)."""
    step_name = status.get("step", "Starting")
    detail = status.get("detail", "")
    started = status.get("started_at", "")
    step_idx = _step_index(step_name)

    progress_pct = max(int((step_idx / len(_STEPS)) * 100), 5) if step_idx >= 0 else 5
    st.progress(progress_pct, text=f"Pipeline running — {detail or step_name}...")

    for i, (_, label) in enumerate(_STEPS):
        if i < step_idx:
            st.success(f"**{label}** — done")
        elif i == step_idx:
            st.info(f"**{label}** — in progress...  \n{detail}")

    if started:
        try:
            start_dt = datetime.fromisoformat(started)
            elapsed = datetime.now() - start_dt
            mins = int(elapsed.total_seconds() // 60)
            secs = int(elapsed.total_seconds() % 60)
            st.caption(f"Running for {mins}m {secs}s (started {start_dt.strftime('%I:%M %p')})")
        except ValueError:
            pass

    st.info("The pipeline is running. This page will update automatically.")
    time.sleep(3)
    st.rerun()


def _run_pipeline_with_progress(lookback_hours: int | None = None):
    """Launch the pipeline subprocess and stream progress to the main page."""
    progress_bar = st.progress(0, text="Starting pipeline...")

    step_placeholders = [st.empty() for _ in _STEPS]
    active_step = -1
    details: list[str] = []
    error_lines: list[str] = []

    cmd = [sys.executable, "-u", str(PROJECT_ROOT / "scripts" / "run_daily.py")]
    if lookback_hours:
        cmd += ["--lookback-hours", str(int(lookback_hours))]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(PROJECT_ROOT),
        bufsize=1,
    )

    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue

        step_idx = _current_step_from_line(line)
        if step_idx is not None and step_idx != active_step:
            if 0 <= active_step < len(_STEPS):
                step_placeholders[active_step].success(
                    f"**{_STEPS[active_step][1]}** — done"
                )
            active_step = step_idx
            pct = int((active_step / len(_STEPS)) * 100)
            progress_bar.progress(pct, text=_STEPS[active_step][1] + "...")
            step_placeholders[active_step].info(
                f"**{_STEPS[active_step][1]}**..."
            )

        detail = _extract_detail(line)
        if detail:
            details.append(detail)
            if 0 <= active_step < len(_STEPS):
                detail_text = "  \n".join(
                    d for d in details if not d.startswith("Final:")
                )
                step_placeholders[active_step].info(
                    f"**{_STEPS[active_step][1]}**  \n{detail_text}"
                )

        if "[ERROR]" in line or "[WARNING]" in line:
            short = re.sub(r"^\S+ \S+ \[\w+\] [\w.]+: ", "", line)
            error_lines.append(short)

    proc.wait()

    if 0 <= active_step < len(_STEPS):
        step_placeholders[active_step].success(
            f"**{_STEPS[active_step][1]}** — done"
        )
    for i in range(active_step + 1, len(_STEPS)):
        step_placeholders[i].empty()

    if proc.returncode == 0:
        progress_bar.progress(100, text="Pipeline complete!")
        final_details = [d for d in details if not d.startswith("Final:")]
        stats = next((d for d in details if d.startswith("Final:")), None)
        summary_parts = []
        if stats:
            summary_parts.append(stats.replace("Final: ", ""))
        if final_details:
            summary_parts.append(" | ".join(final_details))
        st.success(
            "**Pipeline complete!** "
            + (" — ".join(summary_parts) if summary_parts else "")
            + "  \nOpen the **Daily Report** page to see the new report."
        )
    else:
        progress_bar.progress(100, text="Pipeline failed")
        st.error("**Pipeline failed.** Check the errors below.")

    if error_lines:
        with st.expander(
            f"Warnings & Errors ({len(error_lines)})",
            expanded=proc.returncode != 0,
        ):
            for err in error_lines:
                st.text(err)


def render_pdf_export():
    """Render a PDF export widget so users can download any report as a PDF."""
    from reports.report_builder import list_available_reports
    from reports.pdf_exporter import build_daily_pdf

    available = list_available_reports()
    if not available:
        return

    st.subheader("Download PDF Report")
    st.caption(
        "Combines news sections, team highlights, projection changes, "
        "and depth chart diffs into a single PDF."
    )

    col_date, col_btn = st.columns([2, 1])
    with col_date:
        selected = st.selectbox("Report date", available, index=0, key="pdf_date")
    with col_btn:
        st.write("")  # vertical alignment
        generate = st.button("Build PDF", use_container_width=True)

    pdf_key = f"pdf_bytes_{selected}"
    if generate:
        try:
            with st.spinner(f"Building PDF for {selected}..."):
                st.session_state[pdf_key] = build_daily_pdf(selected)
        except Exception as e:
            st.error(f"Failed to build PDF: {e}")
            return

    if pdf_key in st.session_state:
        st.download_button(
            label=f"Download {selected}.pdf",
            data=st.session_state[pdf_key],
            file_name=f"nfl_daily_report_{selected}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )


def render_sidebar_controls() -> bool:
    """Sidebar "Run Pipeline" block. Returns True when it took over the main
    pane (a run is in progress or was just launched), so the caller should
    skip its normal content.
    """
    st.sidebar.divider()
    st.sidebar.subheader("Run Pipeline")

    if not running_locally():
        st.sidebar.info(
            "Pipeline runs aren't available from this dashboard.\n\n"
            "Daily updates land automatically at **10:00 UTC** via GitHub "
            "Actions (plus the 22:00 UTC afternoon refresh and game-day "
            "inactives polls in-season). To trigger an off-cycle run, open "
            "the repo's **Actions** tab and use the *Daily NFL News pipeline* "
            "workflow's `workflow_dispatch` button.\n\n"
            "For YouTube transcripts, run `scripts/collect_youtube.py` "
            "locally and push the results — the **YouTube Report** page will "
            "pick them up."
        )
        return False

    existing_status = _read_status()
    pipeline_running = existing_status is not None

    run_clicked = st.sidebar.button(
        "Collect & Generate Report",
        type="primary",
        use_container_width=True,
        disabled=pipeline_running,
    )

    with st.sidebar.expander("Catch-up mode", expanded=False):
        st.caption(
            "If the PC was off for a while, widen the collection window on "
            "this run so more of the missed news (still in RSS) gets picked "
            "up. Leave at 0 to use the default 28-hour lookback."
        )
        catchup_hours = st.number_input(
            "Extra lookback (hours)",
            min_value=0,
            max_value=240,
            value=0,
            step=12,
            help="24 = 1 day, 72 = 3 days. Max 240 = 10 days.",
            key="catchup_hours",
            disabled=pipeline_running,
        )
        catchup_clicked = st.button(
            "Run catch-up now",
            use_container_width=True,
            disabled=pipeline_running or catchup_hours == 0,
            key="catchup_run",
        )

    if pipeline_running and not (run_clicked or catchup_clicked):
        _show_pipeline_already_running(existing_status)
        return True
    if run_clicked:
        _run_pipeline_with_progress()
        return True
    if catchup_clicked:
        _run_pipeline_with_progress(lookback_hours=int(catchup_hours))
        return True
    return False
