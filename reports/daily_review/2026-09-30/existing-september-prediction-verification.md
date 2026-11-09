# Existing September prediction verification

Recorded: 2026-09-30T15:31:00Z.

Result: **33 / 33 legacy report–trace pairs passed; 0 errors.** Frozen-card replay covered **121 stored rows, 89 unique (target_date, game_pk) pairs**. Nine empty traces passed as valid unscored reports; they were not treated as failures.

All 33 trace and 33 report external sidecars were explicitly checked and were **NOT_PRESENT_LEGACY_OPTIONAL**, not digest mismatches. The legacy producer writes canonical artifacts with embedded content digests and does not require these sidecars. The first strict-sidecar probe was therefore an inapplicable stronger-format check, not evidence of corruption. No seals were created.

The actual frozen card path came from each bound report's `command_inputs.model_card`: `audit/v5_1_bt/model_card.json`. File SHA-256:
`9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`.

## Method

Executed with `PYTHONDONTWRITEBYTECODE=1 python3 -B` in the authorized worktree. Socket creation was patched to raise, and validation made no writes.

For every exact trace path listed in the companion JSON:

1. Call `market_play_authorities.load_ml_authority(root, trace_relative)`.
2. Read trace, bound primary report and actual report-selected card through `secure_fs.read_regular_snapshot`; require canonical trace/report bytes; validate external sidecars if present, explicitly recording absence.
3. Parse the actual card with `load_frozen_live_card_bytes` and require its hash to match the authority.
4. Call `validate_trace_payload(trace, expected_primary_report_content_sha256=report["content_sha256"], expected_model_card_sha256=card.file_sha256)`.
5. Call `replay_stored_row(row, card)` for every trace row.
6. Rebind every snapshot and call `authority.rebind()`.

## Interpretation limits

These are authenticated **existing preliminary forecasts**, not newly created prospective predictions. Their `PRELIMINARY_RESEARCH_ONLY` status is unchanged. No candidate, primary claim, settlement or calibration record was created.

The check covers the existing legacy source-binding contract plus frozen-card replay of stored features, not a fresh reconstruction of all history inputs. It does not establish complete September coverage or validate the separate Sep10 blind reconstruction. All original reports, traces and model bytes were preserved.

The companion JSON records all 33 exact report/trace paths, individual row counts, errors and sidecar disposition.
