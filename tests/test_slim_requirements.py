"""requirements-refresh.txt must cover everything the refresh path imports.

refresh.yml, injuries.yml and inactives.yml install a slim dependency set so a
dashboard Refresh isn't spending ~2m45s of a ~4m run installing torch,
sentence-transformers, openai-whisper and yt-dlp that no refresh mode loads.

That only stays true if nothing new sneaks a heavyweight import into the path.
These tests walk the *transitive* first-party import graph from
``scripts/run_afternoon.py`` — so a module added tomorrow is picked up without
anyone remembering to list it here — and fail if it reaches for a package the
slim set doesn't install.
"""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SLIM = ROOT / "requirements-refresh.txt"
FULL = ROOT / "requirements.txt"
WORKFLOWS = ROOT / ".github" / "workflows"

ENTRY = "scripts.run_afternoon"

FIRST_PARTY = {"collectors", "processing", "scripts", "reports", "dashboard",
               "config_loader", "models", "tests"}

# import name -> distribution name on PyPI
DIST_FOR_IMPORT = {
    "bs4": "beautifulsoup4",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "google": "google-auth",
    "dateutil": "python-dateutil",
    "PIL": "pillow",
}

# Packages whose whole point is the LLM / embedding / media stack. None are
# reachable from a refresh; if one becomes reachable, that is the bug.
HEAVY = {"torch", "sentence-transformers", "transformers", "openai", "anthropic",
         "streamlit", "openai-whisper", "yt-dlp", "reportlab", "numpy", "pandas",
         "scipy", "scikit-learn"}


def _requirements(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for sep in (">=", "==", "<=", "~=", ">", "<", "["):
            if sep in line:
                line = line.split(sep, 1)[0]
        names.add(line.strip().lower())
    return names


def _module_path(mod: str) -> Path | None:
    p = ROOT / Path(*mod.split("."))
    if p.with_suffix(".py").exists():
        return p.with_suffix(".py")
    if (p / "__init__.py").exists():
        return p / "__init__.py"
    return None


def _imports(path: Path) -> tuple[set[str], set[str]]:
    """``(module_scope, lazy)`` imported names.

    The split is the whole point. A module-scope import runs the moment the
    module is loaded, so a missing package is an immediate ImportError. An
    import inside a function only runs if that function is called — which is
    how ``processing/deduplicator.py`` can reference sentence-transformers and
    ``processing/summarizer.py`` can reference openai while a refresh, which
    calls neither, needs neither installed.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lazy_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                lazy_nodes.add(id(child))

    module_scope: set[str] = set()
    lazy: set[str] = set()
    for node in ast.walk(tree):
        names: set[str] = set()
        if isinstance(node, ast.Import):
            names = {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import: first-party by definition
                continue
            if node.module:
                names = {node.module}
        if not names:
            continue
        (lazy if id(node) in lazy_nodes else module_scope).update(names)
    return module_scope, lazy


def _classify(names: set[str], queue: list[str]) -> set[str]:
    out: set[str] = set()
    for imported in names:
        top = imported.split(".")[0]
        if top in FIRST_PARTY:
            queue.append(imported)
            # `from collectors.x import y` may name the package, not a module
            queue.append(imported.rsplit(".", 1)[0])
        elif top in sys.stdlib_module_names or top == "__future__":
            continue
        else:
            out.add(DIST_FOR_IMPORT.get(top, top).lower())
    return out


def _walk_refresh_path() -> tuple[set[str], set[str], set[str]]:
    """``(first-party modules reached, required dists, lazily-referenced dists)``."""
    seen: set[str] = set()
    required: set[str] = set()
    lazy_dists: set[str] = set()
    queue = [ENTRY]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        path = _module_path(mod)
        if path is None:
            continue
        seen.add(mod)
        module_scope, lazy = _imports(path)
        required |= _classify(module_scope, queue)
        lazy_dists |= _classify(lazy, queue)
    return seen, required, lazy_dists - required


def test_the_refresh_path_is_actually_reachable():
    modules, _req, _lazy = _walk_refresh_path()
    # Sanity: the walk found the collectors, not just the entry point.
    assert "collectors.injury_report_collector" in modules
    assert "collectors.nflverse_roster_collector" in modules
    assert "processing.projection_audit" in modules
    assert len(modules) > 15


def test_slim_requirements_cover_every_module_scope_import():
    """A module-scope import that isn't installed is an instant ImportError."""
    _modules, required, _lazy = _walk_refresh_path()
    have = _requirements(SLIM)
    missing = sorted(required - have)
    assert not missing, (
        f"requirements-refresh.txt is missing {missing}. Either add them, or move the "
        "import inside the function that needs it — refresh.yml / injuries.yml / "
        "inactives.yml install only the slim set, so this fails the run, not the install."
    )


def test_lazily_referenced_packages_are_all_known_optional():
    """A lazy import is fine *if* no refresh mode calls that function.

    sentence-transformers (embedding dedup), openai / anthropic (summarizer) and
    whisper / yt-dlp (YouTube) all sit behind functions a refresh never reaches,
    which is exactly why the slim set can leave them out. A NEW name showing up
    here is a decision someone has to make deliberately, so fail and make them.
    """
    _modules, _required, lazy = _walk_refresh_path()
    have = _requirements(SLIM)
    unexpected = sorted(lazy - have - HEAVY - {"sentence_transformers", "yt_dlp", "whisper"})
    assert not unexpected, (
        f"new lazily-imported package(s) on the refresh path: {unexpected}. If a refresh "
        "mode can reach that code, add it to requirements-refresh.txt; if it cannot, add "
        "it to this test's known-optional list with a note saying why."
    )


def test_slim_requirements_stay_slim():
    have = _requirements(SLIM)
    assert not (have & HEAVY), f"the slim set has picked up {sorted(have & HEAVY)}"
    assert have < _requirements(FULL) | {"tzdata"}, "slim should be a subset of the full set"


@pytest.mark.parametrize("workflow", ["refresh.yml", "injuries.yml", "inactives.yml"])
def test_fast_workflows_install_the_slim_set(workflow):
    body = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    assert "pip install -r requirements-refresh.txt" in body
    # The pip cache key has to follow, or the runner keeps restoring the heavy cache.
    assert "cache-dependency-path: requirements-refresh.txt" in body


@pytest.mark.parametrize("workflow", ["daily.yml", "in_season_pm.yml"])
def test_the_full_pipelines_keep_the_full_set(workflow):
    """These two do run the summarizer and the embedding deduplicator.

    podcasts.yml and twitter.yml are not here on purpose: they already install
    their own inline handful of packages, which is the pattern this change
    extends rather than invents.
    """
    body = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    assert "pip install -r requirements.txt" in body
