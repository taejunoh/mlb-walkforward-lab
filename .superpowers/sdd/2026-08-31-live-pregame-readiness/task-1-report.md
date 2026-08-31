# Task 1 report

## Status

Implemented missing-evidence versus true-conflict diagnostics through the real
parser, service, and fixture E2E paths.

## TDD evidence

RED focused run (before production changes):

```text
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest \
  tests.test_live_sources.LiveSourceTests.test_missing_pitcher_list_is_missing_evidence_not_starter_conflict \
  tests.test_live_sources.LiveSourceTests.test_missing_schedule_probable_is_retained_without_consistency \
  tests.test_live_sources.LiveSourceTests.test_missing_one_side_and_known_opposite_mismatch_retain_both_reasons \
  tests.test_live_top3_service.LiveTop3ServiceTests.test_missing_participant_pitchers_are_unscored_with_missing_evidence \
  tests.test_live_top3_service.LiveTop3ServiceTests.test_missing_lineup_evidence_is_unscored_without_invalid_fallback
```

Observed expected failures: 4 failures (false `STARTER_CONFLICT`, collapsed
missing evidence, and `INVALID_HOME_LINEUP` fallback); the missing-schedule
case already passed before the change.

GREEN focused run: the same command completed with `Ran 5 tests ... OK`.

The successive-observation fixture E2E then passed with `Ran 1 test ... OK`.
It exercises missing participants → complete matching participants and
nine-player lineups → changed final probable pitchers using the real pipeline
and scorer, and verifies distinct live observation IDs plus first-report
immutability.

## Full affected-module verification

```text
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest \
  tests.test_live_sources tests.test_live_top3_service tests.test_daily_top3_e2e
```

`Ran 101 tests in 43.873s` — `OK`.

`git diff --check` passed.

## Changes

- Compare probable pitchers only across positive integer known pairs; missing
  observations retain parser flags and cannot create a false conflict or stale
  consistency flag.
- Candidate failure diagnostics preserve missing participant and lineup
  evidence while retaining true fully-known starter conflicts.
- Added parser/service regression coverage and a three-observation fixture E2E.
- Updated only missing-reason expectations and the resulting fixture golden
  content/Markdown digests. The digest change is caused by the intentional
  `INVALID_HOME_LINEUP` → `MISSING_HOME_LINEUP` report correction; preliminary
  probabilities remain unchanged.

## Commit

`Clarify missing pregame participant diagnostics` (final hash reported to the
parent agent)

## Concerns

The approved behavior changes the existing partial-fixture report reason, so
its content and Markdown golden digests necessarily changed. No generated
cache, audit, data, or report evidence was removed.
