# v5.2 Task 10 Schedule Neutralization Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Carry authenticated schedule provenance through the superseding v5.2 source pipeline and neutralize exactly three duplicate-gamePk-dependent team schedule metrics without changing old-run bytes or ordinary scoring.

**Architecture:** The existing superseding schedule inventory remains the immutable anchor authority. A pure helper derives raw-object and full normalized-anchor hashes, the game-evidence and history reducers carry them through closed source-only branches, and runtime replay requires both the authenticated disposition capability and the Git-bound three-row policy. Preparation transports the closed projection separately from ordinary schedule rows, and `ScheduleHistory` consumes it through an immutable `(game_pk, official_date, team_id)` index.

**Tech Stack:** Python 3, `unittest`, immutable dataclasses/mapping proxies, canonical JSON SHA-256, JSON Schema Draft 2020-12, Ruff, Git.

---

## Scope and file ownership

No new production module is permitted. The only production files changed by this plan are:

- `mlb_backtest/v52_schedule_inventory.py` — derive source-only anchor provenance from the existing closed anchors.
- `mlb_backtest/v52_sources/schedule.py` — build an ordinary-shape derived schedule adapter and carry trusted provenance into complete game projections.
- `mlb_backtest/v52_game_evidence_inventory.py` — authenticate and transport source provenance while preserving old/partial branches.
- `mlb_backtest/v52_sources/history.py` — own the frozen policy, validate its exact projection, and emit the three history records.
- `mlb_backtest/v52_verify_sources.py` — select old/source runtime branches and replay the reducers with the authenticated capability.
- `mlb_backtest/prepare.py` — validate and transport the projection into feature history.
- `mlb_backtest/feature_engine.py` — index and consume exact team-level neutralizations.
- `schemas/v5_2_game_evidence_inventory.schema.json` — close old, complete-source, and partial-source game shapes.
- `schemas/v5_2_season_source_snapshot.schema.json` — close old/source history payload shapes.
- `schemas/v5_2_normalized_snapshot.schema.json` — close old/source normalized envelopes and payloads.

Tests may add the one compact fixture
`tests/fixtures/v52_schedule_neutralizations/boundary_anchors.json`. The
quarantined 21 MB schedule response and all protected historical evidence stay
unmodified and untracked.

The policy identity is:

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

## Task 1: Freeze the four-game hermetic provenance boundary

**Files:**

- Create: `tests/fixtures/v52_schedule_neutralizations/boundary_anchors.json`
- Modify: `mlb_backtest/v52_schedule_inventory.py:434-500`
- Modify: `tests/test_v52_schedule_inventory.py`

- [ ] **Step 1: Add the exact compact fixture**

Create the fixture with this complete content:

```json
{
  "records": [
    {
      "anchor": {
        "alternate_site": false,
        "away_score": 6,
        "away_team_id": 133,
        "completed_innings": 9,
        "doubleheader_order": 1,
        "game_pk": 565338,
        "home_score": 4,
        "home_team_id": 114,
        "neutral_site": false,
        "official_date": "2019-05-20",
        "played": true,
        "raw_game_object_sha256": "92900ec47f924a1a68d533d63ae068faabd42262b808461a543974b61f75d5ef",
        "scheduled_innings": 9,
        "scheduled_start_utc": "2019-05-20T22:10:00Z",
        "season": 2019,
        "season_start_date": "2019-03-20",
        "terminal_status": "Final",
        "venue_id": 5,
        "winner_team_id": 133
      },
      "normalized_anchor_sha256": "5224b908bc7a340ac057bb9f56efa1e9ebdf3ac64972191611e0e0b802410c96"
    },
    {
      "anchor": {
        "alternate_site": false,
        "away_score": 5,
        "away_team_id": 146,
        "completed_innings": 11,
        "doubleheader_order": 1,
        "game_pk": 565535,
        "home_score": 4,
        "home_team_id": 116,
        "neutral_site": false,
        "official_date": "2019-05-21",
        "played": true,
        "raw_game_object_sha256": "11afba7dcd8ec0de2b78f8773e2c49c8f23a7d104e9b1a2b58d9a9f5893ff019",
        "scheduled_innings": 9,
        "scheduled_start_utc": "2019-05-21T23:10:00Z",
        "season": 2019,
        "season_start_date": "2019-03-20",
        "terminal_status": "Final",
        "venue_id": 2394,
        "winner_team_id": 146
      },
      "normalized_anchor_sha256": "131e5e173effb6b610c00624a3544bb36c0b42968e69c3d1c2e740a8e35248b2"
    },
    {
      "anchor": {
        "alternate_site": false,
        "away_score": 5,
        "away_team_id": 116,
        "completed_innings": 11,
        "doubleheader_order": 1,
        "game_pk": 566156,
        "home_score": 4,
        "home_team_id": 133,
        "neutral_site": false,
        "official_date": "2019-09-06",
        "played": true,
        "raw_game_object_sha256": "35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab",
        "scheduled_innings": 9,
        "scheduled_start_utc": "2019-09-07T02:07:00Z",
        "season": 2019,
        "season_start_date": "2019-03-20",
        "terminal_status": "Final",
        "venue_id": 10,
        "winner_team_id": 116
      },
      "normalized_anchor_sha256": "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29"
    },
    {
      "anchor": {
        "alternate_site": false,
        "away_score": 2,
        "away_team_id": 116,
        "completed_innings": 9,
        "doubleheader_order": 1,
        "game_pk": 566157,
        "home_score": 10,
        "home_team_id": 133,
        "neutral_site": false,
        "official_date": "2019-09-07",
        "played": true,
        "raw_game_object_sha256": "db8427682b066341a9f204dd2832ff418d93946b1ff12e07b147e7e60398ca6d",
        "scheduled_innings": 9,
        "scheduled_start_utc": "2019-09-08T01:07:00Z",
        "season": 2019,
        "season_start_date": "2019-03-20",
        "terminal_status": "Final",
        "venue_id": 10,
        "winner_team_id": 133
      },
      "normalized_anchor_sha256": "c5b3a273d946f93b500430363f679e351a1b03e739576a1f3830bafbc829769f"
    }
  ]
}
```

- [ ] **Step 2: Write the failing provenance tests**

Import `build_schedule_anchor_provenance` and add:

```python
NEUTRALIZATION_FIXTURE = (
    ROOT / "tests/fixtures/v52_schedule_neutralizations/boundary_anchors.json"
)

def test_source_anchor_provenance_binds_exact_boundary_hashes(self) -> None:
    fixture = json.loads(NEUTRALIZATION_FIXTURE.read_text())
    anchors = [row["anchor"] for row in fixture["records"]]
    actual = build_schedule_anchor_provenance(anchors)
    self.assertEqual(set(actual), {565338, 565535, 566156, 566157})
    for row in fixture["records"]:
        game_pk = row["anchor"]["game_pk"]
        self.assertEqual(
            actual[game_pk],
            {
                "game_pk": game_pk,
                "official_date": row["anchor"]["official_date"],
                "away_team_id": row["anchor"]["away_team_id"],
                "home_team_id": row["anchor"]["home_team_id"],
                "raw_game_object_sha256":
                    row["anchor"]["raw_game_object_sha256"],
                "normalized_anchor_sha256":
                    row["normalized_anchor_sha256"],
            },
        )

def test_source_anchor_provenance_rejects_closed_shape_attacks(self) -> None:
    fixture = json.loads(NEUTRALIZATION_FIXTURE.read_text())
    anchor = fixture["records"][0]["anchor"]
    attacks = (
        {key: value for key, value in anchor.items()
         if key != "raw_game_object_sha256"},
        {**anchor, "unexpected": True},
        {**anchor, "raw_game_object_sha256":
            anchor["raw_game_object_sha256"][:32]},
    )
    for attack in attacks:
        with self.subTest(attack=attack), self.assertRaises(ValueError):
            build_schedule_anchor_provenance([attack])
    with self.assertRaisesRegex(ValueError, "duplicate"):
        build_schedule_anchor_provenance([anchor, anchor])
```

- [ ] **Step 3: Run the tests and confirm RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_schedule_inventory.V52ScheduleInventoryTest.test_source_anchor_provenance_binds_exact_boundary_hashes \
  tests.test_v52_schedule_inventory.V52ScheduleInventoryTest.test_source_anchor_provenance_rejects_closed_shape_attacks \
  -v
```

Expected: import failure because `build_schedule_anchor_provenance` does not exist.

- [ ] **Step 4: Implement the pure closed-anchor helper**

Add beside `_derived_projection_anchors()`:

```python
def build_schedule_anchor_provenance(
    anchors: Sequence[Mapping[str, object]],
) -> Mapping[int, Mapping[str, object]]:
    result: dict[int, Mapping[str, object]] = {}
    for raw_anchor in anchors:
        if not isinstance(raw_anchor, Mapping):
            raise ValueError("source anchor provenance row differs")
        anchor = dict(raw_anchor)
        if set(anchor) != _SOURCE_ANCHOR_KEYS:
            raise ValueError("source anchor provenance keys differ")
        game_pk = _positive_int(
            anchor.get("game_pk"), "source anchor provenance gamePk"
        )
        if game_pk in result:
            raise ValueError("source anchor provenance contains duplicate gamePk")
        raw_sha256 = anchor.get("raw_game_object_sha256")
        if (
            not isinstance(raw_sha256, str)
            or _SHA256.fullmatch(raw_sha256) is None
        ):
            raise ValueError("source anchor raw-object provenance differs")
        result[game_pk] = MappingProxyType({
            "game_pk": game_pk,
            "official_date": anchor["official_date"],
            "away_team_id": anchor["away_team_id"],
            "home_team_id": anchor["home_team_id"],
            "raw_game_object_sha256": raw_sha256,
            "normalized_anchor_sha256":
                hashlib.sha256(_canonical_json_bytes(anchor)).hexdigest(),
        })
    return MappingProxyType(result)
```

Export the helper in `__all__`. Do not add `normalized_anchor_sha256` to the
schedule inventory artifact.

- [ ] **Step 5: Run GREEN checks and commit**

Run the two named tests, then:

```bash
ruff check mlb_backtest/v52_schedule_inventory.py \
  tests/test_v52_schedule_inventory.py
git diff --check
git add \
  mlb_backtest/v52_schedule_inventory.py \
  tests/test_v52_schedule_inventory.py \
  tests/fixtures/v52_schedule_neutralizations/boundary_anchors.json
git commit -m "feat: bind normalized schedule anchor provenance"
```

## Task 2: Carry provenance through complete game evidence only

**Files:**

- Modify: `mlb_backtest/v52_sources/schedule.py:342-507`
- Modify: `mlb_backtest/v52_game_evidence_inventory.py:81-110,691-730,747-929,1081-1227`
- Modify: `schemas/v5_2_game_evidence_inventory.schema.json`
- Modify: `schemas/v5_2_season_source_snapshot.schema.json`
- Modify: `tests/test_v52_source_schedule.py`
- Modify: `tests/test_v52_game_evidence_inventory.py`

- [ ] **Step 1: Write reducer and inventory RED tests**

Add these exact behaviors:

```python
def _test_schedule_provenance(anchor_sidecar):
    return {
        anchor["game_pk"]: {
            "game_pk": anchor["game_pk"],
            "official_date": anchor["official_date"],
            "away_team_id": anchor["away_team_id"],
            "home_team_id": anchor["home_team_id"],
            "raw_game_object_sha256": hashlib.sha256(
                f"raw:{anchor['game_pk']}".encode()
            ).hexdigest(),
            "normalized_anchor_sha256": hashlib.sha256(
                f"anchor:{anchor['game_pk']}".encode()
            ).hexdigest(),
        }
        for anchor in anchor_sidecar["semantics"]["anchors"]
    }

def test_game_snapshot_carries_source_anchor_provenance_only_when_supplied(self):
    anchor = recompute_schedule_anchor_sidecar(_schedule_raw(), _query())
    boxscore_sidecar = recompute_boxscore_sidecar(
        _boxscore_raw(), _box_query()
    )
    boxscores = {9001: _artifact(dict(boxscore_sidecar))}
    old = recompute_game_evidence_snapshot([anchor], boxscores)
    self.assertNotIn("raw_game_object_sha256", old["games"][0])
    source = recompute_game_evidence_snapshot(
        [anchor],
        boxscores,
        schedule_provenance=_test_schedule_provenance(anchor),
    )
    verified = next(row for row in source["games"] if row["played"] is True)
    unplayed = next(row for row in source["games"] if row["played"] is False)
    self.assertEqual(
        set(verified) - set(old["games"][0]),
        {"raw_game_object_sha256", "normalized_anchor_sha256"},
    )
    self.assertNotIn("raw_game_object_sha256", unplayed)
    self.assertNotIn("normalized_anchor_sha256", unplayed)

def test_game_snapshot_rejects_incomplete_schedule_provenance(self):
    anchor = recompute_schedule_anchor_sidecar(_schedule_raw(), _query())
    boxscore_sidecar = recompute_boxscore_sidecar(
        _boxscore_raw(), _box_query()
    )
    boxscores = {9001: _artifact(dict(boxscore_sidecar))}
    provenance = _test_schedule_provenance(anchor)
    changed = {
        game_pk: dict(value)
        for game_pk, value in provenance.items()
    }
    changed[9001]["official_date"] = "2019-01-01"
    for attack in (
        {},
        {**provenance, 999999: provenance[9001]},
        {9001: {"raw_game_object_sha256": "0" * 64}},
        changed,
    ):
        with self.subTest(attack=attack), self.assertRaises(ValueError):
            recompute_game_evidence_snapshot(
                [anchor],
                boxscores,
                schedule_provenance=attack,
            )
```

In the inventory tests, require the superseding complete `VERIFIED` games to
carry both fields, old complete games to omit them, partial `{game_pk, season}`
rows to remain valid, and final rebind to reject either changed hash.

- [ ] **Step 2: Run the focused modules and confirm RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_source_schedule \
  tests.test_v52_game_evidence_inventory \
  -v
```

Expected: unexpected `schedule_provenance` keyword and source schema/validator
failures.

- [ ] **Step 3: Separate the derived adapter from provenance**

Extend the reducer signature:

```python
def recompute_game_evidence_snapshot(
    schedule_anchors: Sequence[Mapping[str, object]],
    boxscores: Mapping[int, tuple[bytes, str]],
    *,
    schedule_provenance:
        Mapping[int, Mapping[str, object]] | None = None,
) -> Mapping[str, object]:
```

Before joining games, validate an exact provenance population when the keyword
is not `None`. Each entry must have exactly:

```python
{
    "game_pk": 565338,
    "official_date": "2019-05-20",
    "away_team_id": 133,
    "home_team_id": 114,
    "raw_game_object_sha256": "<lowercase 64 hex>",
    "normalized_anchor_sha256": "<lowercase 64 hex>",
}
```

The mapping population must equal the joined anchor population and every
game/date/team identity must equal its ordinary-shape anchor.

For each complete `VERIFIED` played game, add those two fields before computing
`source_game_sha256`. Do not add them to old games, partial inventory items, or
`INELIGIBLE_UNPLAYED` rows.

When the superseding 2019 inventory builds its synthetic query sidecar, strip
`raw_game_object_sha256` from each adapter anchor:

```python
def derived_schedule_sidecar_adapter(
    query: LogicalQuery,
    source_anchors: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    anchors = [
        {
            key: value
            for key, value in source_anchor.items()
            if key != "raw_game_object_sha256"
        }
        for source_anchor in source_anchors
        if source_anchor.get("season") == 2019
    ]
    if not anchors:
        raise ValueError("source disposition adapter anchors differ")
    return {
        "byte_count": 0,
        "grain": query.grain,
        "parser_version": query.parser_version,
        "query_id": logical_query_id(query),
        "raw_sha256": hashlib.sha256(b"").hexdigest(),
        "schema_version": 1,
        "semantics": {"anchors": anchors},
        "source": query.source,
    }
```

Validate every input anchor against the source closed shape before stripping
the one provenance field. Export this pure helper so inventory construction and
normalized replay use identical adapter bytes; import `logical_query_id` beside
`LogicalQuery`.

Pass the separately derived mapping:

```python
schedule_provenance = build_schedule_anchor_provenance(
    verified_schedule.payload["anchors"]
)
game_snapshot = dict(
    recompute_game_evidence_snapshot(
        schedule_sidecars,
        boxscores,
        schedule_provenance=(
            schedule_provenance
            if disposition_authority is not None
            else None
        ),
    )
)
```

- [ ] **Step 4: Close the runtime and schema branches**

Change the projection validator to:

```python
def _validate_game_projection(
    snapshot: Mapping[str, object],
    *,
    run_id: str,
) -> None:
```

The exact complete row keys are `_COMMON_GAME_KEYS` for old rows and
`_COMMON_GAME_KEYS | {"raw_game_object_sha256",
"normalized_anchor_sha256"}` for complete superseding `VERIFIED` rows.
Superseding `INELIGIBLE_UNPLAYED` rows retain the old key set. Source partial
rows keep exactly `{"game_pk", "season"}`. No other mixture is valid.

In both `schemas/v5_2_game_evidence_inventory.schema.json` and the game-evidence
branch of `schemas/v5_2_season_source_snapshot.schema.json`, define a source
complete game by extending the old complete game with:

```json
{
  "raw_game_object_sha256": {"$ref": "#/$defs/sha256"},
  "normalized_anchor_sha256": {"$ref": "#/$defs/sha256"}
}
```

Require both only for source `VERIFIED` rows. Keep old complete rows and source
partial rows closed, with different boundaries:

- `v5_2_game_evidence_inventory.schema.json` is the acquisition-progress
  artifact. Its source branch accepts a population containing only partial
  `{"game_pk", "season"}` rows and must not use `contains VERIFIED`.
- `v5_2_season_source_snapshot.schema.json` is terminal normalized input. Its
  source branch permits complete source `VERIFIED` rows plus complete
  `INELIGIBLE_UNPLAYED` rows, forbids partial rows, and uses `contains` to
  require at least one source `VERIFIED` row. Because this payload has no run ID,
  the `contains` also prevents ambiguity with the old branch.

- [ ] **Step 5: Run GREEN checks and commit**

Run both modules, schema validation, then:

```bash
ruff check \
  mlb_backtest/v52_sources/schedule.py \
  mlb_backtest/v52_game_evidence_inventory.py \
  tests/test_v52_source_schedule.py \
  tests/test_v52_game_evidence_inventory.py
git diff --check
git add \
  mlb_backtest/v52_sources/schedule.py \
  mlb_backtest/v52_game_evidence_inventory.py \
  schemas/v5_2_game_evidence_inventory.schema.json \
  schemas/v5_2_season_source_snapshot.schema.json \
  tests/test_v52_source_schedule.py \
  tests/test_v52_game_evidence_inventory.py
git commit -m "feat: carry schedule provenance through game evidence"
```

## Task 3: Replay superseding game provenance with the exact capability

**Files:**

- Modify: `mlb_backtest/v52_verify_sources.py:257-410,446-530,1462-1517,1592-2019`
- Modify: `schemas/v5_2_normalized_snapshot.schema.json`
- Modify: `tests/test_v52_verify_sources.py`

- [ ] **Step 1: Write source-game replay RED tests**

Add tests that establish this exact matrix:

```python
def test_source_game_replay_accepts_only_exact_derived_schedule_capability(self):
    snapshots = _build_game_normalized_snapshots(
        _SOURCE_RUN_ID,
        self.source_records,
        self.source_game_inventory,
        self.timezone_gate,
        self.source_schedule_inventory,
        duplicate_gamepk_disposition=self.disposition_binding,
        dispositioned_query_ids=(self.disposition_binding.query_id,),
    )
    source_2019 = next(row for row in snapshots if row.season == 2019)
    self.assertEqual(
        source_2019.duplicate_gamepk_disposition,
        self.disposition_binding,
    )
    for snapshot in snapshots:
        verified = [
            row for row in snapshot.payload["games"]
            if row["verification_status"] == "VERIFIED"
        ]
        self.assertTrue(verified)
        self.assertTrue(all(
            "raw_game_object_sha256" in row
            and "normalized_anchor_sha256" in row
            for row in verified
        ))

def test_source_game_replay_rejects_capability_and_provenance_mutations(self):
    for attack in (
        self.wrong_run_inputs,
        self.wrong_disposition_query_inputs,
        self.wrong_disposition_sidecar_inputs,
        self.changed_raw_hash_inputs,
        self.changed_anchor_hash_inputs,
    ):
        with self.subTest(attack=attack), self.assertRaises(ValueError):
            replay_verified_snapshots(attack)
```

Also assert that only the 2019 source envelope carries
`duplicate_gamepk_disposition`/`dispositioned_query_ids`; the other six source
seasons carry source game provenance but omit disposition envelope fields.

- [ ] **Step 2: Run the focused source replay and confirm RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_game_evidence_inventory \
  tests.test_v52_verify_sources \
  -v
```

Expected: `_verified_sidecar()` rejects `DERIVED_DISPOSITION`, capability is not
passed to the game reducer, or the normalized schema rejects source fields.

- [ ] **Step 3: Parse the inventory capability before building games**

Extract the existing inventory-binding logic into:

```python
def _game_inventory_disposition_binding(
    game_inventory: VerifiedGameEvidenceInventory,
    run_id: str,
) -> tuple[
    DuplicateGamePkDispositionBinding | None,
    tuple[str, ...],
]:
```

Call it before `_build_game_normalized_snapshots()` in
`load_snapshot_replay_inputs()`. Add keyword-only arguments to the pure game
builder:

```python
def _build_game_normalized_snapshots(
    run_id: str,
    records: Sequence[VerifiedQueryRecord],
    game_inventory: VerifiedGameEvidenceInventory,
    timezone_gate: VerifiedTimezoneGate,
    schedule_inventory: VerifiedScheduleAnchorInventory,
    *,
    duplicate_gamepk_disposition:
        DuplicateGamePkDispositionBinding | None = None,
    dispositioned_query_ids: tuple[str, ...] = (),
) -> tuple[NormalizedSnapshot, ...]:
```

`replay_verified_snapshots()` must pass the same capability from
`SnapshotReplayInputs`.

- [ ] **Step 4: Admit only the exact derived schedule record**

Do not widen `_verified_sidecar()`. Add a schedule-specific wrapper that:

1. routes ordinary fetched/protected schedule and boxscore records through
   `_verified_sidecar()`;
2. accepts `DERIVED_DISPOSITION` only for the exact source run, query ID,
   disposition binding, and sidecar SHA;
3. requires no retained raw bytes;
4. calls the shared ordinary-shape adapter from
   `v52_sources.schedule`;
5. derives the separate provenance map with
   `build_schedule_anchor_provenance()`;
6. performs the existing synthetic-to-published lineage replacement.

The reducer invocation is:

```python
season_provenance = build_schedule_anchor_provenance([
    anchor
    for anchor in schedule_inventory.payload["anchors"]
    if anchor["season"] == season
])
payload = recompute_game_evidence_snapshot(
    [schedule_sidecar],
    boxscores,
    schedule_provenance=(
        season_provenance if run_id == _SOURCE_RUN_ID else None
    ),
)
```

- [ ] **Step 5: Close and commit the normalized source-game branch**

Define `_SOURCE_RUN_ID =
"historical_walkforward_500_superseding_565534"` beside the existing endpoint
and season constants.

In the normalized schema and runtime validator:

- non-source runs use old game keys and no disposition;
- all seven source seasons require both hashes on complete `VERIFIED` games;
- source `INELIGIBLE_UNPLAYED` games use the old complete key set;
- source 2019 alone requires the exact disposition envelope;
- source seasons other than 2019 forbid disposition envelope fields.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_game_evidence_inventory \
  tests.test_v52_verify_sources \
  -v
ruff check mlb_backtest/v52_verify_sources.py \
  tests/test_v52_verify_sources.py
git diff --check
git add \
  mlb_backtest/v52_verify_sources.py \
  schemas/v5_2_normalized_snapshot.schema.json \
  tests/test_v52_verify_sources.py
git commit -m "feat: replay superseding game provenance exactly"
```

## Task 4: Emit the exact three-row history projection

**Files:**

- Modify: `mlb_backtest/v52_sources/history.py`
- Modify: `schemas/v5_2_season_source_snapshot.schema.json`
- Modify: `tests/test_v52_source_history.py`

- [ ] **Step 1: Write the mandatory policy tests**

Add exact local fixtures:

```python
def _task10_source_snapshot() -> dict[str, object]:
    fixture = json.loads(
        (
            ROOT
            / "tests/fixtures/v52_schedule_neutralizations/boundary_anchors.json"
        ).read_text()
    )
    games = []
    for record in fixture["records"]:
        anchor = record["anchor"]
        games.append({
            **anchor,
            "feature_history_cutoff": (
                date.fromisoformat(anchor["official_date"])
                - timedelta(days=1)
            ).isoformat(),
            "normalized_anchor_sha256":
                record["normalized_anchor_sha256"],
            "source_game_sha256": hashlib.sha256(
                f"source:{anchor['game_pk']}".encode()
            ).hexdigest(),
            "verification_status": "VERIFIED",
        })
    return {
        "games": games,
        "grain": "season_game_evidence_snapshot",
        "schema_version": 1,
        "source_query_sidecar_sha256s": ["1" * 64],
    }

def _task10_disposition() -> dict[str, object]:
    return {
        "query_id":
            "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095",
        "query_sidecar": {
            "path":
                "audit/v5_2_qd/historical_superseding_565534/acquisition/"
                "query_sidecars/"
                "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1.json",
            "sha256":
                "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1",
        },
        "success_receipt": {
            "path":
                "data/v5_2_qd/historical_superseding_565534/raw/queries/"
                "mlb_schedule_game_feed/"
                "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095/"
                "success.json",
            "sha256": "8" * 64,
        },
    }
```

Import `date` and `timedelta`, then add:

```python
def test_three_exact_duplicate_dependency_neutralizations_and_no_others(self):
    snapshot = recompute_prior_history_sidecar(
        _task10_source_snapshot(),
        source_snapshot_sha256="1" * 64,
        run_id=SOURCE_RUN_ID,
        duplicate_gamepk_disposition=_task10_disposition(),
    )
    identities = tuple(
        (
            row["target_raw_game_object_sha256"],
            row["target_normalized_anchor_sha256"],
            row["game_pk"],
            row["official_date"],
            row["team_id"],
        )
        for row in snapshot["schedule_neutralizations"]
    )
    self.assertEqual(identities, EXPECTED_NEUTRALIZATIONS)
    self.assertEqual(len(identities), 3)
    for row in snapshot["schedule_neutralizations"]:
        self.assertEqual(
            {
                "value": row["value"],
                "sample_size": row["sample_size"],
                "quality": row["quality"],
                "reason_codes": row["reason_codes"],
            },
            {
                "value": 50.0,
                "sample_size": 0,
                "quality": 0.0,
                "reason_codes": [
                    "schedule_neutral_duplicate_gamepk_dependency"
                ],
            },
        )

def test_565535_and_566157_complete_hashes_are_known_but_unapproved(self):
    emitted = set(EXPECTED_NEUTRALIZATIONS)
    for identity in KNOWN_UNAPPROVED_NEIGHBORS:
        self.assertNotIn(identity, emitted)
```

Also add a mutation table covering missing/duplicate target games, changed
date/team/raw hash/anchor hash, truncated hash, wrong disposition binding,
reordered output, extra output, and an empty new field in an old payload.

- [ ] **Step 2: Characterize the old bytes and confirm RED**

The unchanged old fixture must remain:

```python
old = recompute_prior_history_sidecar(
    _schedule_snapshot(),
    source_snapshot_sha256="1" * 64,
    run_id="historical_walkforward_500",
)
self.assertNotIn("schedule_neutralizations", old)
self.assertEqual(len(canonical_json_bytes(old) + b"\n"), 687)
self.assertEqual(
    hashlib.sha256(canonical_json_bytes(old) + b"\n").hexdigest(),
    "b4803935007402aa248928f01b06ba9f4d4c18463fdec3d574fa8b586578f941",
)
```

Run the two mandatory tests. Expected: missing constants/signature/field.

- [ ] **Step 3: Add one policy owner and one validator**

In `history.py`, define:

```python
SOURCE_RUN_ID = "historical_walkforward_500_superseding_565534"
EXPECTED_NEUTRALIZATIONS: tuple[
    tuple[str, str, int, str, int], ...
] = (
    (
        "35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab",
        "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29",
        566156, "2019-09-06", 116,
    ),
    (
        "35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab",
        "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29",
        566156, "2019-09-06", 133,
    ),
    (
        "92900ec47f924a1a68d533d63ae068faabd42262b808461a543974b61f75d5ef",
        "5224b908bc7a340ac057bb9f56efa1e9ebdf3ac64972191611e0e0b802410c96",
        565338, "2019-05-20", 133,
    ),
)
KNOWN_UNAPPROVED_NEIGHBORS: tuple[
    tuple[str, str, int, str, int], ...
] = (
    (
        "11afba7dcd8ec0de2b78f8773e2c49c8f23a7d104e9b1a2b58d9a9f5893ff019",
        "131e5e173effb6b610c00624a3544bb36c0b42968e69c3d1c2e740a8e35248b2",
        565535, "2019-05-21", 116,
    ),
    (
        "11afba7dcd8ec0de2b78f8773e2c49c8f23a7d104e9b1a2b58d9a9f5893ff019",
        "131e5e173effb6b610c00624a3544bb36c0b42968e69c3d1c2e740a8e35248b2",
        565535, "2019-05-21", 146,
    ),
    (
        "db8427682b066341a9f204dd2832ff418d93946b1ff12e07b147e7e60398ca6d",
        "c5b3a273d946f93b500430363f679e351a1b03e739576a1f3830bafbc829769f",
        566157, "2019-09-07", 116,
    ),
    (
        "db8427682b066341a9f204dd2832ff418d93946b1ff12e07b147e7e60398ca6d",
        "c5b3a273d946f93b500430363f679e351a1b03e739576a1f3830bafbc829769f",
        566157, "2019-09-07", 133,
    ),
)

def validate_schedule_neutralizations(
    value: object,
) -> tuple[Mapping[str, object], ...]:
    """Accept only empty legacy input or the exact canonical policy records."""
```

The validator requires the nine-key closed record, full hashes, canonical sort
order, fixed metric/reason, uniqueness, and exact policy equality. Return
mapping proxies inside a tuple. Export `SOURCE_RUN_ID`,
`EXPECTED_NEUTRALIZATIONS`, `KNOWN_UNAPPROVED_NEIGHBORS`,
`validate_schedule_neutralizations`, and `recompute_prior_history_sidecar` in
`__all__`.

- [ ] **Step 4: Extend the reducer without I/O**

Use this signature:

```python
def recompute_prior_history_sidecar(
    schedule_sidecar: Mapping[str, object],
    *,
    source_snapshot_sha256: str,
    run_id: str,
    duplicate_gamepk_disposition:
        Mapping[str, object] | None = None,
) -> Mapping[str, object]:
```

Derive and validate one season from the nonempty game population. The runtime
matrix is:

- every non-source run rejects a disposition argument and provenance fields,
  then returns the existing five keys;
- source seasons other than 2019 require source provenance on complete
  `VERIFIED` games, reject a disposition argument, and still return the existing
  five-key history payload;
- source 2019 requires the exact disposition query ID and sidecar path/SHA,
  reconstructs the 19-key anchor from every complete `VERIFIED` game, checks
  `canonical_sha256(anchor)`, finds every policy game exactly once, checks team
  participation, emits exactly three canonical records, and proves all known
  neighbors remain absent.

In the season-source schema, keep the old five-key branch and add one source
history branch whose `schedule_neutralizations` property is a `const` equal to
the exact three-record array. Empty, reordered, mutated, or additional rows
must fail schema validation.

- [ ] **Step 5: Run GREEN checks and commit**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_source_history -v
ruff check mlb_backtest/v52_sources/history.py \
  tests/test_v52_source_history.py
git diff --check
git add \
  mlb_backtest/v52_sources/history.py \
  schemas/v5_2_season_source_snapshot.schema.json \
  tests/test_v52_source_history.py
git commit -m "feat: emit exact schedule neutralization projection"
```

## Task 5: Close history replay around the authenticated disposition

**Files:**

- Modify: `mlb_backtest/v52_verify_sources.py:112-137,500-670,1590-1825,2023-2200`
- Modify: `schemas/v5_2_normalized_snapshot.schema.json`
- Modify: `tests/test_v52_verify_history_snapshots.py`
- Modify: `tests/test_v52_verify_sources.py`

- [ ] **Step 1: Write old/source replay RED tests**

Add tests that require:

```python
def test_superseding_history_replay_requires_exact_projection(self):
    source = self.source_history_snapshot()
    self.assertEqual(
        source.payload["schedule_neutralizations"],
        self.expected_neutralization_records,
    )
    validate_history_snapshot_lineage(source, self.source_game_snapshot)

def test_old_history_replay_remains_byte_identical_and_io_free(self):
    with patch("pathlib.Path.read_bytes", side_effect=AssertionError), \
         patch("urllib.request.urlopen", side_effect=AssertionError):
        replayed = validate_history_snapshot_lineage(
            self.old_history_snapshot, self.old_game_snapshot
        )
    self.assertNotIn(
        "schedule_neutralizations", replayed["payload"]
    )
```

Mutation cases must cover wrong run ID, disposition query/path/SHA, missing or
changed raw/anchor hash, mixed old/source keys, empty/reordered/extra history
rows, and source fields in any other run.

- [ ] **Step 2: Run focused replay tests and confirm RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_verify_history_snapshots \
  tests.test_v52_verify_sources \
  -v
```

Expected: source history payload keys are rejected, the capability is not
passed to the history reducer, or deterministic recomputation differs.

- [ ] **Step 3: Pass the authenticated capability to both history replay paths**

Add keyword-only capability arguments:

```python
def _build_history_normalized_snapshots(
    run_id: str,
    game_snapshots: Sequence[NormalizedSnapshot],
    *,
    duplicate_gamepk_disposition:
        DuplicateGamePkDispositionBinding | None = None,
    dispositioned_query_ids: tuple[str, ...] = (),
) -> tuple[NormalizedSnapshot, ...]:
```

`replay_verified_snapshots()` passes the global capability into this builder.
The builder requires it to equal the 2019 game snapshot capability and passes
`.as_mapping()` to `recompute_prior_history_sidecar()` only for source 2019.
The other six source seasons and every old run pass `None`.

`_validate_history_snapshot_lineage()` derives the same argument from the
authenticated game snapshot and re-runs the reducer without filesystem or
network access.

- [ ] **Step 4: Select the exact source-2019 runtime and schema branch**

Add distinct runtime sets:

```python
_SOURCE_HISTORY_PAYLOAD_KEYS = _HISTORY_PAYLOAD_KEYS | {
    "schedule_neutralizations",
}
```

`_validate_normalized_snapshot_mapping()` selects by authenticated envelope
`run_id` and `season`:

- source 2019 history requires the exact three-record array and exact
  `duplicate_gamepk_disposition`/`dispositioned_query_ids`;
- source history for the other six seasons uses the old five-key payload and
  forbids disposition fields and even an empty new array;
- every non-source run uses the old five-key payload and forbids source fields.

Update both reducer calls:

```python
payload = recompute_prior_history_sidecar(
    game_snapshot.payload,
    source_snapshot_sha256=game_snapshot.sha256,
    run_id=game_snapshot.run_id,
    duplicate_gamepk_disposition=(
        game_snapshot.duplicate_gamepk_disposition.as_mapping()
        if (
            game_snapshot.run_id == _SOURCE_RUN_ID
            and game_snapshot.season == 2019
            and game_snapshot.duplicate_gamepk_disposition is not None
        )
        else None
    ),
)
```

In `v5_2_normalized_snapshot.schema.json`, split history into source-2019,
source-other-season, and non-source closed branches. The neutralization record
schema has the exact nine keys and fixed metric/reason values; runtime enforces
literal membership and order. Keep every payload `additionalProperties: false`.

- [ ] **Step 5: Run GREEN checks and commit**

Run the two modules and schema checks, then:

```bash
ruff check mlb_backtest/v52_verify_sources.py \
  tests/test_v52_verify_history_snapshots.py \
  tests/test_v52_verify_sources.py
git diff --check
git add \
  mlb_backtest/v52_verify_sources.py \
  schemas/v5_2_normalized_snapshot.schema.json \
  tests/test_v52_verify_history_snapshots.py \
  tests/test_v52_verify_sources.py
git commit -m "feat: verify schedule neutralization replay"
```

## Task 6: Transport the closed projection through preparation

**Files:**

- Modify: `mlb_backtest/prepare.py:149-205,1166-1340,1344-1410`
- Modify: `tests/test_prepare.py`

- [ ] **Step 1: Write transport and mutation RED tests**

Import `copy` and `EXPECTED_NEUTRALIZATIONS`, then add these module helpers:

```python
import copy

from mlb_backtest.v52_sources.history import EXPECTED_NEUTRALIZATIONS

def _neutralization_records() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "game_pk": game_pk,
            "official_date": official_date,
            "team_id": team_id,
            "target_raw_game_object_sha256": raw_sha256,
            "target_normalized_anchor_sha256": anchor_sha256,
            "value": 50.0,
            "sample_size": 0,
            "quality": 0.0,
            "reason_codes": [
                "schedule_neutral_duplicate_gamepk_dependency"
            ],
        }
        for (
            raw_sha256,
            anchor_sha256,
            game_pk,
            official_date,
            team_id,
        ) in EXPECTED_NEUTRALIZATIONS
    )

def _task10_schedule_build(
    neutralizations: tuple[dict[str, object], ...] = (),
) -> ScheduleHistoryBuild:
    return ScheduleHistoryBuild(
        rows=(
            {
                "game_pk": 565338,
                "official_date": "2019-05-20",
                "team_codes": ["AWY", "HOM"],
                "away_team_code": "AWY",
                "home_team_code": "HOM",
                "away_team_id": 133,
                "home_team_id": 114,
                "venue_name": "Park",
                "innings": 9,
            },
            {
                "game_pk": 566156,
                "official_date": "2019-09-06",
                "team_codes": ["AWY", "HOM"],
                "away_team_code": "AWY",
                "home_team_code": "HOM",
                "away_team_id": 116,
                "home_team_id": 133,
                "venue_name": "Park",
                "innings": 11,
            },
        ),
        coverage_start="2019-05-01",
        coverage_end="2019-09-30",
        coverage_complete=True,
        source_manifest_sha256="1" * 64,
        expected_game_pks=(565338, 566156),
        schedule_neutralizations=neutralizations,
    )
```

Add the tests using only local fixtures:

```python
def test_schedule_history_build_transports_exact_schedule_neutralizations(self):
    parsed = parse_statcast(STATCAST)
    source = StatcastData(
        tuple(dict(row, outs_recorded=0) for row in parsed.pitches),
        tuple(dict(row, outs_recorded=0) for row in parsed.plate_appearances),
    )
    records = _neutralization_records()
    history = build_feature_history(
        source,
        {slot: 1 / 9 for slot in range(1, 10)},
        _task10_schedule_build(records),
    )
    self.assertEqual(
        tuple(map(dict, history.schedule_history.schedule_neutralizations)),
        records,
    )
    with self.assertRaises(TypeError):
        history.schedule_history.neutralization_by_key[
            (566156, "2019-09-06", 116)
        ] = {}

def test_schedule_history_build_rejects_neutralization_mutations(self):
    records = _neutralization_records()
    changed_hash = list(copy.deepcopy(records))
    changed_hash[0]["target_raw_game_object_sha256"] = "0" * 64
    changed_metric = list(copy.deepcopy(records))
    changed_metric[0]["quality"] = 1.0
    attacks = (
        records[:-1],
        records + records[:1],
        tuple(reversed(records)),
        tuple(changed_hash),
        tuple(changed_metric),
    )
    for attack in attacks:
        with self.subTest(attack=attack), self.assertRaises(ValueError):
            _task10_schedule_build(attack)
    self.assertEqual(
        _task10_schedule_build().schedule_neutralizations,
        (),
    )
```

Also retain a legacy constructor test with no new field; it must produce an
empty channel.

- [ ] **Step 2: Run the named tests and confirm RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_prepare.PrepareBoundaryTest.test_schedule_history_build_transports_exact_schedule_neutralizations \
  tests.test_prepare.PrepareBoundaryTest.test_schedule_history_build_rejects_neutralization_mutations \
  -v
```

Expected: constructor or attribute failure.

- [ ] **Step 3: Add the immutable build field**

Import `validate_schedule_neutralizations` and append the defaulted field after
`provenance` to preserve positional compatibility:

```python
@dataclass(frozen=True)
class ScheduleHistoryBuild:
    rows: tuple[dict[str, object], ...]
    coverage_start: str
    coverage_end: str
    coverage_complete: bool
    source_manifest_sha256: str | None
    expected_game_pks: tuple[int, ...]
    provenance: Mapping[str, object] = dataclass_field(default_factory=dict)
    schedule_neutralizations: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        # Insert these lines after the current coverage, row, and manifest checks.
        canonical = validate_schedule_neutralizations(
            self.schedule_neutralizations
        )
        object.__setattr__(self, "schedule_neutralizations", canonical)
```

- [ ] **Step 4: Preserve authenticated numeric side bindings and pass both channels**

In `schedule_history_rows()`, require positive distinct
`away_team_id`/`home_team_id` from the verified schedule game and add these
exact fields to each production row:

```python
row = {
    "game_pk": game_pk,
    "official_date": official.isoformat(),
    "team_codes": [away_code, home_code],
    "away_team_code": away_code,
    "home_team_code": home_code,
    "away_team_id": _positive_id(
        game.get("away_team_id"), "schedule away_team_id"
    ),
    "home_team_id": _positive_id(
        game.get("home_team_id"), "schedule home_team_id"
    ),
    "venue_name": venue_name,
    "innings": innings,
    "final_outs": game.get("final_outs"),
    "doubleheader_marker": game.get("doubleheader_marker"),
}
if row["away_team_id"] == row["home_team_id"]:
    raise ValueError("schedule history requires distinct numeric team identities")
```

Extend
`test_schedule_history_build_has_codes_markers_venue_and_coverage` to assert
both IDs and both codes survive the verified `schedule_history_rows()` path.
Legacy direct `ScheduleHistory` fixtures may still omit all numeric side fields;
partial presence is invalid.

Pass the neutralization channel separately into `ScheduleHistory`:

Change only the construction boundary:

```python
frozen_schedule = ImmutableScheduleHistory(
    schedule_build.rows,
    coverage_start=schedule_build.coverage_start,
    coverage_end=schedule_build.coverage_end,
    coverage_complete=schedule_build.coverage_complete,
    schedule_neutralizations=schedule_build.schedule_neutralizations,
)
```

Do not place the records inside generic provenance.

- [ ] **Step 5: Run GREEN checks and commit**

Run all prepare tests, then:

```bash
ruff check mlb_backtest/prepare.py tests/test_prepare.py
git diff --check
git add mlb_backtest/prepare.py tests/test_prepare.py
git commit -m "feat: transport schedule neutralizations to feature history"
```

## Task 7: Build the immutable exact-key feature index

**Files:**

- Modify: `mlb_backtest/feature_engine.py:101-240`
- Modify: `tests/test_feature_engine.py`

- [ ] **Step 1: Write index RED tests**

Import the policy and add a local fixture builder:

```python
from mlb_backtest.v52_sources.history import EXPECTED_NEUTRALIZATIONS

def task10_neutralization_records() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "game_pk": game_pk,
            "official_date": official_date,
            "team_id": team_id,
            "target_raw_game_object_sha256": raw_sha256,
            "target_normalized_anchor_sha256": anchor_sha256,
            "value": 50.0,
            "sample_size": 0,
            "quality": 0.0,
            "reason_codes": [
                "schedule_neutral_duplicate_gamepk_dependency"
            ],
        }
        for (
            raw_sha256,
            anchor_sha256,
            game_pk,
            official_date,
            team_id,
        ) in EXPECTED_NEUTRALIZATIONS
    )

def task10_schedule_rows() -> tuple[dict[str, object], ...]:
    identities = (
        (565338, "2019-05-20", 133, 114),
        (565535, "2019-05-21", 146, 116),
        (566156, "2019-09-06", 116, 133),
        (566157, "2019-09-07", 116, 133),
    )
    return tuple(
        {
            "game_pk": game_pk,
            "official_date": official_date,
            "team_codes": ["AWY", "HOM"],
            "away_team_code": "AWY",
            "home_team_code": "HOM",
            "away_team_id": away_team_id,
            "home_team_id": home_team_id,
            "venue_name": "Park",
            "innings": 9,
        }
        for game_pk, official_date, away_team_id, home_team_id
        in identities
    )

def test_schedule_neutralization_index_is_exact_and_immutable(self):
    history = ScheduleHistory(
        task10_schedule_rows(),
        coverage_start="2019-05-01",
        coverage_end="2019-09-30",
        coverage_complete=True,
        schedule_neutralizations=task10_neutralization_records(),
    )
    self.assertEqual(
        set(history.neutralization_by_key),
        {
            (566156, "2019-09-06", 116),
            (566156, "2019-09-06", 133),
            (565338, "2019-05-20", 133),
        },
    )
    self.assertEqual(
        history.neutralization_game_dates,
        frozenset({
            (566156, "2019-09-06"),
            (565338, "2019-05-20"),
        }),
    )
    self.assertEqual(
        history.team_id_by_game_date_code[
            (566156, "2019-09-06", "AWY")
        ],
        116,
    )
    self.assertEqual(
        history.team_id_by_game_date_code[
            (566156, "2019-09-06", "HOM")
        ],
        133,
    )
    with self.assertRaises(TypeError):
        history.neutralization_by_key[
            (566156, "2019-09-06", 116)
        ] = {}
```

Keep the existing ordinary global-unique `game_pk` test unchanged.

- [ ] **Step 2: Run the named test and confirm RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_feature_engine.FeatureEngineTest.test_schedule_neutralization_index_is_exact_and_immutable \
  -v
```

Expected: unexpected constructor keyword or missing index.

- [ ] **Step 3: Validate once and build separate indexes**

Extend only the keyword-only constructor:

```python
def __init__(
    self,
    rows: Iterable[Mapping[str, object]],
    *,
    coverage_start: str | None,
    coverage_end: str | None,
    coverage_complete: bool,
    schedule_neutralizations:
        Iterable[Mapping[str, object]] = (),
) -> None:
```

After ordinary-row indexes are built:

```python
canonical = validate_schedule_neutralizations(schedule_neutralizations)
self.schedule_neutralizations = canonical
self.neutralization_by_key = MappingProxyType({
    (
        int(row["game_pk"]),
        str(row["official_date"]),
        int(row["team_id"]),
    ): row
    for row in canonical
})
self.neutralization_game_dates = frozenset(
    (int(row["game_pk"]), str(row["official_date"]))
    for row in canonical
)
```

While validating ordinary rows, accept either:

- the legacy shape with all of `away_team_code`, `away_team_id`,
  `home_team_id` absent; or
- the production side-binding shape with all four
  `away_team_code`, `home_team_code`, `away_team_id`, `home_team_id` present,
  distinct positive IDs, distinct codes, and
  `team_codes == [away_team_code, home_team_code]`.

Build the authenticated side index only from the complete production shape:

```python
team_id_by_game_date_code: dict[tuple[int, str, str], int] = {}
for row in canonical:
    if "away_team_id" not in row:
        continue
    identity = (int(row["game_pk"]), str(row["official_date"]))
    for side in ("away", "home"):
        key = (identity[0], identity[1], str(row[f"{side}_team_code"]))
        if key in team_id_by_game_date_code:
            raise ValueError("schedule side identity is duplicated")
        team_id_by_game_date_code[key] = int(row[f"{side}_team_id"])
self.team_id_by_game_date_code = MappingProxyType(
    team_id_by_game_date_code
)
```

Do not insert neutralization records into `rows`, `by_team`, `by_team_date`, or
`game_dates`; the authenticated side index is derived only from ordinary
production schedule rows.

- [ ] **Step 4: Run ordinary and new index regressions**

Run the new test plus:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_feature_engine.FeatureEngineTest.test_schedule_history_rejects_malformed_rows \
  tests.test_feature_engine.FeatureEngineTest.test_schedule_history_indexes_once_for_reasonable_repeated_builds \
  -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
ruff check mlb_backtest/feature_engine.py tests/test_feature_engine.py
git diff --check
git add mlb_backtest/feature_engine.py tests/test_feature_engine.py
git commit -m "feat: index exact schedule neutralizations"
```

## Task 8: Consume only authenticated team/date hits

**Files:**

- Modify: `mlb_backtest/feature_engine.py:893-1000,1322-1370`
- Modify: `tests/test_feature_engine.py`

- [ ] **Step 1: Write exact-hit and negative RED tests**

Reuse `task10_neutralization_records()` from Task 7 and add:

```python
TASK10_REASON = "schedule_neutral_duplicate_gamepk_dependency"

def task10_feature_history() -> FeatureHistory:
    return FeatureHistory(
        data([]),
        slot_weights={slot: 1 / 9 for slot in range(1, 10)},
        schedule_history=ScheduleHistory(
            task10_schedule_rows(),
            coverage_start="2019-05-01",
            coverage_end="2019-09-30",
            coverage_complete=True,
            schedule_neutralizations=task10_neutralization_records(),
        ),
    )

def task10_game(
    game_pk: int,
    official_date: str,
    away_team_id: int,
    home_team_id: int,
) -> dict[str, object]:
    return game(
        game_pk=game_pk,
        official_date=official_date,
        scheduled_start_utc=f"{official_date}T23:00:00Z",
        away_team_id=away_team_id,
        home_team_id=home_team_id,
        away_team_code="AWY",
        home_team_code="HOM",
    )

def task10_build(target: dict[str, object]) -> dict[str, object]:
    as_of = (
        date.fromisoformat(str(target["official_date"])) - timedelta(days=1)
    ).isoformat()
    return build(
        target,
        task10_feature_history(),
        league_era={as_of: era_record(as_of=as_of)},
    )
```

Add complete tests:

```python
def test_approved_schedule_neutralizations_override_only_matching_side(self):
    both = task10_build(task10_game(
        566156, "2019-09-06", away_team_id=116, home_team_id=133
    ))
    for side in ("away", "home"):
        self.assertEqual(
            both[side]["schedule"],
            {
                "value": 50.0,
                "sample_size": 0,
                "quality": 0.0,
                "flags": [TASK10_REASON],
            },
        )
    one = task10_build(task10_game(
        565338, "2019-05-20", away_team_id=133, home_team_id=114
    ))
    self.assertEqual(one["away"]["schedule"]["flags"], [TASK10_REASON])
    self.assertNotIn(TASK10_REASON, one["home"]["schedule"]["flags"])

def test_target_requires_prepared_numeric_id_and_matching_code(self):
    base = task10_game(
        566156, "2019-09-06", away_team_id=116, home_team_id=133
    )
    attacks = []
    for key in ("away_team_id", "away_team_code"):
        attack = dict(base)
        attack.pop(key)
        attacks.append(attack)
    attacks.extend((
        {**base, "away_team_id": True},
        {**base, "away_team_id": 0},
        {**base, "away_team_id": 133},
        {**base, "away_team_code": "BAD"},
    ))
    for attack in attacks:
        with self.subTest(attack=attack), self.assertRaises(ValueError):
            task10_build(attack)

def test_unapproved_side_of_target_game_uses_existing_schedule_path(self):
    result = task10_build(task10_game(
        565338, "2019-05-20", away_team_id=133, home_team_id=114
    ))
    self.assertEqual(result["away"]["schedule"]["flags"], [TASK10_REASON])
    self.assertNotIn(TASK10_REASON, result["home"]["schedule"]["flags"])

def test_known_neighbors_and_non_target_games_remain_ordinary(self):
    for target in (
        task10_game(565535, "2019-05-21", 146, 116),
        task10_game(566157, "2019-09-07", 116, 133),
        game(),
    ):
        result = task10_build(target)
        for side in ("away", "home"):
            self.assertNotIn(
                TASK10_REASON, result[side]["schedule"].get("flags", [])
            )

def test_caller_fields_cannot_authorize_schedule_neutralization(self):
    target = task10_game(565535, "2019-05-21", 146, 116)
    target.update({
        "target_raw_game_object_sha256":
            EXPECTED_NEUTRALIZATIONS[0][0],
        "target_normalized_anchor_sha256":
            EXPECTED_NEUTRALIZATIONS[0][1],
        "reason_codes": [TASK10_REASON],
    })
    result = task10_build(target)
    for side in ("away", "home"):
        self.assertNotIn(
            TASK10_REASON, result[side]["schedule"].get("flags", [])
        )
```

The three approved side results must be:

```python
{
    "value": 50.0,
    "sample_size": 0,
    "quality": 0.0,
    "flags": [
        "schedule_neutral_duplicate_gamepk_dependency"
    ],
}
```

The history record uses `reason_codes`; the feature metric converts them to the
existing `flags` contract. Do not add `reason_codes` to raw feature output.

- [ ] **Step 2: Run the named tests and confirm RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_feature_engine.FeatureEngineTest.test_approved_schedule_neutralizations_override_only_matching_side \
  tests.test_feature_engine.FeatureEngineTest.test_target_requires_prepared_numeric_id_and_matching_code \
  tests.test_feature_engine.FeatureEngineTest.test_unapproved_side_of_target_game_uses_existing_schedule_path \
  tests.test_feature_engine.FeatureEngineTest.test_known_neighbors_and_non_target_games_remain_ordinary \
  tests.test_feature_engine.FeatureEngineTest.test_caller_fields_cannot_authorize_schedule_neutralization \
  -v
```

Expected: ordinary schedule computation or missing target identity validation.

- [ ] **Step 3: Bind the prepared team pair only on target dates**

In `_build_side_components()`:

```python
name = str(game[f"{side}_team"])
if name not in team_code_by_name:
    raise ValueError(f"team mapping missing {name}")
mapped_code = team_code_by_name[name]
identity = (int(game["game_pk"]), target.isoformat())
team_id: int | None = None
if identity in history.schedule_history.neutralization_game_dates:
    prepared_code = game.get(f"{side}_team_code")
    team_id = game.get(f"{side}_team_id")
    if prepared_code != mapped_code:
        raise ValueError("prepared team code differs from frozen team mapping")
    if type(team_id) is not int or team_id <= 0:
        raise ValueError("approved target requires numeric team identity")
    expected_team_id = (
        history.schedule_history.team_id_by_game_date_code.get(
            (identity[0], identity[1], mapped_code)
        )
    )
    if expected_team_id is None or team_id != expected_team_id:
        raise ValueError(
            "prepared numeric team identity differs from verified schedule"
        )
```

Non-target legacy fixtures may continue to omit numeric IDs and prepared codes.

- [ ] **Step 4: Check the exact index before ordinary calculation**

Use:

```python
def _schedule_component(
    game: Mapping[str, object],
    history: FeatureHistory,
    team_code: str,
    team_id: int | None,
    target: date,
) -> dict:
    game_pk = game.get("game_pk")
    game_date = target.isoformat()
    if (
        game_pk, game_date
    ) in history.schedule_history.neutralization_game_dates:
        if type(team_id) is not int or team_id <= 0:
            raise ValueError("approved target requires numeric team identity")
        record = history.schedule_history.neutralization_by_key.get(
            (game_pk, game_date, team_id)
        )
        if record is not None:
            return _metric(
                record["value"],
                record["sample_size"],
                record["quality"],
                flags=list(record["reason_codes"]),
            )
```

Insert this block immediately before the current `prior = [...]` statement, so
the current ordinary calculation remains the no-hit continuation. Pass
`mapped_code` and `team_id` from `_build_side_components()`. A valid
non-allowlisted opponent side of 565338 follows the ordinary branch. Caller
hash-like fields are ignored.

- [ ] **Step 5: Run GREEN checks and commit**

Run the full feature module, then:

```bash
ruff check mlb_backtest/feature_engine.py tests/test_feature_engine.py
git diff --check
git add mlb_backtest/feature_engine.py tests/test_feature_engine.py
git commit -m "feat: neutralize three duplicate-dependent schedule metrics"
```

## Task 9: Prove the full old/source integration gate

**Files:**

- Modify: `tests/test_v52_verify_history_snapshots.py`
- Modify: `tests/test_prepare.py`
- Modify: `tests/test_feature_engine.py`

- [ ] **Step 1: Add one source end-to-end test**

In `tests/test_v52_verify_history_snapshots.py`, import
`build_schedule_anchor_provenance`, `recompute_game_evidence_snapshot`,
`recompute_prior_history_sidecar`, `ScheduleHistoryBuild`,
`build_feature_history`, `build_raw_game_features`, `StatcastData`, and the
feature test helpers `game`, `mapping`, and `era_record`. Add this local
fixture builder:

```python
from mlb_backtest.feature_engine import build_raw_game_features
from mlb_backtest.prepare import ScheduleHistoryBuild, build_feature_history
from mlb_backtest.statcast import StatcastData
from mlb_backtest.v52_schedule_inventory import (
    build_schedule_anchor_provenance,
)
from mlb_backtest.v52_sources.history import (
    recompute_prior_history_sidecar,
)
from mlb_backtest.v52_sources.schedule import (
    recompute_game_evidence_snapshot,
)

def _task10_source_game_payload() -> dict[str, object]:
    fixture = json.loads(
        (
            ROOT
            / "tests/fixtures/v52_schedule_neutralizations/boundary_anchors.json"
        ).read_text()
    )
    source_anchors = [row["anchor"] for row in fixture["records"]]
    ordinary_anchors = [
        {
            key: value
            for key, value in anchor.items()
            if key != "raw_game_object_sha256"
        }
        for anchor in source_anchors
    ]
    schedule_sidecar = {
        "byte_count": 0,
        "grain": "season_schedule_and_game",
        "parser_version": "v5.2.1",
        "query_id": "1" * 64,
        "raw_sha256": hashlib.sha256(b"").hexdigest(),
        "schema_version": 1,
        "semantics": {"anchors": ordinary_anchors},
        "source": "mlb_schedule_game_feed",
    }
    boxscores = {}
    for offset, anchor in enumerate(source_anchors, start=1):
        game_pk = anchor["game_pk"]
        semantics = {
            "actual_starter_ids": [10_000 + offset, 20_000 + offset],
            "away_score": anchor["away_score"],
            "away_starting_hitter_ids":
                list(range(30_000 + offset * 10, 30_009 + offset * 10)),
            "away_team_id": anchor["away_team_id"],
            "game_pk": game_pk,
            "home_score": anchor["home_score"],
            "home_starting_hitter_ids":
                list(range(40_000 + offset * 10, 40_009 + offset * 10)),
            "home_team_id": anchor["home_team_id"],
            "official_date": anchor["official_date"],
            "winner_team_id": anchor["winner_team_id"],
        }
        sidecar = {
            "byte_count": 0,
            "grain": "season_schedule_and_game",
            "parser_version": "v5.2.1",
            "query_id": hashlib.sha256(
                f"boxscore:{game_pk}".encode()
            ).hexdigest(),
            "raw_sha256": hashlib.sha256(
                f"raw:{game_pk}".encode()
            ).hexdigest(),
            "schema_version": 1,
            "semantics": semantics,
            "source": "mlb_schedule_game_feed",
        }
        contents = canonical_json_bytes(sidecar) + b"\n"
        boxscores[game_pk] = (
            contents,
            hashlib.sha256(contents).hexdigest(),
        )
    return dict(recompute_game_evidence_snapshot(
        [schedule_sidecar],
        boxscores,
        schedule_provenance=build_schedule_anchor_provenance(
            source_anchors
        ),
    ))
```

Add the complete vertical test:

```python
def test_task10_source_provenance_to_exact_feature_hits_end_to_end(self):
    from mlb_backtest import v52_verify_sources as verifier
    from tests.test_feature_engine import era_record, game, mapping

    run_id = "historical_walkforward_500_superseding_565534"
    disposition = verifier.DuplicateGamePkDispositionBinding(
        query_id=(
            "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095"
        ),
        query_sidecar=verifier.AuthorityBinding(
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            "query_sidecars/"
            "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1.json",
            "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1",
        ),
        success_receipt=verifier.AuthorityBinding(
            "data/v5_2_qd/historical_superseding_565534/raw/queries/"
            "mlb_schedule_game_feed/"
            "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095/"
            "success.json",
            "8" * 64,
        ),
    )
    game_payload = _task10_source_game_payload()
    game_mapping = {
        "grain": "season_game_evidence_snapshot",
        "logical_query_ids": ["1" * 64],
        "payload": game_payload,
        "run_id": run_id,
        "schema_version": 1,
        "season": 2019,
        "source": "mlb_schedule_game_feed",
        "source_query_sidecar_sha256s":
            game_payload["source_query_sidecar_sha256s"],
        "split_receipt_sha256s": [],
        "status": "READY",
        "duplicate_gamepk_disposition": disposition.as_mapping(),
        "dispositioned_query_ids": [disposition.query_id],
    }
    game_bytes = canonical_json_bytes(game_mapping) + b"\n"
    game_snapshot = verifier.NormalizedSnapshot(
        schema_version=1,
        run_id=run_id,
        season=2019,
        source="mlb_schedule_game_feed",
        grain="season_game_evidence_snapshot",
        status="READY",
        logical_query_ids=("1" * 64,),
        source_query_sidecar_sha256s=tuple(
            game_payload["source_query_sidecar_sha256s"]
        ),
        split_receipt_sha256s=(),
        payload=game_payload,
        canonical_bytes=game_bytes,
        sha256=hashlib.sha256(game_bytes).hexdigest(),
        duplicate_gamepk_disposition=disposition,
        dispositioned_query_ids=(disposition.query_id,),
    )
    history_payload = recompute_prior_history_sidecar(
        game_payload,
        source_snapshot_sha256=game_snapshot.sha256,
        run_id=run_id,
        duplicate_gamepk_disposition=disposition.as_mapping(),
    )
    self.assertEqual(len(history_payload["schedule_neutralizations"]), 3)
    history_snapshot = verifier._history_normalized_snapshot(
        run_id, game_snapshot, history_payload
    )
    schema = json.loads(SCHEMA.read_text())
    jsonschema.validate(
        game_snapshot.as_mapping(),
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    jsonschema.validate(
        history_snapshot.as_mapping(),
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    with patch("pathlib.Path.read_bytes", side_effect=AssertionError), \
         patch("urllib.request.urlopen", side_effect=AssertionError):
        verifier._validate_history_snapshot_lineage(
            history_snapshot, game_snapshot
        )

    schedule_build = ScheduleHistoryBuild(
        rows=({
            "game_pk": 566156,
            "official_date": "2019-09-06",
            "team_codes": ["AWY", "HOM"],
            "away_team_code": "AWY",
            "home_team_code": "HOM",
            "away_team_id": 116,
            "home_team_id": 133,
            "venue_name": "Park",
            "innings": 11,
        },),
        coverage_start="2019-05-01",
        coverage_end="2019-09-30",
        coverage_complete=True,
        source_manifest_sha256="9" * 64,
        expected_game_pks=(566156,),
        schedule_neutralizations=tuple(
            history_payload["schedule_neutralizations"]
        ),
    )
    feature_history = build_feature_history(
        StatcastData((), ()),
        {slot: 1 / 9 for slot in range(1, 10)},
        schedule_build,
    )
    target = game(
        game_pk=566156,
        official_date="2019-09-06",
        scheduled_start_utc="2019-09-07T02:07:00Z",
        away_team_id=116,
        home_team_id=133,
        away_team_code="AWY",
        home_team_code="HOM",
    )
    result = build_raw_game_features(
        target,
        feature_history,
        mapping(),
        {"2019-09-05": era_record(as_of="2019-09-05")},
    )
    for side in ("away", "home"):
        self.assertEqual(
            result[side]["schedule"]["flags"],
            ["schedule_neutral_duplicate_gamepk_dependency"],
        )
```

This body proves the schedule-anchor fixture, game evidence, history projection,
normalized schema/replay, preparation, immutable index, and two 566156 team
hits. Task 8 already proves the third hit, known-neighbor negatives, and caller
hash rejection.

- [ ] **Step 2: Add the paired old-run characterization**

Add:

```python
def test_task10_old_history_remains_five_key_and_empty_in_prepare(self):
    old_history = self._build()[0]
    self.assertEqual(
        set(old_history.payload),
        {
            "games",
            "grain",
            "schema_version",
            "source_game_sha256s",
            "source_snapshot_sha256",
        },
    )
    self.assertNotIn("schedule_neutralizations", old_history.payload)
    for game_row in self.games[0].payload["games"]:
        self.assertNotIn("raw_game_object_sha256", game_row)
        self.assertNotIn("normalized_anchor_sha256", game_row)
    schedule_build = ScheduleHistoryBuild(
        rows=(),
        coverage_start="2018-03-29",
        coverage_end="2018-10-01",
        coverage_complete=True,
        source_manifest_sha256="7" * 64,
        expected_game_pks=(),
    )
    feature_history = build_feature_history(
        StatcastData((), ()),
        {slot: 1 / 9 for slot in range(1, 10)},
        schedule_build,
    )
    self.assertEqual(
        feature_history.schedule_history.schedule_neutralizations,
        (),
    )
```

- [ ] **Step 3: Run the narrow integration suite**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_schedule_inventory \
  tests.test_v52_source_schedule \
  tests.test_v52_game_evidence_inventory \
  tests.test_v52_source_history \
  tests.test_v52_verify_history_snapshots \
  tests.test_v52_verify_sources \
  tests.test_prepare \
  tests.test_feature_engine \
  -v
```

Expected: PASS.

- [ ] **Step 4: Run static and policy-closure checks**

```bash
ruff check \
  mlb_backtest/v52_schedule_inventory.py \
  mlb_backtest/v52_sources/schedule.py \
  mlb_backtest/v52_game_evidence_inventory.py \
  mlb_backtest/v52_sources/history.py \
  mlb_backtest/v52_verify_sources.py \
  mlb_backtest/prepare.py \
  mlb_backtest/feature_engine.py \
  tests/test_v52_schedule_inventory.py \
  tests/test_v52_source_schedule.py \
  tests/test_v52_game_evidence_inventory.py \
  tests/test_v52_source_history.py \
  tests/test_v52_verify_history_snapshots.py \
  tests/test_v52_verify_sources.py \
  tests/test_prepare.py \
  tests/test_feature_engine.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_execution_registry -v
git diff --check
```

Expected: PASS and no execution-code closure drift.

- [ ] **Step 5: Verify protected evidence and commit the integration test**

Before staging:

```bash
git status --short
git diff --name-only
```

The protected untracked historical/experimental directories and duplicate
files must remain unstaged. Stage only the three integration test files:

```bash
git add \
  tests/test_v52_verify_history_snapshots.py \
  tests/test_prepare.py \
  tests/test_feature_engine.py
git commit -m "test: prove schedule neutralization end to end"
```

## Final acceptance

Before declaring Task 10 complete:

1. `schedule_neutralizations` contains exactly the three approved records.
2. 565535 and 566157 remain ordinary for every team.
3. Old-run canonical bytes and ordinary feature behavior are unchanged.
4. Partial source inventory rows remain acquisition-valid but cannot reach history.
5. The history reducer performs no filesystem or network access.
6. The feature path reads authorization only from the validated history index.
7. Target team IDs exactly match the immutable verified schedule
   `(game_pk, official_date, team_code) -> team_id` binding.
8. No weight, score, calibration, or probability file changes.
9. All focused modules, execution-code closure, Ruff, and `git diff --check` pass.
10. Protected historical evidence remains untouched and unstaged.
