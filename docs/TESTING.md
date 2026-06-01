# Testing

A `pytest` suite covers the pure, high-risk logic whose breakage would
silently degrade report quality (wrong source attribution, fluff leaking
through, mis-tagged teams, scrambled projection columns).

## Run it

```bash
# one-time: add the test dependency to the conda env
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe -m pip install -r requirements-dev.txt

# run the suite
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe -m pytest
```

Config lives in `pytest.ini` (testpaths = `tests/`). `tests/conftest.py` puts
the project root on `sys.path` (so imports match the runtime) and provides two
fixtures: `make_item` (a `NewsItem` factory) and `fake_teams_by_abbr` (a minimal
team map built around the `CAR`/`WAS` abbreviation-collision edge cases).

## What's covered

| File | Module under test | Key guarantees |
|------|-------------------|----------------|
| `test_quality_filter.py` | `processing.quality_filter` | drop-pattern matching, mock-draft keep-year logic, disabled-filter passthrough (config monkeypatched, not coupled to live settings) |
| `test_deduplicator.py` | `processing.deduplicator` | title normalization, transaction-name overlap (different players never merge), `pick_primary` ranking (ESPN beats SBN even with a longer blog body), `flatten_groups` "Also reported by" note |
| `test_team_detection.py` | `collectors.rss_collector._detect_teams` | full-name/nickname matching; abbreviations only match as uppercase tokens, so "car crash"→not CAR and "was traded"→not WAS |
| `test_projection_colmap.py` | `scripts.snapshot_projections._build_player_col_map` | duplicate "YPA Adj" disambiguated to "Scramble YPA Adj"/"Pass YPA Adj", skip-headers excluded, column indices preserved |
| `test_citations.py` | `dashboard.citations.build_citation_linker` | `[N]` / `[1, 2]` linkified to source URLs, unknown numbers pass through, int `num` coerced to string |
| `test_yt_cache.py` | `processing.yt_cache` | cache key deterministic + order-independent, changes with range/videos, save/load round-trip, corrupt-file tolerance |

The `deduplicate()` tests are written to hold **regardless of whether
sentence-transformers loads** — the transaction-name gate runs before any
similarity scoring, so those assertions are backend-independent. (The model
*is* installed in the env, so the embedding path is what actually runs.)

## CI

`.github/workflows/tests.yml` runs the suite on every push to `master` and on
pull requests (Python 3.12, installs `requirements.txt` + `requirements-dev.txt`,
caches HF models). It is independent of the daily data-pipeline workflow.

## Where to extend next

Good next candidates, all pure or near-pure: `cross_day_filter` thresholding,
`reddit_collector._should_skip` / `_extract_reporter`, the summarizer's
`_press_relevance_score`, and `depth_chart_collector.diff_depth_charts`.
