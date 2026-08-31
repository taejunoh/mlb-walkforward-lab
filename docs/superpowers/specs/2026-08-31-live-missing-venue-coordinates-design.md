# Live missing venue coordinates: approved bounded design

The user approved connecting the existing model's missing-coordinate neutral-context and lower-quality handling to live input validation. This records that approved design; it does not authorize a new model, changed weights, new data sources, or unrelated infrastructure work.

## Problem and evidence

The previous timezone repair works. The next official response, venue 5340 (Estadio Alfredo Harp Helu), has a valid name/id and `America/Mexico_City` timezone but no `location.defaultCoordinates`. Its preserved raw SHA-256 is `7cc89857d769e57a75478c77192171523eda2f1f0dfb79929a36d37b60917585` (435 bytes).

Live venue admission currently requires coordinates for every historical venue. Downstream travel/schedule feature computation already supports missing coordinates with neutral context, explicit reason flags, and lower schedule quality. The repair connects these existing behaviors; it must not invent a coordinate or treat unknown travel as observed zero travel.

## Required behavior

1. Preserve valid venue id/name/timezone and immutable raw-source binding.
2. Represent a genuinely missing coordinate pair explicitly as null/None, never geographic zero or guessed values. Preserve the distinction from malformed/partial/non-finite/out-of-range supplied coordinates, which remain errors.
3. Carry missingness through venue authority creation, validation, schedule rows and serialization. Do not silently drop the historical venue or any game. Avoid NaN in canonical JSON or evidence hashes.
4. Use the existing downstream neutral travel/time-zone context, quality reduction and missing-coordinate reason flags for affected feature inputs. Test their propagation through real feature/prediction construction, not only the parser.
5. Complete-coordinate input behavior remains unchanged. Existing timezone and venue identity validation remains intact.
6. Do not alter the feature engine's formulas, scoring weights, trained parameters, or the frozen model card (SHA-256 `9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`). If the established neutral path cannot satisfy these requirements, report the conflict before widening the change.

Prediction diagnostics use additive `schedule_quality_flags` with exactly away/home sorted unique arrays of nonempty strings. New producer rows always include the field; prior V1 rows without it remain valid. The Markdown report distinguishes old unrecorded flags from an explicitly empty flag list. Neither schema version nor probability calculations change.

## Verification and execution

Use TDD with the saved actual response and targeted malformed/missing/complete-coordinate cases. Prove lower quality and reason flags are visible to prediction construction when missing coordinates are relevant, and that unaffected fixtures are unchanged. Run the directly related offline tests, not the multi-hour exact suite. Review before one live run with existing caches under the same isolated LFG root. Preserve all previous receipts and failed-attempt records; report real output or the next exact failure without claiming unavailable picks.

## Scope and alternatives

Acquiring a trustworthy alternate coordinate source remains possible but was not successful in the bounded official-source check. Guessing coordinates or excluding historical games changes inputs without evidence and is not allowed. The approved choice is the model's existing explicit missing-input behavior.

All work stays in `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix`. No Documents, custody/production, sudo, privileged ceremonies, dependency installation, push, merge, PR, or access to `/tmp/r4-readonly-tree-manifest.txt`. Only the controller's final live run may fetch the previously authorized official MLB/Savant model inputs.
