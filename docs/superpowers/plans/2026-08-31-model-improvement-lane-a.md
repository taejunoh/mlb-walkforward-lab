# Model Improvement Lane A Implementation Plan

This plan records Lane A's consumer contract for the staged model-improvement
workflow. The shared order is **Task8A Common OOF FeatureSeal admission +
baseline label-free seals/loaders → Task6A baseline replay receipt after its
matching baseline seal → Task8B nonbaseline label-free seals/loaders after
same-fold replay receipt → Task6B SourceInventory → GameEvidence outcomes,
reports, and final pair**.

Lane A never creates an outcome-bearing prediction artifact. Its prediction
producer and loaders must use the canonical receipt-bound APIs:

```python
seal_oof_predictions(spec, source_gate, feature_seal, baseline_replay, ledger_id, rows)
load_prediction_seal(spec, source_gate, feature_seal, baseline_replay, path, expected_sha256)
load_prediction_rows(spec, source_gate, feature_seal, baseline_replay, seal)
```

Task8A publishes the baseline seal over the Common OOF `FeatureSeal`; Task6A
then publishes the matching `BaselineReplayReceipt`. Lane A's H22/H23
candidate seals are Task8B outputs and must bind the same-fold replay receipt,
the candidate scoring card, source gate, feature seal, and RunSpec prediction
contract. A candidate cannot be evaluated before those immutable artifacts are
securely reloaded.

Task6B alone derives a typed `OutcomeReceipt` from the authenticated
`SourceInventory → GameEvidence` chain. Lane A calls exactly:

The receipt boundary is `seal_outcome_receipt`, `load_outcome_receipt`, and
`load_outcome_rows`; no raw outcome mapping is accepted.

```python
pair = evaluate_predictions(spec, candidate, baseline, outcome_receipt)
```

Lane A obtains the complete pair only through
`load_evaluation_pair(spec, path, expected_sha256)`; the returned pair is the
sole authority for its named candidate and baseline reports.

The downstream frozen-card boundary securely loads the resulting
`EvaluationPair` and consumes its named candidate and baseline reports. It
must check `outcome_receipt_path` and `outcome_receipt_sha256` on the loaded
pair and reports; `outcomes_sha256` is an observation-content digest and is never sufficient outcome provenance. Lane A does not accept a raw outcome
mapping, an ad hoc label collection, or a local evaluator.

H22 remains development-only. H23 may emit `H23_FROZEN` only after strict
candidate-vs-baseline metrics from the receipt-bound pair pass on the identical
population. The frozen card binds the securely loaded pair, both report
descriptors, the candidate prediction seal, scoring card, baseline replay, and
all source/feature receipts. Any pair, report, receipt, or seal path/digest
mismatch fails closed before card publication.
