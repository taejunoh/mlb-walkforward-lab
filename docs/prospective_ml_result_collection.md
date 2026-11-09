# Official MLB results → prospective ML evaluation

This is a manual, single-pass bridge. It fetches official results for the
primary observations explicitly named by an existing prospective calibration
report. It does not create predictions, fit a model, change probability values,
calculate market returns, or enable recurring collection.

## First invocation for the seven September 2 observations

Run from the existing LFG worktree:

```sh
cd '/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix'
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B \
  -m mlb_backtest.prospective_ml_results collect \
  --root "$PWD" \
  --report reports/prospective_ml_observation/8f30a9e94dd3c22ecf2a2d62e962014b5f26a5d862c76fd470360cb19a5d0eeb.json
```

Use the report path returned by the command as `--report` on the next manual
invocation. Do not repeatedly use an old pending report after a settlement was
committed. The command refuses stale input rather than selecting an unlisted
settlement revision or creating a duplicate.

If no game has become Final, new source receipts can exist while the returned
report path stays the same: its evaluated population and metrics have not
changed. This is not a failed capture or a newly evaluated outcome.

## Meaning of the output

- **Final:** the official payload must match the saved gamePk, official date,
  home team and away team. Only this state creates a new settlement head.
- **Not Final:** the response is preserved but no win/loss is invented. The
  observation stays pending, including exceptional states requiring later
  resolution such as postponement or suspension.
- **Already settled:** an explicitly supplied terminal head is retained, with
  no additional request or automatic correction.
- **Failure:** invalid input evidence, network/HTTP failure, mismatched identity
  or failed persistence is reported with a nonzero exit. Inspect the emitted
  paths and reasons. Earlier durable captures and committed heads remain; do
  not delete evidence or treat leftover files as successful completion.

If saving the updated report itself fails, the command still reports known
receipt/head paths and the report error. No report path is claimed as durable
unless it was successfully saved. Use these exact references for recovery;
do not launch another collection with stale input to work around the error.

Input reports, primary claims, snapshots and their references are authenticated
before the first request. All sample and head selection is explicit; directory
globs and automatic latest-file selection are not used.

Official response bytes and metadata use the existing source storage layout:

- `data/experimental_daily/live_observations/mlb_official_result/`
- `audit/experimental_daily/live_receipts/`

Result captures have a dedicated query kind and unique observation identity,
separate from pregame captures. Settlement and report outputs retain the
existing `audit/prospective_ml_observation/` and
`reports/prospective_ml_observation/` formats.

## Corrections remain explicit

The bridge never changes an already settled official result automatically.
When an official correction is confirmed and its source receipt exists, use
the existing manual workflow with the exact prior **head**, not record:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B \
  -m mlb_backtest.prospective_ml_settlement settle \
  --root "$PWD" --snapshot "$SNAPSHOT_RELATIVE" \
  --result-receipt "$RESULT_RECEIPT_RELATIVE" --prior "$PRIOR_HEAD_RELATIVE"
```

Then build a report naming the original primary claims and the committed
revision head explicitly. A supplied nonterminal head from a manual workflow
also requires that existing explicit revision workflow, not automatic revision
by this bridge.

## Interpretation

The calibration report measures saved `p_home` against later official results
using Brier score, log loss and descriptive accuracy. Pending games remain
outside settled denominators. This is not a betting account ledger or a claim
that probabilities are calibrated, independent games are numerous enough, or
expected market value is realized profit. The seven-game starting population
does not establish model quality.

## First live verification — September 2, 2026

The approved single collection completed with exit 0: seven official responses
persisted, zero Final settlements, seven pending observations. The original
report path above was returned unchanged; no completed-outcome metric exists
yet. The original predictions and claims were preserved byte-for-byte.

Responses completed at 23:30:40–45 UTC (19:30:40–45 New York). Independent
read-only admission accepted all seven receipts and the report; all seven
fixed first settlement heads were absent. Three games were In Progress,
three Warmup and one Pre-Game at capture time.

Receipts below are under `audit/experimental_daily/live_receipts/`:

| Game | gamePk | Receipt basename | Settlement |
| --- | --- | --- | --- |
| NYM at TB | 822931 | `e143c0aeb7fb4fe995b183106f6dd2db.json` | Pending |
| SF at PIT | 823339 | `4a300c8aa91e44fcb4126290ac7aa982.json` | Pending |
| TOR at CLE | 824390 | `c22fa10897054910b003e48baf7ff99f.json` | Pending |
| DET at MIN | 823660 | `c37aeb7e7e7a40bdace92221cab19975.json` | Pending |
| MIA at KC | 824068 | `8526ef94aa2545c0ab69c569b048ab94.json` | Pending |
| MIL at CHC | 824634 | `7fd4d4b0f586480da4eca0ef16c92409.json` | Pending |
| CWS at HOU | 824147 | `5d0b13b3b1e2451381bbea61c75fefe9.json` | Pending |

Verification included 92 passing related tests, Ruff and independent
specification/code-quality reviews. No recurring automation, refit, commit or
push was performed.
