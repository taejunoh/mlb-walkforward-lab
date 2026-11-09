# Single-book coverage

User-approved scope: change the minimum fresh sportsbook coverage from three to one. This follows the discussion separating price coverage from model calibration reliability. No additional model approval is implied.

- A single supported book with the required valid paired market quotes can satisfy coverage for ML or totals.
- Zero eligible books still cannot produce PLAY.
- Keep quote freshness (300 seconds), identity/time checks, duplicate detection, completeness, EV and residual checks unchanged.
- Keep the five supported-book capture-outcome accounting contract. Unavailable outcomes are not eligible quotes.
- Keep historical reports, evidence, frozen model cards, and existing user changes untouched. New reports bind the updated policy and digest; do not reclassify old reports.
- Update the policy constant, both evaluators, policy schema, focused behavior tests, and current user-facing market-play documentation only.
- Do not add historical-policy migration or retrain/promote any model in this task.

Acceptance: focused tests demonstrate one-book ML and totals coverage acceptance under otherwise valid inputs, zero-book rejection, and continued stale/identity/duplicate/residual rejection.
