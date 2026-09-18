"""Pick up pushed code in a long-running dashboard process.

Streamlit Community Cloud pulls every push into the running container and
re-executes the entrypoint and page scripts from disk on each run — but the
project modules those scripts *import* stay cached in ``sys.modules`` from
whenever they were first loaded. On 2026-09-18 the Team page (a new script,
so current) called ``line_insights.game_card`` on a cached copy of
``processing/line_insights.py`` from the previous push, which predated the
function: AttributeError until the app was rebooted. A change that does not
raise is worse — the old code just keeps running.

``refresh_changed_modules()`` runs at the top of ``dashboard/app.py`` on every
run. When any project module's file is newer than the copy in memory, it drops
*every* project module (so nothing keeps a reference into an old one via
``from x import y``) and lets the scripts import them fresh. It never touches
third-party packages, and costs a stat per project module per run.

A module first seen by this watcher is compared with the process's start
time: a file modified after the process started may have been loaded before
the change, so it is refreshed once. That is what lets the very deploy that
introduces this file clean up the stale modules already in memory.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SELF = __name__
_seen: dict[str, float] = {}


def _process_start() -> float:
    """Epoch seconds this process started (Linux /proc; else import time)."""
    try:
        stat = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
        start_ticks = int(stat[19])          # field 22 overall; 20 after "pid (comm) state"
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                     if line.startswith("btime"))
        return btime + start_ticks / os.sysconf("SC_CLK_TCK")
    except Exception:  # noqa: BLE001 — not Linux: local runs have Streamlit's own watcher
        return time.time()


_START = _process_start()


def _project_modules() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name, mod in list(sys.modules.items()):
        if name in (_SELF, "__main__"):
            continue
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            p = Path(f).resolve()
        except (OSError, ValueError):
            continue
        if PROJECT_ROOT not in p.parents or "site-packages" in p.parts:
            continue
        out[name] = p
    return out


def refresh_changed_modules() -> list[str]:
    """Drop project modules when any of them changed on disk. Returns the
    names of the changed ones (empty on an ordinary run)."""
    modules = _project_modules()
    changed: list[str] = []
    for name, path in modules.items():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        prev = _seen.get(name)
        _seen[name] = mtime
        if (prev is None and mtime > _START) or (prev is not None and mtime != prev):
            changed.append(name)
    if not changed:
        return []
    for name in modules:
        sys.modules.pop(name, None)
        parent, _, child = name.rpartition(".")
        pkg = sys.modules.get(parent) if parent else None
        if pkg is not None and getattr(pkg, child, None) is not None:
            try:
                delattr(pkg, child)          # else `from pkg import child` returns the old one
            except AttributeError:
                pass
    logger.info("Code changed on disk (%s) - reloading %d project modules",
                ", ".join(sorted(changed)[:5]), len(modules))
    return changed
