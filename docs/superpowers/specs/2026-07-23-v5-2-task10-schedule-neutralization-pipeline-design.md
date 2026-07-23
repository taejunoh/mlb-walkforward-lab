# v5.2 Task 10 Schedule Neutralization Pipeline Design

**Date:** 2026-07-23
**Status:** Approved design direction
**Authority:** The user approved the full end-to-end expansion on 2026-07-23.

## Context

The superseding `historical_walkforward_500_superseding_565534` run must
neutralize exactly three schedule metrics whose historical inputs depend on
duplicate MLB schedule objects. The original Task 10 plan named only:

- `mlb_backtest/v52_sources/history.py`
- `mlb_backtest/feature_engine.py`
- `tests/test_v52_source_history.py`
- `tests/test_feature_engine.py`

That scope cannot produce a valid integrated artifact. The verified history
payload and normalized snapshot schemas are closed, the verifier enforces
closed key sets, `prepare.py` drops history metadata before constructing
`FeatureHistory`, and the current history reducer cannot see the raw schedule
object SHA, normalized anchor SHA, numeric team ID, or authenticated
disposition authority.

Task 10 therefore expands only as far as required to carry one closed,
authenticated neutralization channel from the schedule boundary to the feature
consumer. It does not change model weights, scores, calibration, feature
windows, or ordinary schedule behavior.

## Considered approaches

### A. Full authenticated pipeline

Carry raw-object and normalized-anchor lineage through the superseding schedule
snapshot, emit a closed neutralization projection, validate it during replay,
transport it through `prepare.py`, and consume it through an immutable exact-key
index.

This is the selected approach because every production boundary can reproduce
and authenticate the same three rows.

### B. Split producer and consumer into separate tasks

First publish the history projection, then add the prepare/feature transport.
This produces smaller commits but leaves an intermediate state in which a
schema-valid field is not consumed. The implementation plan may use staged
commits, but the task is not complete until both halves are integrated.

### C. Four-file unit-only implementation

Teach the reducer and feature engine about the allowlist without extending
schemas, replay validation, or transport. This was rejected because the
normalized snapshot would reject the new field and production preparation
would discard it.

## Frozen neutralization authority

The only approved identities are:

```python
EXPECTED_NEUTRALIZATIONS = (
    (
        "35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab",
        "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29",
        566156,
        "2019-09-06",
        116,
    ),
    (
        "35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab",
        "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29",
        566156,
        "2019-09-06",
        133,
    ),
    (
        "92900ec47f924a1a68d533d63ae068faabd42262b808461a543974b61f75d5ef",
        "5224b908bc7a340ac057bb9f56efa1e9ebdf3ac64972191611e0e0b802410c96",
        565338,
        "2019-05-20",
        133,
    ),
)
```

Each tuple is:

```text
(raw_game_object_sha256, normalized_anchor_sha256,
 game_pk, official_date, team_id)
```

Canonical ordering is raw SHA, game PK, then team ID. Full 64-character hashes
are mandatory. Prefix matching is forbidden.

The following complete adjacent team identities are known but unapproved:

```python
KNOWN_UNAPPROVED_NEIGHBORS = (
    (
        "11afba7dcd8ec0de2b78f8773e2c49c8f23a7d104e9b1a2b58d9a9f5893ff019",
        "131e5e173effb6b610c00624a3544bb36c0b42968e69c3d1c2e740a8e35248b2",
        565535,
        "2019-05-21",
        116,
    ),
    (
        "11afba7dcd8ec0de2b78f8773e2c49c8f23a7d104e9b1a2b58d9a9f5893ff019",
        "131e5e173effb6b610c00624a3544bb36c0b42968e69c3d1c2e740a8e35248b2",
        565535,
        "2019-05-21",
        146,
    ),
    (
        "db8427682b066341a9f204dd2832ff418d93946b1ff12e07b147e7e60398ca6d",
        "c5b3a273d946f93b500430363f679e351a1b03e739576a1f3830bafbc829769f",
        566157,
        "2019-09-07",
        116,
    ),
    (
        "db8427682b066341a9f204dd2832ff418d93946b1ff12e07b147e7e60398ca6d",
        "c5b3a273d946f93b500430363f679e351a1b03e739576a1f3830bafbc829769f",
        566157,
        "2019-09-07",
        133,
    ),
)
```

These neighbor literals prove that knowing an input hash does not authorize a
neutralization. They use the same five-field identity granularity as the
allowlist and every listed neighbor team must remain ordinary.

The canonical record sort key is exactly:

```text
(raw_game_object_sha256, game_pk, team_id)
```

`normalized_anchor_sha256` and `official_date` are validated identity fields
that are functionally fixed by the selected raw object and game PK; they are
not additional sort keys.

## Policy authority

`EXPECTED_NEUTRALIZATIONS` is a Git-bound execution policy, not a fact inferred
only from the `565534` disposition capability. The disposition capability
admits the superseding run and authenticates its selected old schedule
projection. The committed policy allowlist separately authorizes exactly the
three downstream dependency neutralizations.

The verifier requires both authorities:

1. the exact authenticated superseding disposition binding; and
2. the exact policy constant shipped in bound execution code and covered by
   this approved design plus hermetic fixtures.

There is no runtime configuration or prefix-based extension point. Changing
any tuple requires a new approved design amendment, regenerated compact
fixtures, exact closure review of the execution-code set, and new
characterization tests. A disposition binding by itself can never authorize an
additional schedule neutralization.

## Output record

Every approved history record has exactly:

```python
{
    "game_pk": 566156,
    "official_date": "2019-09-06",
    "team_id": 116,
    "target_raw_game_object_sha256":
        "35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab",
    "target_normalized_anchor_sha256":
        "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29",
    "value": 50.0,
    "sample_size": 0,
    "quality": 0.0,
    "reason_codes": [
        "schedule_neutral_duplicate_gamepk_dependency"
    ],
}
```

The top-level history payload stores these records in
`schedule_neutralizations`, separate from `games`.

They must not be inserted into ordinary schedule history rows. Game `566156`
has two approved team records, while ordinary `ScheduleHistory` requires
globally unique `game_pk` rows.

## Authority and data flow

### 1. Schedule provenance

For the superseding run only, the verified schedule/game-evidence path retains
two additional per-game values:

- `raw_game_object_sha256`
- `normalized_anchor_sha256`

`raw_game_object_sha256` is SHA-256 of `canonical_json_bytes(raw_game)`, where
`raw_game` is the individual unchanged game mapping selected from
`payload["dates"][*]["games"][*]` by the authenticated schedule occurrence
projection. The canonical bytes do not include a trailing newline.

`normalized_anchor_sha256` is SHA-256 of `canonical_json_bytes(anchor)`, where
`anchor` is the complete closed superseding anchor mapping with exactly:

```text
alternate_site, away_score, away_team_id, completed_innings,
doubleheader_order, game_pk, home_score, home_team_id, neutral_site,
official_date, played, raw_game_object_sha256, scheduled_innings,
scheduled_start_utc, season, season_start_date, terminal_status, venue_id,
winner_team_id
```

The hash is computed before adding `normalized_anchor_sha256` to any downstream
record, preventing a circular digest.

The raw hash comes from the authenticated `ScheduleOccurrenceProjection` and
must equal the `raw_game_object_sha256` already bound by the selected schedule
anchor. The normalized hash is computed in the schedule/game-evidence bridge
over the entire closed anchor mapping returned by
`v52_schedule_inventory._derived_projection_anchors()`, using the project's
canonical JSON serializer and before adding either provenance hash to the
downstream game record. It is not the schedule query-sidecar SHA and must never
be substituted with that value.

The values originate at the authenticated schedule-inventory boundary. They
must not be reconstructed from `source_game_sha256`, which is a different
lineage hash.

Old-run schedule and game-evidence payloads continue to omit both fields.

Bootstrap and partial game-inventory items remain valid for acquisition
progress. A partial item such as `{game_pk, season}` does not carry the two
provenance hashes and is never eligible for history generation.

The provenance fields are required only on complete `VERIFIED` superseding
game projections admitted to the terminal season-source snapshot. History
generation requires every approved target to be complete and verified. A
partial, pending, quarantined, or ineligible target blocks the source gate; it
does not fall back to an ordinary schedule metric.

### 2. History projection

`recompute_prior_history_sidecar()` remains a deterministic, network-free
reducer. Its caller supplies:

- the verified game-evidence payload;
- the trusted run identity;
- the already-authenticated duplicate disposition binding.

The reducer does not reopen repository evidence. The caller is responsible for
authenticating the disposition capability before invoking it.

For the old run, the reducer produces the existing five-key payload exactly and
omits `schedule_neutralizations`.

For the superseding run, the reducer:

1. requires the exact authenticated disposition query ID, sidecar path, and
   sidecar SHA;
2. finds every approved game exactly once;
3. verifies the full raw-object and normalized-anchor hashes;
4. verifies the official date and numeric team participation;
5. emits exactly the three frozen records in canonical order;
6. proves that known adjacent games remain unapproved;
7. rejects missing, duplicated, altered, truncated, or additional targets.

### 3. Schema and replay validation

The season-source and normalized-snapshot schemas admit two closed shapes with
`oneOf`.

- Old branch: the neutralization field and added provenance fields are absent.
- Superseding branch: the provenance fields and exact three-record
  neutralization array are required.

The standalone payload schema cannot decide which run owns a payload because
the history payload itself has no run ID. Runtime validation is therefore
authoritative: `v52_verify_sources.py` selects the permitted shape from the
authenticated run/snapshot envelope, rejects the other shape, recomputes the
history reducer with the authenticated disposition capability, and requires
byte-equivalent canonical payloads.

Historical, fixture, and every other non-`SourceRunSpec` authority serialize
the existing five-key history payload exactly. They must omit
`schedule_neutralizations`; an empty `schedule_neutralizations: []` is also
rejected. Only the exact superseding `SourceRunSpec` may use the new shape.

Unknown keys, optional neutralization rows, permissive prefixes, or a mixed
old/new payload fail closed.

### 4. Preparation transport

`ScheduleHistoryBuild` gains a separate immutable
`schedule_neutralizations` field. `build_feature_history()` validates and
passes it into `ScheduleHistory`; it does not hide it inside the generic
`provenance` mapping.

Each transported record is checked for:

- exact key set and types;
- canonical ordering and uniqueness;
- positive `game_pk` and `team_id`;
- canonical date;
- full lowercase hashes;
- exact metric values and reason code;
- membership in `EXPECTED_NEUTRALIZATIONS`.

Old callers default to an empty collection.

### 5. Feature consumption

`ScheduleHistory` stores neutralizations in an immutable index keyed by:

```text
(game_pk, official_date, team_id)
```

Production model-preparation game rows already carry both numeric
`away_team_id`/`home_team_id` and their team codes. `prepare.py` preserves
those fields and validates that the numeric identity and code describe the same
evaluated side. `_build_side_components()` derives the numeric ID from
`game[f"{side}_team_id"]` and passes it explicitly to
`_schedule_component()` alongside the existing team code.

The trusted binding for one side is the pair:

```text
(game[f"{side}_team_id"], game[f"{side}_team_code"])
```

from the authenticated prepared game row. `_build_side_components()` requires
the existing name-to-code mapping result to equal the prepared
`game[f"{side}_team_code"]`, requires its `team_code` argument to equal that
same value, and passes `game[f"{side}_team_id"]` without conversion. Team
names, abbreviations from another row, fuzzy lookup, and reverse inference from
the neutralization allowlist are forbidden.

Old non-target fixtures may omit numeric IDs because the ordinary branch does
not use them. A game whose `(game_pk, official_date)` belongs to the approved
target set must have the exact numeric team ID; missing or inconsistent
identity fails closed. `_schedule_component()` then checks the immutable index:

- exact hit: return the frozen neutral metric and identities;
- no hit: execute the existing ordinary schedule calculation unchanged;
- target game/date with missing or mismatched numeric team identity: fail
  closed instead of guessing from team code.

The hashes used to authorize a hit come from the authenticated history index,
never from caller-supplied game fields.

## Scoring behavior

An exact hit returns:

```python
{
    "value": 50.0,
    "sample_size": 0,
    "quality": 0.0,
    "reason_codes": [
        "schedule_neutral_duplicate_gamepk_dependency"
    ],
}
```

The fixed schedule weight remains unchanged. Quality zero marks the component
as unavailable without redistributing its weight. No scoring, configuration,
calibration, or probability code changes are permitted.

## Failure behavior

The pipeline fails closed on:

- missing or duplicate approved games;
- altered raw-object or normalized-anchor hashes;
- truncated hash comparison;
- changed date or team identity;
- extra or reordered neutralization rows;
- an unapproved neighbor becoming neutralized;
- superseding fields in an old-run payload;
- absent superseding fields in a new-run payload;
- schema/reducer/replay disagreement;
- transport dropping or mutating a record;
- ambiguous or missing numeric team identity on an approved target.

Ordinary non-target games retain existing behavior.

## Required implementation surface

The implementation plan must cover the narrowest production path that satisfies
the data flow:

- `mlb_backtest/v52_schedule_inventory.py`;
- `mlb_backtest/v52_sources/schedule.py`;
- `mlb_backtest/v52_game_evidence_inventory.py`;
- `mlb_backtest/v52_sources/history.py`;
- `mlb_backtest/v52_verify_sources.py`;
- `mlb_backtest/prepare.py`;
- `mlb_backtest/feature_engine.py`;
- `schemas/v5_2_game_evidence_inventory.schema.json`;
- `schemas/v5_2_season_source_snapshot.schema.json`;
- `schemas/v5_2_normalized_snapshot.schema.json`;
- focused producer, schedule/game-evidence, schema/replay, prepare-transport,
  and feature tests.

`mlb_backtest/v52_duplicate_gamepk_disposition.py` remains the authority owner
and is reused without changing its selection semantics. The implementation
plan must not add other production files unless this design is amended first.
No unrelated refactor is allowed.

## Test strategy

### Producer tests

- `test_three_exact_duplicate_dependency_neutralizations_and_no_others`
- `test_565535_and_566157_complete_hashes_are_known_but_unapproved`
- mutation table for missing, duplicate, reordered, altered, truncated, and
  extra targets
- deterministic canonical replay
- old-run byte-for-byte characterization

Hermetic tracked fixtures contain only the four required boundary games and
the minimal authenticated disposition inputs. Tests do not depend on the
untracked 21 MB quarantined raw file.

### Schema and verifier tests

- old branch accepts only old shape;
- superseding branch requires both provenance hashes and exactly three records;
- recomputation and normalized replay are byte-equivalent;
- disposition, raw hash, anchor hash, or run-identity mutation fails;
- filesystem and network calls remain unavailable during pure replay.

### Preparation tests

- all three rows survive producer-to-`ScheduleHistory` transport unchanged;
- empty old-run channel remains compatible;
- dropped, duplicated, reordered, or mutated records fail.

### Feature tests

- both team IDs for `566156` hit independently;
- team `133` for `565338` hits;
- wrong team/date/game and both known neighbors use the ordinary branch;
- hashes are never accepted from caller-supplied game fields;
- existing ordinary schedule characterization remains unchanged;
- fixed weights are not redistributed.

### Integration gate

One superseding normalized-snapshot replay must prove:

```text
schedule inventory
  -> game evidence with raw/anchor provenance
  -> history projection with exactly three records
  -> normalized schema and replay verification
  -> prepare transport
  -> exact feature neutralization
```

The equivalent old-run replay must remain byte-identical and contain no
neutralization channel.

## Acceptance criteria

Task 10 is complete only when:

1. exactly the three approved rows are emitted and consumed;
2. the two known neighbors are explicitly proven unapproved;
3. every authority boundary is closed and replayable;
4. old-run payloads and ordinary feature outputs remain unchanged;
5. no score, weight, calibration, or probability code changes;
6. focused and integration tests, Ruff, schema validation, and diff checks pass;
7. protected historical evidence remains untouched.
