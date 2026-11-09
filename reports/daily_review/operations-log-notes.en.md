# English Notes for Archived Operations Logs

This companion translates the Korean text fields in the archived operations-check and operations-status JSON files. The linked files retain the original wording, timestamps, status codes, and evidence.

## Automation label

MLB daily evaluation, settlement, and error review

| Source | Field |
|---|---|
| [2026-09-29/operations-status.json](2026-09-29/operations-status.json) | `automation.name` |
| [2026-09-30/operations-status.json](2026-09-30/operations-status.json) | `automation.name` |

## September 29 coverage

No pregame forecast was saved for any of the four September 29 games. The workflow could not evaluate postseason type F. The accuracy denominator is zero, so accuracy is undefined. No forecasts or explanations for losses were generated after observing results.

| Source | Field |
|---|---|
| [2026-09-29/operations-check-20260930T020446Z.json](2026-09-29/operations-check-20260930T020446Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-29/operations-check-20260930T023446Z.json](2026-09-29/operations-check-20260930T023446Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-29/operations-check-20260930T030446Z.json](2026-09-29/operations-check-20260930T030446Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-29/operations-check-20260930T033446Z.json](2026-09-29/operations-check-20260930T033446Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-29/operations-status.json](2026-09-29/operations-status.json) | `current_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T040446Z.json](2026-09-30/operations-check-20260930T040446Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T043446Z.json](2026-09-30/operations-check-20260930T043446Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T050446Z.json](2026-09-30/operations-check-20260930T050446Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T053446Z.json](2026-09-30/operations-check-20260930T053446Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T060447Z.json](2026-09-30/operations-check-20260930T060447Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T063447Z.json](2026-09-30/operations-check-20260930T063447Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T070447Z.json](2026-09-30/operations-check-20260930T070447Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T073447Z.json](2026-09-30/operations-check-20260930T073447Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T080447Z.json](2026-09-30/operations-check-20260930T080447Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T083447Z.json](2026-09-30/operations-check-20260930T083447Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T090447Z.json](2026-09-30/operations-check-20260930T090447Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T093447Z.json](2026-09-30/operations-check-20260930T093447Z.json) | `previous_day_prediction_coverage.note_ko` |
| [2026-09-30/operations-check-20260930T100447Z.json](2026-09-30/operations-check-20260930T100447Z.json) | `previous_day_prediction_coverage.note_ko` |

## September 30 coverage before cutoff

At this check, the September 30 games were still pregame and no missed cutoffs were confirmed. Evaluation was blocked because postseason type F was unsupported. The four September 29 gaps belong to the prior slate.

| Source | Field |
|---|---|
| [2026-09-30/operations-check-20260930T040446Z.json](2026-09-30/operations-check-20260930T040446Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T043446Z.json](2026-09-30/operations-check-20260930T043446Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T050446Z.json](2026-09-30/operations-check-20260930T050446Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T053446Z.json](2026-09-30/operations-check-20260930T053446Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T060447Z.json](2026-09-30/operations-check-20260930T060447Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T063447Z.json](2026-09-30/operations-check-20260930T063447Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T070447Z.json](2026-09-30/operations-check-20260930T070447Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T073447Z.json](2026-09-30/operations-check-20260930T073447Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T080447Z.json](2026-09-30/operations-check-20260930T080447Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T083447Z.json](2026-09-30/operations-check-20260930T083447Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T090447Z.json](2026-09-30/operations-check-20260930T090447Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T093447Z.json](2026-09-30/operations-check-20260930T093447Z.json) | `prediction_coverage_summary.note_ko` |
| [2026-09-30/operations-check-20260930T100447Z.json](2026-09-30/operations-check-20260930T100447Z.json) | `prediction_coverage_summary.note_ko` |

## PHI at ATL missed cutoff

The scheduled pregame cutoff for PHI at ATL (849841) passed without a saved prediction. This is a coverage gap. Model pass/fail and pick accuracy do not apply. Neither the result nor the actual first pitch time was checked.

| Source | Field |
|---|---|
| [2026-09-30/operations-status.json](2026-09-30/operations-status.json) | `current_day_prediction_coverage.note_ko` |

## Authorization question

After live-data validation, should automatic forecasting, recording, and settlement be enabled with betting and totals forecasts outside the scope?

| Source | Field |
|---|---|
| [2026-09-30/operations-status.json](2026-09-30/operations-status.json) | `research_activation.authorization_evidence.preceding_question` |

## Recorded reply

Continue.

| Source | Field |
|---|---|
| [2026-09-30/operations-status.json](2026-09-30/operations-status.json) | `research_activation.authorization_evidence.user_reply` |
