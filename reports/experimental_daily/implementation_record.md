# v5.1 Experimental Live Daily Top 3 — Implementation Record

> **RESEARCH ONLY · NON-OFFICIAL SAMPLE · PROFITABILITY NOT VALIDATED**

## Scope and release status

- Governing design: `docs/superpowers/specs/2026-07-21-v5-1-experimental-live-top3-design.md`.
- Governing plan: `docs/superpowers/plans/2026-07-21-v5-1-experimental-live-top3.md`, Task 9.
- Implementation base/HEAD before Task 9 documentation:
  `afeea6a8d13600c726a8cb08f4aee959374a9086`.
- Implementation commits: `c2aa756`, `a56bef3`, `a7e10be`, `6fd3a35`,
  `3af7598`, and `61dbb26`. This record is committed immediately after that
  sequence; its own hash is intentionally not embedded as a self-reference.

The implementation and its verification gates are ready. The first successful
same-day report is pending external Baseball Savant publication for the exact
D-1 population. This record does **not** mark the live smoke successful.

The attempted command was:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.daily_top3
```

Target date was `2026-07-22` Eastern; the result-free cutoff was `2026-07-21`.
No `reports/experimental_daily/YYYY-MM-DD/` run directory or JSON/Markdown
ranking report was generated. Selected counts are therefore not applicable;
in every future valid report, `verified_top3` must be exactly empty.

## Real-run chronology

All Eastern times below are EDT (`UTC-04:00`).

1. **First bootstrap, 02:39–02:56 EDT (06:39–06:56 UTC).** The schedule
   candidate and several Statcast chunks were durably recorded. The run then
   failed during strict APFS regular-file revalidation. Residue is the orphan
   raw
   `data/experimental_daily/history_cache/baseball_savant_statcast_attempt/4e0db2ac9ad814e15d3a48dd7dd6fbd28118f471decb649d9e27a6a56a2199e6.raw`
   and lock
   `audit/experimental_daily/history_receipt_locks/history-history_statcast-5f9a7eab0e9da3ff4343bb2d7584ea41.lock`.
   Cwd was `/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest`;
   command was `PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.daily_top3`;
   exit code was `2`.
2. **Second bootstrap retry, 03:01–03:03 EDT (07:01–07:03 UTC).** It failed
   on the same APFS behavior after writing
   `data/experimental_daily/history_cache/baseball_savant_statcast_attempt/cc066684d6bcfefeeccf50807b658b76a15cadc1057a1fca9daa5007622e8870.raw`
   and its `93dc62d4c0b6efe096a03397c8dbc7f3` lock. After the fix, that exact residue
   was safely resumed and completed at 03:33:30 EDT.
   Cwd was `/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest`;
   command was `PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.daily_top3`;
   exit code was `2`.
3. **APFS root cause and fix.** APFS can expose one delayed `st_ctime_ns`
   transition after a durable write while bytes, inode, device, size, mode, and
   single-link status remain unchanged. The previous one-pass identity check
   treated that benign transition as tampering. The fix permits one bounded
   ctime-only stabilization transition, rereads and compares the exact bytes,
   and still rejects content/inode changes, aliasing, multiple transitions, and
   unstable state.
4. **Post-fix run, 19m40 observed wall time.** Evidence publication spans
   07:33:30–07:56:22 UTC because the run resumed the second failure's residue.
   It reached a broad `2026-07-01..2026-07-21` Savant response with exactly
   25,000 data rows; the stored manifest's `row_count` is 25,001 nonblank CSV
   rows including the header. The then-current ordering compared D-1 coverage
   before treating the response as capped and reported
   `STATCAST_D1_COVERAGE_DIFFERS`; no report was generated. Cwd was
   `/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest`;
   command was `PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.daily_top3`;
   exit code was `2`.
5. **Capped-query corrections.** Row-cap detection now precedes exact D-1
   coverage, capped parents split recursively, and neither exact-cap nor
   over-cap canonical parents can be selected or bound. Canonical selection is
   an exact, nonoverlapping partition of uncapped requested ranges.
6. **Final retry, 08:28:11–08:31:21 UTC (04:28:11–04:31:21 EDT).** The broad
   response had 25,000 data rows (25,001 manifest nonblank rows including the
   header) and split; its `07-01..07-11` child had the same counts and split
   again. `07-01..07-06` had 22,192 data rows (22,193 including the header), and
   `07-07..07-11` had 21,510 data rows (21,511 including the header); both were
   promoted canonically. The uncapped `07-12..07-21` child had 22,477 data rows
   (22,478 including the header) but contained no `2026-07-21` game PKs. The
   schedule required the following 13:
   `822787, 822871, 823114, 823437, 823760, 824005, 824085, 824165, 824329,
   824409, 824652, 824895, 825056`. Actual D-1 receipt PKs were `[]`, proving a
   true source-publication lag. The command correctly exited on
   `Statcast D-1 coverage differs` without producing a ranking report. Cwd was
   `/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest`;
   command was `PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.daily_top3`;
   exit code was `2`.

The receipt bounds and run counts are:

| Source/stage | Count | Response-complete UTC bound | Durable UTC bound |
|---|---:|---|---|
| Reused D-1 schedule candidate | 1 receipt | `2026-07-22T06:39:25.370087Z` | `2026-07-22T06:39:25.649681Z` |
| Final-retry Savant attempts | 5 receipts | `2026-07-22T08:28:11.29886Z`–`2026-07-22T08:31:17.637587Z` | `2026-07-22T08:28:15.002416Z`–`2026-07-22T08:31:21.467394Z` |
| Canonical Savant promotions in final retry | 2 ranges | `07-01..07-06`; `07-07..07-11` | persisted at `08:29:47Z`; `08:30:29Z` |
| Current-day schedule/participants/final revalidation | 0 | not reached | not reached |
| Ranking reports / selected rows | 0 / not applicable | no report | no report |

## Preserved live evidence

The authenticated schedule candidate is:

- raw: `data/experimental_daily/history_cache/mlb_schedule_attempt/b203658408f541c03c503695c860df1f1983e42dd0898c6261a04b9c1d7565f4.raw`
- manifest: `data/experimental_daily/history_cache/mlb_schedule_attempt/b203658408f541c03c503695c860df1f1983e42dd0898c6261a04b9c1d7565f4.dad93a6eb0427bf4505610ac532606492bdee74c8de59854e46208bea46f70dd.manifest.json`
- receipt: `audit/experimental_daily/history_receipts/history-history_schedule-dad93a6eb0427bf4505610ac53260649.json`
- completion: `audit/experimental_daily/history_receipts/history-history_schedule-dad93a6eb0427bf4505610ac53260649.completion.json`

The capped broad query that exposed the ordering bug is bound by:

- raw: `data/experimental_daily/history_cache/baseball_savant_statcast_attempt/e160570b6fc207f42e24b7e7f0f520fe0c9a9ca69bf7026bb38a8958c507394a.raw`
- manifest: `data/experimental_daily/history_cache/baseball_savant_statcast_attempt/e160570b6fc207f42e24b7e7f0f520fe0c9a9ca69bf7026bb38a8958c507394a.4eb08ed06952f0dba240bb9036c64ef32e3804c59afccf9c335b1f4e65aa167d.manifest.json`
- receipt: `audit/experimental_daily/history_receipts/history-history_statcast-4eb08ed06952f0dba240bb9036c64ef3.json`
- completion: `audit/experimental_daily/history_receipts/history-history_statcast-4eb08ed06952f0dba240bb9036c64ef3.completion.json`

The final retry's exact query evidence is:

| Range | Outcome | Data rows (header excluded) | Manifest `row_count` (nonblank rows, header included) | Attempt manifest | Receipt | Canonical promotion |
|---|---|---:|---:|---|---|---|
| `07-01..07-21` | `STATCAST_ROW_CAP_SPLIT_REQUIRED` | 25,000 | 25,001 | `data/experimental_daily/history_cache/baseball_savant_statcast_attempt/e160570b6fc207f42e24b7e7f0f520fe0c9a9ca69bf7026bb38a8958c507394a.7bedb9f733792b7a7351ea1dcdfd09346de368515f692187c6bba19a9da7210d.manifest.json` | `audit/experimental_daily/history_receipts/history-history_statcast-7bedb9f733792b7a7351ea1dcdfd0934.json` | none |
| `07-01..07-11` | `STATCAST_ROW_CAP_SPLIT_REQUIRED` | 25,000 | 25,001 | `data/experimental_daily/history_cache/baseball_savant_statcast_attempt/7ff3246b07994581f32dccaf3e587839fc53d30c1b8d50e6aab8b6f950588dec.204fa37a95775d51fa31942f3aa9b0f1c301e1d02dbfaf8a45695c4a8af35185.manifest.json` | `audit/experimental_daily/history_receipts/history-history_statcast-204fa37a95775d51fa31942f3aa9b0f1.json` | none |
| `07-01..07-06` | `COMPLETE_CANDIDATE` | 22,192 | 22,193 | `data/experimental_daily/history_cache/baseball_savant_statcast_attempt/1215663dc62bcd1c82d7968e51b18bb657b410df22919b87f939113fe5710dd4.afcb7c99c5e25c6844e3778feb9cee42ac96d36e20e9f3caf8cd35d427c12581.manifest.json` | `audit/experimental_daily/history_receipts/history-history_statcast-afcb7c99c5e25c6844e3778feb9cee42.json` | `data/experimental_daily/history_cache/baseball_savant_statcast/1215663dc62bcd1c82d7968e51b18bb657b410df22919b87f939113fe5710dd4.2eaddea46c5c08a9061c999b99f60fed14dcd09d263df91b8b93df8db2eb67bc.manifest.json` |
| `07-07..07-11` | `COMPLETE_CANDIDATE` | 21,510 | 21,511 | `data/experimental_daily/history_cache/baseball_savant_statcast_attempt/597c888e1b78578c33f1f4a28e3a5b8d8d4c61599691ed3f860a9b870b149319.94dd1857b5fc8623a9ead61a156a13d34b0ce1c859cb4e001fc13e5429535e85.manifest.json` | `audit/experimental_daily/history_receipts/history-history_statcast-94dd1857b5fc8623a9ead61a156a13d3.json` | `data/experimental_daily/history_cache/baseball_savant_statcast/597c888e1b78578c33f1f4a28e3a5b8d8d4c61599691ed3f860a9b870b149319.3b2fc8e9428dd6c6685174bbb4346b20e130e93f976888918731962e7855c9be.manifest.json` |
| `07-12..07-21` | `STATCAST_D1_COVERAGE_DIFFERS` | 22,477 | 22,478 | `data/experimental_daily/history_cache/baseball_savant_statcast_attempt/7bb2d5410425413a0ba4747ad5c2f3d3861823b8c69fc834d8b3c164e81fbe95.c884917c758cc734db8d9c80e28898ceb6fce4078a50cde35a2992419b7fc01a.manifest.json` | `audit/experimental_daily/history_receipts/history-history_statcast-c884917c758cc734db8d9c80e28898ce.json` | none |

Every listed live artifact was observed as a regular single-link file. No
successful report path exists yet.

## Verification and reviews

| Gate | Result |
|---|---|
| Focused Daily Top 3 and secure-filesystem gate | `307` tests, `OK` |
| Affected historical v5.1 regression gate | `181` tests, `OK` |
| Ruff | `All checks passed!` |
| `git diff --check` | Exit `0` |
| Protected v5.2 evidence manifest | `28/28` SHA-256 entries `OK` |
| Independent specification review | `APPROVED`; accepted states, boundaries, digests, failures, and non-goals conform |
| Independent code-quality/security review | `APPROVED`; no Critical or Important findings remain |

The exact focused/security command and result were:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_secure_fs tests.test_live_top3_contracts tests.test_live_model_card tests.test_live_sources tests.test_live_history tests.test_live_features tests.test_live_top3_service tests.test_daily_top3 tests.test_daily_top3_e2e tests.test_statcast tests.test_model_prep
```

Result: exit `0`; `Ran 307 tests`; `OK`.

The exact affected historical regression command and result were:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_feature_engine tests.test_scoring_prep tests.test_scoring tests.test_ranking tests.test_pipeline tests.test_leakage tests.test_sources tests.test_statcast tests.test_mlb_stats tests.test_career_bf -v
```

Result: exit `0`; `Ran 181 tests`; `OK`.

The exact static and protected-evidence commands and results were:

```bash
ruff check mlb_backtest tests
git diff --check
shasum -a 256 -c /tmp/mlb-v52-565534-audit.ywfx0C/old-evidence.sha256
```

Results: Ruff exited `0` with `All checks passed!`; `git diff --check`
exited `0` with no diagnostics; the checksum command exited `0` with all
`28/28` protected v5.2 entries reported `OK`.

## Protected evidence and limitations

The 28-file v5.2 evidence set bound by
`/tmp/mlb-v52-565534-audit.ywfx0C/old-evidence.sha256` remained byte-identical.
The following protected untracked duplicates were not edited, deleted, staged,
or treated as implementation inputs:

- `mlb_backtest/v52_query_plan 2.py`
- `tests/test_v52_query_plan 2.py`
- `reports/v5_2_qd/walkforward_500_preflight 2.md`
- `schemas/v5_2_preflight.schema 2.json`

This work performed no v5.2 acquisition or phase transition. It establishes
implementation behavior and preserved real-source failure evidence only. It
does not establish a successful same-day ranking, profitability, ROI, CLV,
betting actionability, or formal prospective validation. Rerun the one-command
smoke after Savant publishes all 13 required D-1 games; only a subsequent
schema-valid JSON/Markdown report can complete the first-report portion of
Task 9.
