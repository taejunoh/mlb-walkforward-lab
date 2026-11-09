# Temporal calibration candidate — results and chronology

## Legacy status and scope

Review found that the historical prepare runner hashed its combined pin object,
including `locked/predictions.jsonl` and
`outcomes/evaluated/evaluated_predictions.jsonl`, before it wrote the candidate
prediction seal. Therefore the assertion in the old receipt that 2025 outcomes
were excluded from prepare is false. Neither sealed receipt may be amended.

All existing `prepare/`, `evaluate/`, `prepare-final/`, and `evaluate-final/`
artifacts are consequently classified
`LEGACY_RETROSPECTIVE_ONLY_PRESEAL_OUTCOME_READ`. They remain authoritative only
as a reproducible numeric comparison of the already-recorded inputs and
outputs, **not** as a temporally admitted pre-outcome experiment. No claim that
the predictions were sealed before outcome access may be made from these files.

This is a bounded retrospective candidate only. Its label is
`RETROSPECTIVE_CANDIDATE_ONLY_NOT_FOR_LIVE_USE`; it is not a promotion,
not a v5.2 Lane A/H22/H23/H24 gate, and must not be used live.

## Corrected technical replay

`prepare-corrected/` and `evaluate-corrected/` are the one controller-executed
technical replay after the pre-seal read defect was fixed. The corrected prepare
path has separate prepare/evaluation pins and does not open locked or evaluated
2025 sources. It records direct source hashes for `normalize.py`, `scoring.py`,
and `scoring_prep.py`; corrected evaluate verifies these and the runner hash
before it opens outcome paths.

The prepare receipt seal is
`9bc723332b80f36d8786f5258a51ebd247cf7b05879ca2d68a28e02c32ff5805`,
with recorded UTC `2026-09-02T20:42:54Z`. Its prediction seal is
`f5b5297a7fb0b988089e54839a40173b141c471938501df8e56deb5cb39d8039`.
The evaluation-report seal is
`db00e569023875ab9ed3638fde1c352dce18477faba088d3dab241280f9fa043`,
with recorded UTC `2026-09-02T20:43:16Z`. All three sidecars were read and
matched their files.

This corrected replay is technically sealed, but remains a known-2025,
retrospective reconstruction rather than an unseen or live validation. The old
pairs remain permanently legacy; this replay does not repair or relabel them.

## Result on the fixed population

| model | N | W-L | accuracy | mean pick probability | Brier | log loss |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2022-normalized + 2023-calibrated candidate | 1,340 | 717-623 | 0.535075 | 0.561059 | 0.246172 | 0.685362 |
| pinned v5.1 baseline | 1,340 | 718-622 | 0.535821 | 0.565661 | 0.246271 | 0.685553 |

The candidate's absolute Brier and log-loss differences are small
(-0.000098450 and -0.000190548 respectively). These are descriptive
retrospective values, not independently validated evidence or a profitability
claim.

The sealed candidate parameters are alpha `0.08502520804939874` and beta
`0.5799643656381512`. The full prediction SHA-256 is
`f5b5297a7fb0b988089e54839a40173b141c471938501df8e56deb5cb39d8039`.

The corrected replay exactly matches the legacy numeric-reference pair for
candidate and baseline N/W/L, accuracy, mean pick probability, Brier, and log
loss, as well as the full prediction SHA and alpha/beta. It therefore changes
the evidence boundary and recording—not the algorithm or observed numbers.

## Chronology and duplicate artifacts

The prior account that `prepare/` preceded all 2025 outcome reads is withdrawn:
the prepare pin verification itself read those sources. The first evaluation
attempt then stopped because the evaluator incorrectly required 1,972 candidate
keys to equal the 1,340 locked keys. The correction only required candidate
coverage of every locked key while retaining exact equality between locked and
evaluated keysets. It did not change the fit, candidate input, parameters,
predictions, comparator, ranking, or threshold.

After the numeric-reference `evaluate/` result existed, receipt caveat metadata
and lint/import annotations were added. `prepare-final/` and `evaluate-final/`
were then produced as preserved, post-outcome metadata-equivalent duplicates.
They have the same full prediction SHA, alpha/beta, and all reported metrics as
the original pair, but their receipt/report file hashes differ because the
receipt metadata and local code digest differ. Do not choose either pair for
chronology-sensitive analysis; see `artifact_manifest.json`.

No actual execution timestamps were captured in these artifacts. They are
therefore explicitly **unrecorded** here; no timestamps have been inferred
from filesystem metadata or backfilled. The corrected future-only runner emits
an actual UTC `execution_utc` field for both prepare and evaluate artifacts.

## Important limitations

- The comparison changes **both** 2022-only normalization and 2023-only
  calibration. It is not an isolated alpha/beta treatment effect.
- 2025 was already seen. The feature evidence uses reconstructed final
  lineups, not pristine live T-45 evidence.
- 2024 raw features and labels were excluded from normalization, calibration,
  and metrics. This is not an H24 validation.
- No Top-1 reranking, threshold selection, or post-hoc selection was done.
- Fixed 0.1 bins and monthly summaries in the evaluation report are
  descriptive; Wilson intervals do not establish independence or a guarantee.
- This does not support a live-use, independently validated, or profitable
  claim.
- Low, nonblocking code gap: evaluation validates `outcome.status == "Final"`
  but does not explicitly require `actual_winner` to be one of the locked
  home/away team values. The current pinned source constrains this risk; no
  code change is made in this bounded replay.

## Verification record

The focused suite ran 8 tests successfully, followed by Ruff with no findings:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v51_temporal_calibration_candidate -v
ruff check scripts/analysis/v51_temporal_calibration_candidate.py tests/test_v51_temporal_calibration_candidate.py
```

Controller records also report 59 affected tests and both specification and
quality reviews as PASS.
