# Live Missing Venue Coordinates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Carry genuine coordinate absence into the existing neutral travel context, reduced quality, and visible prediction flags without changing model weights.

**Architecture:** Preserve the five-key venue reference, using a paired `None` representation only for absent coordinates. Retain all strict checks for supplied coordinates and timezone. Export existing schedule flags in ranked predictions instead of hiding the reason for lower quality.

**Tech Stack:** Existing Python 3.11, unittest, current immutable raw/receipt cache, existing v5.1 model.

**Spec:** `docs/superpowers/specs/2026-08-31-live-missing-venue-coordinates-design.md`.

## Global Constraints

- Work only in `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix` on its existing branch.
- Preserve canonical original, previous receipts, failed-run evidence, all unrelated changes, model card and feature/scoring formulas.
- An absent or JSON-null `location.defaultCoordinates` becomes `(None, None)`. A present coordinate object must contain both finite numeric values within latitude/longitude ranges. Partial/null members, bool/string/non-numeric, NaN/Inf, and out-of-range values remain errors.
- Keep valid venue id/name/city/timezone checks. Do not guess coordinates, inject geographic zeros, exclude games, or relax unrelated validation.
- Existing feature engine computes neutral context and reduced quality. Export existing flags; do not create new scoring heuristics.
- Implementation and tests are offline. No live CLI, network, install, full exact suite, H6, Task12/14, Documents, custody/production, sudo, push, merge, PR or forbidden manifest access.
- Controller owns one live execution after review. No retry during implementation.

### Task 1: Connect paired missing coordinates through the live contract

**Ownership/files:**
- Modify `mlb_backtest/live_history.py`: venue parser only.
- Modify `mlb_backtest/live_features.py`: venue reference validation/construction only.
- Modify `mlb_backtest/live_top3_contracts.py`: additive ranked schedule quality flags validation only.
- Modify `mlb_backtest/live_top3_service.py`: copy actual raw schedule flags into prediction output.
- Modify `schemas/experimental_daily_top3.schema.json`: optional closed diagnostic property, legacy V1 compatibility.
- Modify `mlb_backtest/daily_top3.py`: render the diagnostic flags without conflating legacy omission with an empty list.
- Tests: `tests/test_live_history.py`, `tests/test_live_features.py`, `tests/test_live_top3_service.py`, `tests/test_live_top3_contracts.py`, `tests/test_daily_top3.py`, directly affected `tests/test_daily_top3_e2e.py` if needed.
- Fixture: `tests/fixtures/venue_5340_missing_coordinates.json`, exact captured raw bytes.

- [ ] **Step 1: Real response RED and feature-path RED.**

Use saved raw `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix/data/experimental_daily/history_cache/mlb_live_history_venue/7cc89857d769e57a75478c77192171523eda2f1f0dfb79929a36d37b60917585.raw` (435 bytes, SHA matching basename). Independently assert the parsed pair and timezone:

```python
parsed = live_history._parse_venue(raw, 5340)
assert parsed["venue_latitude"] is None
assert parsed["venue_longitude"] is None
assert parsed["time_zone"] == "America/Mexico_City"
```

Adapt the exact call signature from existing parser tests. Add separate real feature-path tests passing paired None through venue authority, historical/target rows and prepared features. Assert both existing flags (`travel_neutral_missing_venue_coordinates`, `time_zones_neutral_missing_venue_coordinates`) and lower schedule quality than the corresponding complete-coordinate fixture. Assert no game/reference silently disappears and complete-coordinate parity remains. Capture expected failures before production edits.

Include mixed complete-coordinate and missing-coordinate rows in a real history DataFrame, not only an all-None fixture: pandas may represent missing numeric cells as NaN. Any normalization at the internal DataFrame boundary must match the paired missing authority; it must not accept upstream explicit NaN coordinates or leak NaN into canonical JSON.

- [ ] **Step 2: Minimal parser and authority changes.**

Parser coordinate decision:

```python
coordinates = location.get("defaultCoordinates")
if coordinates is None:
    latitude = longitude = None
else:
    # Require a coordinate mapping with both supplied numeric members.
    latitude = coordinates["latitude"]
    longitude = coordinates["longitude"]
    if (isinstance(latitude, bool) or isinstance(longitude, bool)
            or not isinstance(latitude, (int, float))
            or not isinstance(longitude, (int, float))):
        raise ValueError("venue reference coordinates differs")
    latitude, longitude = float(latitude), float(longitude)
    if not (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("venue reference coordinates differs")
```

Use existing module imports/helpers/errors where they already provide this behavior. Preserve previous id/name/timezone checks and immutable source binding. Authority's five-key record accepts exactly a valid coordinate pair or both None. Avoid passing None to `float`; keep paired None in by-name mappings and schedule rows. A partial None pair remains `INVALID_VENUE_REFERENCE`. Canonical serialization must remain valid JSON null, not NaN.

- [ ] **Step 3: Visible flags without changing scoring.**

New producer rows include:

```python
"schedule_quality_flags": {
    "away": sorted(set(raw["away"]["schedule"]["flags"])),
    "home": sorted(set(raw["home"]["schedule"]["flags"])),
}
```

Use actual existing raw structure (`raw_components` if named differently). If no flags, emit empty arrays. Keep provenance `reason_codes` separate. Make this additive for archived V1 records: old ranked rows may omit the field; when present require exactly away/home keys and string arrays with no duplicates and deterministic sorting. New service outputs always include it. Preserve all previous required fields and rejection of unrelated unknown keys.

Test actual producer output preservation of side-specific flags and successful payload validation; test malformed field types/sides/entries rejected. Prove existing reports without the additive field still validate. Existing feature engine and model card must have no diff.

Update the closed JSON Schema with the optional property (not required), exactly away/home required inside it, arrays of nonempty strings and uniqueItems. Python validation also enforces sorted order. Add a Markdown quality-flags column using the actual side-specific flags; a legacy absent field renders `not recorded`, new empty sides render `none`. Test validation/rendering for both old and new rows. This exposes existing diagnostics without a schema-version bump or changed probabilities.

- [ ] **Step 4: Focused GREEN, related suite, self-review and commit.**

Run new tests first. Then run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest tests.test_live_history tests.test_live_features tests.test_live_top3_service tests.test_live_top3_contracts tests.test_daily_top3
git diff --check
shasum -a 256 audit/v5_1_bt/model_card.json
```

If a directly affected contract module exists, include its focused tests. Expected: all related tests pass; model card hash stays `9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`. Baseline CLI tests intentionally print negative-case errors; distinguish those from unexpected warnings/failures. Do not run full exact suites. Commit only owned code/test/fixture files. Report exact RED/GREEN commands/results, commit, changes and concerns to the controller. Do not spawn agents.

## Controller execution and verification

After task review and whole-branch review, run the repaired CLI once with existing isolated cache and target 2026-08-31. Preserve old raw/receipts; no duplicate running process. Validate generated report status, source provenance, model-card hash, and flags, or preserve and diagnose the exact failure. No probability is claimed if inference did not produce it; experimental ML output is not O/U or verified betting value.
