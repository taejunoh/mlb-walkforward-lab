# Post-start reconstructed research evaluation

This command produces an isolated, post-start reconstructed research report. It is not a prospective observation, a calibration input, a historical official play, or a betting recommendation. The lineup source is reconstructed after the fact and its pre-start availability is not timestamp-proven.

The evaluator only reads four explicit local snapshots: a sealed D-1 feature-history bundle, a constrained reconstructed participant projection and its closed lineup source, a frozen model card, and a D-1 career-BF mapping. It never reads results, settlements, markets, odds, or live schedule state. A source artifact containing a `score`, `winner`, or `result` field is rejected.

Run it with:

```bash
python -m mlb_backtest.poststart_research_evaluation evaluate \
  --root ROOT --history-bundle RELATIVE_PATH \
  --participant-projection RELATIVE_PATH --model-card RELATIVE_PATH \
  --career-bf RELATIVE_PATH
```

Artifacts are append-only JSON, Markdown, and trace files under `reports/experimental_poststart_research/<target_date>/`. They are labelled `POSTSTART_RECONSTRUCTED_RESEARCH`, `NOT_PROSPECTIVE`, and `NOT_BET_VALIDATED`; they do not alter prospective reports, settlement records, calibration cohorts, or any ledger.
