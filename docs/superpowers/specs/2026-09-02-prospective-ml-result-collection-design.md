# Prospective ML official-result collection

## Approval and scope

The user approved this design in the conversation on 2026-09-02: one manual
command fetches official MLB results, preserves the response evidence, settles
completed games through the existing settlement module, and emits an updated
calibration report. This is not scheduling, refitting, probability calibration,
market ROI settlement, historical backfill, or H24 activation.

The existing isolated LFG worktree and all pre-existing dirty/untracked files
remain in place. Do not commit, push, merge, use privileged/custody/production
paths, or access the old Documents workspace or forbidden tree manifest.

## One-command workflow

`python -m mlb_backtest.prospective_ml_results collect --root ROOT --report REPORT`

1. Authenticate the explicitly supplied existing calibration report and all
   referenced primary claims, snapshots and settlement heads before network
   access. Never discover samples or choose a revision through directory globs.
2. For unresolved snapshots, derive the exact official date and gamePk from the
   authenticated snapshot. Request a single-game StatsAPI schedule response via
   the existing bounded HTTP transport; user-provided URLs are not accepted.
3. Store raw response, manifest, receipt and completion through existing source
   persistence helpers. Preserve actual capture times and response bytes. Existing
   receipt validation must accept the saved evidence before settlement.
4. Require exact game/date/home/away identity. `Final` is the only automatically
   settled state; non-final/exceptional states remain unresolved and are reported
   without a forced win/loss or fabricated score.
5. Use existing build/persist settlement and report functions, without changing
   their output schemas, math, correction semantics or failure behavior. The
   result evidence loader may additionally admit the existing helper's exact
   `mlb_official_result` RawArtifact manifest, with strict source/query/payload
   and receipt/completion bindings; retain the legacy manifest validation.
   Output the updated report path and per-game collection disposition.

## Repetition and failures

An explicitly supplied report's already terminal settlements are not fetched or
revised automatically. Official corrections retain the existing manual `--prior`
workflow. Reusing a stale pending report after a committed settlement must not
create a new revision or silently choose a different head: reject with guidance
to use the returned report/head. The fixed O_EXCL settlement head remains the
commit boundary under concurrency.

Network, malformed JSON, identity, redirect, integrity and write failures are
visible failures, never PASS or successful completion. Preserve any already
durable evidence and successful earlier heads and print their exact paths so
partial progress is recoverable. A repeated command is a new capture, not a
promise to recreate old network bytes. No automatic retry, polling, deletion,
repair, or re-scoring is introduced.

## Verification

Use synthetic primary fixtures and fake transport only at the HTTP boundary;
exercise real receipt storage, admission, settlement and report functions.
Real-helper round trips and tampered helper manifests must be tested: legacy
synthetic receipt fixtures alone do not prove producer/consumer compatibility.
Cover Final, non-final, identity mismatch, malformed/error response, tampered
input report, existing heads, stale input, and persistence failure. A final
read-only source/state comparison must show prior model and prediction bytes
unchanged. After focused tests and two independent reviews, perform at most one
live invocation against the existing seven-game report. Any non-final game stays
pending. Its report remains a small prospective sample, not proof of profitable
or calibrated predictions.
