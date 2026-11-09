# Postseason live activation attempt — 2026-09-30

**Outcome:** failed before a postseason report was produced. This was a research-only activation attempt; it created no trace, prospective receipt, or primary claim.

The existing `daily_top3` command was run with explicit `postseason-research-v1` scope. Approximate run window was 15:21–15:22 UTC; exact seconds were not captured. It exited **2** under Python 3.11.15 (`/Users/taejunoh/.local/bin/python3`) with this stderr:

```text
Daily Top 3 integrity failure: research schedule duplicate coverage gamePk
```

Exact command:

```sh
PYTHONPATH="/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix" PYTHONDONTWRITEBYTECODE=1 python3 -B -m mlb_backtest.daily_top3 --root "/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix" --date 2026-09-30 --competition-scope postseason-research-v1
```

The bounded, read-only diagnostic request used the official MLB Stats API schedule endpoint:

`https://statsapi.mlb.com/api/v1/schedule?sportId=1&gameType=R%2CF%2CD%2CL%2CW&startDate=2026-01-01&endDate=2026-09-29&hydrate=probablePitcher%2Cvenue%2Clinescore`

Repeated IDs included:

- `823042`: 2026-06-25 `Postponed`; 2026-07-23 `Final`
- `823062`: 2026-05-05 `Postponed`; 2026-07-07 `Final`
- `823357`: 2026-07-10 `Postponed`; 2026-07-11 `Final`
- `823471`: 2026-04-29 `Postponed`; 2026-04-30 `Final`

The immediate cause is the raw duplicate-ID rejection in `mlb_backtest/live_history.py::_research_schedule`, which runs before the established `parse_schedule` canonicalizer. That parser collapses exact normalized duplicates, allows a played (`Final`/`Completed Early`) occurrence to supersede an unplayed occurrence after checking game/team identity, and rejects conflicting played identities. The postseason coverage contract also explicitly requires unique canonical game IDs and validates that full-range Statcast population and game date/type agree with canonical schedule identities.

The minimal repair scope is to normalize repeated schedule occurrences using the established strict canonicalization rules before deriving research coverage, while retaining the unique-ID, conflict-rejection, played-population, and Statcast identity/date/type checks. No source change, retry, or new test was made here.

No successful report, trace, prospective pregame receipt, or primary write exists from this attempt. The target report and prospective audit directories were absent. The narrowly checked history cache/receipt namespaces had no files; their existing directory timestamps do not show that this run created them. No target-date outcome or score is included. Activation remains incomplete and requires an authorized repair and verification before a fresh live attempt.

Machine-readable evidence: [postseason-live-activation-attempt-20260930T1521Z.json](postseason-live-activation-attempt-20260930T1521Z.json).
