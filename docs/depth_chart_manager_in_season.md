# Depth Chart Manager — In-Season Edits

The current Depth Chart Manager is built for the offseason. It treats
master `STATUS DESC = "Active"` as a single binary state ("on the active
roster"). Once the season starts you need a richer status model and at
least one new data source.

## Status definitions (per project convention)

| Status | Meaning | Tracked today? |
|---|---|---|
| **Active** | On the 53-man roster | yes |
| **In season** | Active on game day (one of the 48 dressed of the 53) | **no — this doc** |
| **Practice Squad** | On the practice squad (offseason: 90-man overflow) | partial |
| **Reserve/Injured (IR)** | On IR | yes (released-style flag only) |
| **Reserve/PUP, NFI, Suspended** | Inactive list | partial |

Today the tool only flags Active-but-shouldn't-be (Released, Waived,
Reserve/Injured, Retired, Terminated). Game-day inactives, PS elevations,
and short-term IR are not modeled.

## What needs to change in-season

### 1. New data source: weekly inactives list

NFL teams must submit inactives ~90 minutes before kickoff each game.
Sources, in order of reliability:
- **NFL.com Game Center** — JSON endpoint per game has the inactive list.
- **ESPN scoreboard API** — same data, may lag a few minutes.
- **OurLads** does not publish inactives.

**New collector:** `collectors/inactives_collector.py` polling on game days
(Thu evening / Sun afternoon / Mon evening). Output:
`data/inactives/<YYYY-MM-DD>.json` keyed by gsisId with team + game date.

### 2. Master sheet — confirm or add columns

The DepthCharts tab already has `STATUS DESC` (col 27). Check whether you
also use:
- A `Game-Day Status` column for In Season vs Inactive
- The existing `STATUS` (col 26, currently empty in offseason) — this may
  be the right place once games start

If the master sheet doesn't have a separate column today, decide whether
the Depth Chart Manager should compare against `STATUS DESC` only (and
expect "Inactive" for game-day scratches) or against a second column.

### 3. New discrepancy category: "Should be Inactive"

Add to `processing/sheet_reconciliation.py`:

```python
def compute_inactives_mismatches(
    master: dict[str, dict],
    inactives: dict[str, dict],   # gsisId -> {team, game_date}
) -> list[dict]:
    """Players the inactives collector marked inactive for the most recent
    game, but master STATUS DESC still says Active."""
```

Fold the result into `reconcile()` and add a fourth `st.tabs(...)` entry
in `dashboard/pages/depth_chart_manager.py`. Reuse the same dismissal
flow.

### 4. Expand `DEACTIVATING_TX_TYPES`

The constant at the top of `processing/sheet_reconciliation.py` covers
the offseason move types. In-season add:
- `reserve/pup` (Physically Unable to Perform)
- `reserve/nfi` (Non-Football Injury)
- `reserve/suspended`
- `practice-squad-injured`

Confirm the exact strings used by NFL.com transactions feed before adding
— a mismatch silently misses moves.

### 5. Practice-squad noise

Once PS exists (week 1), the master sheet may carry PS players with
`STATUS DESC = "Active"` (they are on a roster, just not the 53). The
agent's OurLads scrape lists them as `depth >= 4` with no PS flag.

**Handling:** master sheet should distinguish PS via a separate status
value (e.g. `"Practice Squad"`); add it to `ACTIVE_STATUSES` so PS
players don't trigger status mismatches by default. Then a PS player
released by Thursday is still flagged correctly because the deactivating
transaction would arrive.

### 6. Game-day elevations

PS players can be elevated for a game (max 3 times per season per
player). NFL.com transactions feed publishes this as a "Standard
Elevation" entry. These are *temporary* — the player reverts to PS after
the game.

**Handling:** track elevations as an enrichment, not a discrepancy. Don't
flag a PS player elevated for Sunday as "should be Active" — they're
elevated on a separate roster slot. The master sheet probably already
handles this in its own workflow; tool should ignore elevation
transactions when computing untracked transactions, or carry them in a
fourth informational tab.

## Code touchpoints summary

| File | Change |
|---|---|
| `processing/sheet_reconciliation.py` | Expand `DEACTIVATING_TX_TYPES`; add `compute_inactives_mismatches`; extend `reconcile()` return dict |
| `dashboard/pages/depth_chart_manager.py` | Add 4th tab; load inactives data; new metric |
| `collectors/inactives_collector.py` | **New** — weekly inactives scraper |
| `scripts/run_daily.py` | Wire inactives collector into game-day runs only |
| `config/settings.yaml` | Schedule for inactives collection |

## When to do this

Trigger: a few weeks before Week 1 (typically late August). The current
tool will keep working fine until then — Released/Waived/Reserve flows
are season-agnostic and remain useful through training camp cuts.

## What the tool will look like in-season

Four tabs instead of three:
1. Team Mismatches (unchanged)
2. Status Mismatches — now includes PS-vs-Active and PUP/NFI cases
3. **Should Be Inactive** (new) — game-day inactives the master sheet still shows Active
4. Untracked Transactions (unchanged)
