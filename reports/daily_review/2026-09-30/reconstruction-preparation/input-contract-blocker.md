# Post-start reconstruction preparation blocker

**Status: `BLOCKED_PREPARATION_ONLY` / `UNEXECUTED_NOT_PILOT_READY`.** The preparation worker made no live acquisition and the post-start evaluator was not invoked. The parent made one separate score-free masked boxscore structural probe; it was never supplied to the evaluator or admitted as a pregame snapshot.

The Sep 25 pilot identity was verified without reading its outcome: R game `824703`, CHC away team ID `112`, BOS home team ID `111`, scheduled for `2026-09-25T17:05:00Z`. Its required D−1 history date is Sep 24, but that pilot history was not acquired. A separate authenticated report targeting Sep24, with history as of Sep23, demonstrates **91 original history bindings**: one `HISTORY_ERA`, one `HISTORY_SCHEDULE`, 55 `HISTORY_STATCAST`, and 34 `HISTORY_VENUE`. This is evidence of the collector's multi-binding contract, not a claim that the unacquired pilot bundle has exactly91 bindings.

The evaluator's closed history loader rejects any binding list other than exactly four entries, then requires exactly one of each of those four roles ([`poststart_research_evaluation.py`](../../../../mlb_backtest/poststart_research_evaluation.py:309)). Meanwhile, [`LiveHistoryBundle`](../../../../mlb_backtest/live_top3_contracts.py:188) validates the required roles and each binding's D−1 `as_of_date` without reducing the source-binding closure to four, and [`build_live_history`](../../../../mlb_backtest/live_history.py:2364) returns the concatenation of schedule, Statcast, venue, and ERA bindings unchanged.

This is a provenance contract mismatch. Selecting one binding per role, merging 55 Statcast and 34 venue records, or fabricating a four-entry list would lose authenticated source lineage. Weakening the evaluator or changing application code is outside this assignment. Therefore stop before evaluator-input creation and do not run the evaluator until a separately authorized resolution preserves all original provenance.

The preparation scaffold and mapping note remain unexecuted and explicitly marked not pilot-ready. Their participant parser is incomplete; do not invoke either file.

The preparation worker made **zero** target-game acquisitions; the parent made the one separate masked probe described above. There were **zero** evaluator runs, new forecasts, or retrospective model reports. No target outcomes or scores were read and no application source/evaluator code was changed.

Evidence references:

- Authenticated existing collector example (Sep24 target / Sep23 history): [2026-09-24 report](../../../experimental_daily/2026-09-24/20260924T203303766913Z-095c7301fcd8a3f4.json)
- Existing September prediction verification: [verification artifact](../existing-september-prediction-verification.json)
- Existing September analysis coverage: [coverage artifact](../september-analysis-coverage-20260930.json)
- Unexecuted scaffold: [prepare_poststart_research_inputs.py](prepare_poststart_research_inputs.py)
- Input mapping: [input-mapping.md](input-mapping.md)
