# All-day MLB prediction and outcome record

The all-day record includes day games, night games, scored games outside a
ranked Top 3, and games with missing forecasts or prices. It does not replace
the strict prospective calibration report.

## Read the evidence class first

- **Prospective primary:** an existing authenticated primary claim fixes the
  selected snapshot. The all-day record reads it; it never creates or changes it.
- **Historical pregame:** an existing eligible prediction was recorded before
  the saved start and is now linked to an official result. Ranked preliminary
  and eligible scored-not-ranked predictions both qualify. This is not a newly
  created prospective claim and is not proof of calibrated probabilities.
- **Diagnostic only:** a numeric value exists but lacks the evidence needed for
  an eligible pregame forecast. It remains visible without becoming a hit/loss.
- **Unscored:** no eligible forecast exists. The game and its result, prices and
  missing-data reasons still belong in the daily record.

Existing versions remain in prediction history. Selection is determined before
looking at results: an exact primary takes precedence, otherwise the latest
eligible pregame prediction is used with deterministic timestamp/hash tie-breaks.
Post-start versions cannot replace the pregame prediction.

## Prices and results

Each price keeps its book, side and observation time. A price captured after a
forecast is not described as known at forecast time. Market-only observations
are retained even when no forecast can be evaluated. Missing prices are null
with a reason, never zero or invented closing prices.

Independent raw sportsbook observations can also be supplied with their capture
outcomes and explicit active/superseded selector. These preserve both sides and
are not market PLAY decisions. Event matching requires the same normalized teams,
same UTC start date and a unique match within 60 seconds of the official start;
both timestamps and the difference remain visible. Pregame timing always uses
the official start. Unavailable sportsbooks remain recorded with their reasons.

Only a valid official Final outcome yields a win/loss for an eligible forecast.
Pending, exceptional and failed result captures remain explicit. Unscored games
may have a final score but do not become prediction losses. No stake, profit,
ROI, probability recalibration or automatic promotion is calculated here.

## Preservation and repeat runs

Inputs are explicit paths to existing traces, prospective reports and result
receipts. No directory scan silently chooses a newer forecast or a different
model version. The admitted slate defines the game population.

Each run writes a content-addressed JSON record and a readable Markdown table
under `reports/experimental_all_day_ml_ledger/<date>/`. A later run is a new
version of the same game records, not another independent sample. Original
prediction/source files and prospective claims/heads remain unchanged.

Failed capture or output writes must be reported with the known saved paths.
Preserve those files, inspect the reason and use explicit evidence references
for recovery. Do not delete earlier evidence or rerun a model to fill old gaps.

This is manual collection; no recurring automation is created.

## Verified September 2 collection

The single live invocation completed with exit 0: 15 games, 6 Final, 9 pending,
0 errors. It reused 7 old receipts and captured the other 8 once each. The
pending values preserve their individual receipt times; they are not a live
scoreboard. All 13 selected probabilities match their original trace authority.
The record retains 67 forecast/diagnostic versions and both ML sides for 11 games.

Read the [Korean all-game table](</Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix/reports/experimental_all_day_ml_ledger/2026-09-02/6558adf80f52eca1a4ab41c4ed20e12c276fb294a081c2b52347900219bed9e4.ko.md>) or
the [complete JSON ledger](</Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix/reports/experimental_all_day_ml_ledger/2026-09-02/6558adf80f52eca1a4ab41c4ed20e12c276fb294a081c2b52347900219bed9e4.json>).
The JSON filename hashes the complete canonical bytes; its `content_sha256`
hashes canonical bytes with that field removed. No ledger-output sidecar is
defined. Original forecast, primary claim/snapshot/report and market-source
hashes are unchanged. Final focused validation: 53 tests, Ruff, specification
review and code-quality review passed. No commit or push was performed.

## September 2 explicit collection inputs

Run from the LFG linked worktree shown in `--root`. This invocation reuses the
seven official snapshots captured at 19:30 ET and requests only the eight games
without supplied result receipts. Reused pending states are **as of their original
capture time**, not a claim of current live status. This is a manual invocation;
do not run a duplicate while a collection is active.

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.all_day_ml_ledger collect \
  --root '/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix' \
  --ml-trace 'reports/experimental_daily/2026-09-02/20260902T155839204827Z-ee2d8347f3243038.trace.json' \
  --ml-trace 'reports/experimental_daily/2026-09-02/20260902T17404356115Z-65acea021b6c4554.trace.json' \
  --ml-trace 'reports/experimental_daily/2026-09-02/20260902T18073555496Z-c39d0a281f899bbe.trace.json' \
  --ml-trace 'reports/experimental_daily/2026-09-02/20260902T220225593479Z-61d5695841e217dc.trace.json' \
  --prospective-report 'reports/prospective_ml_observation/8f30a9e94dd3c22ecf2a2d62e962014b5f26a5d862c76fd470360cb19a5d0eeb.json' \
  --market-supersession 'audit/market_observations/2026-09-02/generated/market-artifact-supersession.json' \
  --market-capture-outcome 'audit/experimental_multibook_market/capture_outcomes/e3d93d63c5df7f8e5731eada1e2d66804987204090e38d4c82c8d74796d25f78.json' \
  --market-capture-outcome 'audit/experimental_multibook_market/capture_outcomes/99513c7d082606294cb7732f619df4f5a3abdf687925512356bceb04e55af0e8.json' \
  --market-capture-outcome 'audit/experimental_multibook_market/capture_outcomes/e55cb6f06789664b13b4ae4189747e3e65dcc40326e43789db337161ac9d9bbf.json' \
  --market-capture-outcome 'audit/experimental_multibook_market/capture_outcomes/7610aeeb6238983a6ea976953ffbb4cf258c3b92a480bbebc83981e9f2da4134.json' \
  --market-capture-outcome 'audit/experimental_multibook_market/capture_outcomes/456c64843ba47e26f7f466272bb44d826924d0f708ba1ee0cdd7533a9311431b.json' \
  --result-receipt '822931=audit/experimental_daily/live_receipts/e143c0aeb7fb4fe995b183106f6dd2db.json' \
  --result-receipt '823339=audit/experimental_daily/live_receipts/4a300c8aa91e44fcb4126290ac7aa982.json' \
  --result-receipt '823660=audit/experimental_daily/live_receipts/c37aeb7e7e7a40bdace92221cab19975.json' \
  --result-receipt '824068=audit/experimental_daily/live_receipts/8526ef94aa2545c0ab69c569b048ab94.json' \
  --result-receipt '824147=audit/experimental_daily/live_receipts/5d0b13b3b1e2451381bbea61c75fefe9.json' \
  --result-receipt '824390=audit/experimental_daily/live_receipts/c22fa10897054910b003e48baf7ff99f.json' \
  --result-receipt '824634=audit/experimental_daily/live_receipts/7fd4d4b0f586480da4eca0ef16c92409.json'
```
