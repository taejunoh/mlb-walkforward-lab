# Prospective ML Daily Flow

Approved scope: a small coordinator for existing pregame, postgame and status operations. It prevents overlooked handoffs; it does not improve model calibration or authorize bets.

## Inputs and receipts

Every invocation uses an explicit absolute project root and exact input paths. Pregame requires an existing ML trace. Optional market trace, raw captures and unavailable records are supplied locally. No model execution, market fetching, latest-file discovery, scheduler or historical prediction reconstruction is introduced.

The coordinator persists immutable content-addressed receipts below `audit/prospective_ml_daily_flow/<target-date>/`. Receipts bind exact inputs and durable output references. Integrity and run association are checked before downstream writes or network requests. Existing underlying artifact validators remain authoritative. Receipt hashes provide integrity, not independent authenticity or authorization to modify model policies.

## Pregame

Persist existing baseline observations, then attach existing shadow calculations using returned primary-claim paths. Use real current time and existing strict pregame gates, including time crossing first pitch during processing. Retain all slate games, including exclusions, with reasons. A failure preserves earlier outputs and records partial progress; no cleanup or backdating.

Supplied raw captures pass existing admission. Raw capture without evaluated market trace is saved-but-unlinked. Missing and explicitly unavailable odds remain distinct from usable evaluated market evidence. A processed workflow is not necessarily complete evaluation evidence.

## Postgame

Require the exact pregame receipt and use only its original claims/attachments. Build the existing baseline report, invoke official result collection, then build a paired shadow report from exact returned settlement heads. Accept an explicit previous postgame receipt on resume, carry its heads, skip FINAL games and preserve existing stale-report rejection. Nonfinal games remain pending. Do not create predictions or shadow attachments after the game.

## Status

Read-only inspection of exact pregame and optional matching postgame receipt. Show per-game missing baseline, shadow, odds/evaluated market link, final result and shadow-pair stages. After-start missing pregame records are missed, not recoverable by scoring retrospectively. Do not mark unavailable prices as complete evidence.

## Verification and non-goals

Exercise real existing APIs with controlled fixtures, malformed receipts, partial failure, timing boundaries, pending/final result handling, replay and read-only status. No real result collection is needed for implementation tests. No changes to model weights, promotion thresholds, one-book policy, existing predictions, settlement history or existing CLI compatibility.
