"""dashboard/module_refresh.py — a running dashboard picks up pushed code."""

import importlib
import os
import sys
import time

from dashboard import module_refresh as mr


def _make_pkg(root, value):
    pkg = root / "fakepkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helpers.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    (pkg / "user.py").write_text("from fakepkg.helpers import VALUE\n", encoding="utf-8")
    return pkg


def _bump(path, seconds):
    t = time.time() + seconds
    os.utime(path, (t, t))


def test_changed_module_is_reimported_and_dependents_follow(tmp_path, monkeypatch):
    pkg = _make_pkg(tmp_path, 1)
    monkeypatch.setattr(mr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(mr, "_seen", {})
    monkeypatch.setattr(mr, "_START", time.time() + 60)     # files predate the "process"
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        from fakepkg import user
        assert user.VALUE == 1
        assert mr.refresh_changed_modules() == []          # first sight, nothing changed

        (pkg / "helpers.py").write_text("VALUE = 2\n", encoding="utf-8")
        _bump(pkg / "helpers.py", 120)
        changed = mr.refresh_changed_modules()
        assert changed == ["fakepkg.helpers"]
        assert "fakepkg.user" not in sys.modules            # dependents dropped too
        from fakepkg import user as fresh                    # `from pkg import mod` re-imports
        assert fresh.VALUE == 2
        assert mr.refresh_changed_modules() == []           # and then it settles
    finally:
        for name in [n for n in sys.modules if n == "fakepkg" or n.startswith("fakepkg.")]:
            sys.modules.pop(name, None)


def test_file_changed_after_process_start_is_refreshed_on_first_sight(tmp_path, monkeypatch):
    """The deploy that introduces the watcher finds modules loaded before the
    push already in memory: anything newer than the process gets refreshed once."""
    pkg = _make_pkg(tmp_path, 1)
    monkeypatch.setattr(mr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(mr, "_seen", {})
    monkeypatch.setattr(mr, "_START", time.time() - 60)     # process older than the files
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        importlib.import_module("fakepkg.helpers")
        assert "fakepkg.helpers" in mr.refresh_changed_modules()   # (the package __init__ too)
        importlib.import_module("fakepkg.helpers")
        assert mr.refresh_changed_modules() == []
        assert pkg.exists()
    finally:
        for name in [n for n in sys.modules if n == "fakepkg" or n.startswith("fakepkg.")]:
            sys.modules.pop(name, None)


def test_third_party_modules_are_never_touched(monkeypatch):
    monkeypatch.setattr(mr, "_seen", {})
    names = mr._project_modules()
    assert "streamlit" not in names and "json" not in names
    assert mr._SELF not in names
