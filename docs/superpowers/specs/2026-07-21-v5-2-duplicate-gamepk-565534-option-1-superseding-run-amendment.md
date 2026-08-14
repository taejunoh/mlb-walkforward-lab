# MLB v5.2 Duplicate `gamePk` 565534 — Option 1 Superseding-Run Amendment

**Status:** implementation-ready after independent review; no production
evidence is changed by this document.

**Decision:** Use Option 1: start a new, isolated v5.2 historical run whose
schedule parser has an explicit, narrow resumed-game disposition rule. The
existing `historical_walkforward_500` run, its query batch, its two phase
receipts, first checkpoint, query registration, failed attempt, quarantine
receipt, and raw body remain immutable forensic evidence. It is never resumed,
reclassified, overwritten, or deleted.

## 1. Proven facts and scope

The current static 2019 schedule response is semantically invalid because it
contains two non-identical regular-season records for the same `gamePk`.
Current `_raw_schedule_metadata` rejects every duplicate game ID before the
existing `parse_schedule` ordinary reconciliation can complete. Independently,
`parse_schedule` rejects this particular terminal pair because its fields
differ by more than the start time. Both fail-closed results are correct for
the old run.

The only approved exception is the single raw-response pair below. It is a
data-specific disposition, not a general "last record wins" rule and not a
change to any v5.1 evidence.

| Item | Authoritative value |
|---|---|
| old logical query ID | `11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8` |
| old query registration path / SHA-256 | `data/v5_2_qd/historical/raw/queries/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/query.json` / `5071511a96867ff6e16979d310de8ca4c597775dfda4cdad89906278400eaf95` |
| old attempted URL | `https://statsapi.mlb.com/api/v1/schedule?endDate=2019-09-29&gameType=R&hydrate=probablePitcher%2Clineups%2Cvenue%2Clinescore&sportId=1&startDate=2019-03-20` |
| old attempt receipt path / SHA-256 | `data/v5_2_qd/historical/raw/queries/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/attempts/000001.json` / `d88722e680d30cd5bb78ef36d62adde15a20e91e6c1a0c132028a2b0aa746fce` |
| immutable quarantine receipt path / SHA-256 | `data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/000001.json` / `a2f26dd297fc2f0de084c857e6deb575973f147e8c9712a724d82308a9a693f8` |
| immutable quarantined raw path / SHA-256 / bytes | `data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0.raw` / `6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0` / `21182105` |
| first occurrence | `date_index=54`, `game_index=5`, canonical-object SHA-256 `5d2fc4945a5e562bb6676d8af0d1272dec29c946c293925a938655d15a000941` |
| first occurrence facts | `gamePk=565534`, `officialDate=2019-05-19`, `gameDate=2019-05-19T17:10:00Z`, `resumeDate=2019-09-07T00:15:00Z`, `resumeGameDate=2019-09-06`, venue `2394` (Comerica Park), `calendarEventID=14-565534-2019-05-19` |
| superseding occurrence | `date_index=161`, `game_index=12`, canonical-object SHA-256 `6b07d5e4781c472ca1e0a22b7190acdef61c8fe3d8885524835d6f6c876a9dd6` |
| superseding occurrence facts | `gamePk=565534`, `officialDate=2019-05-19`, `gameDate=2019-09-07T00:15:00Z`, `resumedFrom=2019-05-19T17:10:00Z`, `resumedFromDate=2019-05-19`, venue `10` (Oakland Coliseum), `calendarEventID=14-565534-2019-09-06` |
| old static batch / artifact SHA-256 | `069fd746f9c7ecb1267b7ae44f8d44a1aeb3575a92c64482dacbc5b61d8f0571` / `a6a2ac816620797b21e5bcf297fb92453f1143ed0d3d4c01effbf15c531ca39a` |
| old checkpoint / SHA-256 | `audit/v5_2_qd/historical/execution/acquisition_checkpoints/00000000-00b8b6eae2c0ba8de17f6ed1c05f0c9ddfb98e967e9b7ef0284581cc98b5ad06.json` / `00b8b6eae2c0ba8de17f6ed1c05f0c9ddfb98e967e9b7ef0284581cc98b5ad06` |
| old contract / SHA-256 | `protocols/v5_2_qd/historical_source_acquisition.json` / `cef89a1d4191ef83eb0fc2b0f4bdd191aeb8ba53cbb1bc6df73199604f23a9d9` |
| old RunSpec / SHA-256 | `protocols/v5_2_qd/historical_walkforward_500.json` / `d622bff184f0bdade22848d4b76fc43bdf9ac88c7a51513dee4a7d5f51bd1492` |

The old attempt was HTTP 200 but quarantined as `SEMANTIC_INVALID`; it is not
failed transport evidence. The new run must acquire its own query object and
must not convert the old one to success even if the remote response bytes are
identical.

## 2. New run identity and non-overlap

Create these new immutable authorities only after this corrected draft is
approved; do not edit either old authority file.

| New authority | Exact path / required identity |
|---|---|
| amendment design | this file |
| immutable implementation plan | `docs/superpowers/plans/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md` |
| SourceRunSpec | `protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json`, `run_id="historical_walkforward_500_superseding_565534"` |
| acquisition contract | `protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json` |
| raw namespace | `data/v5_2_qd/historical_superseding_565534/raw` |
| source namespace | `data/v5_2_qd/historical_superseding_565534/sources` |
| audit namespace | `audit/v5_2_qd/historical_superseding_565534` |
| report namespace | `reports/v5_2_qd/historical_superseding_565534` |

The current `RunSpec` parser is an exact literal parser for a 19-key model-run
object. It cannot safely represent a source-only run: its required namespaces
include `features`, `locked`, and `outcomes`, while its folds, OOF calibration,
selection, model-card paths, and outcome-isolation fields confer downstream
meaning that this amendment explicitly withholds. Do not pass a cosmetically
modified copy through that old literal branch.

Add a second closed authority type, `SourceRunSpec`, and make source-acquisition
entry points accept `RunSpec | SourceRunSpec` only where they already consume a
RunSpec. The old `RunSpec`, `RUN_REQUIRED_KEYS`, `NAMESPACE_KEYS`, parser,
validator, and typed equality remain unchanged. The new source-only JSON object
has exactly these top-level keys and values:

```json
{
  "schema_version": 1,
  "run_id": "historical_walkforward_500_superseding_565534",
  "model_version": "v5.2-QD",
  "label": "Historical source acquisition superseding duplicate gamePk 565534",
  "authority_scope": "SOURCE_ONLY_THROUGH_SOURCES_READY",
  "required_source_years": [2018, 2019, 2020, 2021, 2022, 2023, 2024],
  "forbidden_source_years": [2025],
  "source_requirements": {
    "minimum": ["mlb_schedule_game_feed", "baseball_savant_statcast", "mlb_stats_pitching", "venue_team_timezone_table", "prior_schedule_history"],
    "optional": ["environment", "defense", "catcher", "injury_restriction", "market_odds"]
  },
  "parser_policy": {
    "existing_source_parser_version": "v5.2.1",
    "schedule_duplicate_disposition": "SCHEDULE_DUPLICATE_GAMEPK_565534"
  },
  "namespaces": {
    "raw": "data/v5_2_qd/historical_superseding_565534/raw",
    "sources": "data/v5_2_qd/historical_superseding_565534/sources",
    "audit": "audit/v5_2_qd/historical_superseding_565534",
    "reports": "reports/v5_2_qd/historical_superseding_565534"
  },
  "downstream_authority": {
    "features": false,
    "locks": false,
    "outcomes": false,
    "oof": false,
    "model_cards": false,
    "targets": false,
    "selections": false,
    "reports_500": false
  }
}
```

`SourceRunSpec` exposes typed fields only for these exact keys. It has no fold,
target-count, date-state, terminal-model, feature, lock, outcome, OOF,
selection, card, or 500-result field. `parse_source_run_spec()` is an exact
literal parser for the object above; `load_source_run_spec()` is its file
loader. The source stack selects `load_run_spec()` only for the exact old path
and `load_source_run_spec()` only for the exact new path. No permissive
run-ID-based fallback exists.

Convert `schemas/v5_2_run_spec.schema.json` to an exact top-level `oneOf`:
branch one is the current old `const` plus its closed schema unchanged; branch
two is the exact SourceRunSpec object above with `additionalProperties=false`
at every object level and equality between `properties`, `required`, and the
literal's keys. This is a second schema branch, not a broadening of the old
namespace definition.

`protocols.write_historical_contract()` and
`verify_v52_design.verify_contract_schemas()` currently reconstruct the old
single-const schema and would overwrite or reject this branch. Extract one
`historical_authority_schema()` builder from the two literal oracles. The old
writer continues publishing the exact old protocol bytes plus the combined
schema; a new source-authority writer publishes the exact new protocol bytes
plus the same combined schema. The verifier compares the checked-in schema to
that combined builder and separately parses both authority files. Neither
writer rewrites the other protocol file.

The new contract retains parser version `v5.2.1` for every existing source and binds this design
and its new plan by working-tree SHA-256 (and the plan blob ID), and binds the
new SourceRunSpec. The raw-less duplicate disposition is a separate, closed logical
query rather than a source-parser-version change. Its `supersedes` object contains
only the old run ID, old RunSpec hash, old contract hash, old static-batch ID
and hash, old checkpoint hash, old query ID, old quarantine-receipt hash, and
old raw hash listed in §1. It is provenance, never a permission to read or
mutate those old paths.

The new contract has the old contract's 15 top-level keys plus the one exact
`supersedes` key. Its `bindings` has exactly `approved_design`,
`implementation_plan`, and `run_spec`; it does not retain the old
`plan_a_amendment` binding. `canonical_roots` has exactly `raw`, `sources`,
`acquisition_audit`, and `execution_audit`, all derived from the SourceRunSpec
(`audit/acquisition` and `audit/execution`). All other acquisition policy
values are copied exactly from the old contract except the
`mlb_schedule_game_feed.endpoints` array, which adds the exact
`disposition://SCHEDULE_DUPLICATE_GAMEPK_565534` endpoint after the two existing
HTTP endpoint templates without changing grain or parser version. Convert
`schemas/v5_2_acquisition_contract.schema.json` to an exact top-level `oneOf`:
the current old `const`/closed branch remains unchanged and the new branch is a
closed schema plus exact new `const`. `_verify_frozen_acquisition_schema()`
must select by authenticated contract path and require equality to one branch;
it may not accept an open common-property subset.

Refactor v5.2 path construction so the authenticated `RunSpec` or
`SourceRunSpec` namespace is the
sole source for raw, sources, acquisition audit, execution audit, source
reviews/replays, schedule inventory, game inventory, and checkpoints. Reject a
authority whose namespace roots overlap the old run or another run; reject an
symlink, hard link, non-directory, or an unapproved path. Existing default
commands retain the old canonical paths; new commands require the new SourceRunSpec
and derive all paths from it. No `--root`, `--run-spec`, `--contract`, or
environment value may cross-bind old and new paths.

The path refactor includes every source-stage owner with frozen old literals:
`execution_registry.py`, `v52_query_plan.py`, `v52_acquire.py`,
`v52_schedule_inventory.py`, `v52_legacy_decision_inventory.py`,
`v52_timezone_review.py`, `v52_game_evidence_inventory.py`,
`v52_verify_sources.py`, `v52_snapshot_store.py`,
`v52_source_orchestration.py`, and `v52_source_finalize.py`. The legacy
decision inventory remains a decision about protected v5.1 candidates, but its
new-run receipt/batch/output paths and authority bindings live only in the new
audit namespace. No source-stage module may retain a hidden
`historical_walkforward_500` or `audit/data/v5_2_qd/historical` assertion for
the new branch.

The old-chain heads remain exactly:

- `000000-DESIGN_ACCEPTED-ec9dca911b919f32f2012b8aaacb996b09e1c5e976cd00df7026502c6e07413a.json`, SHA-256 `ec9dca911b919f32f2012b8aaacb996b09e1c5e976cd00df7026502c6e07413a`;
- `000001-ACQUIRING-e889d76bf07e66b1cdfc1a65170e2a7aab83dad855cab535fa5483e5bbe55f2a.json`, SHA-256 `e889d76bf07e66b1cdfc1a65170e2a7aab83dad855cab535fa5483e5bbe55f2a`; and
- checkpoint sequence zero above, with all seven static IDs pending and no
  completed or quarantined IDs in that checkpoint.

## 3. Exact raw-less disposition query and selection contract

Create `mlb_backtest/v52_duplicate_gamepk_disposition.py`. It owns the single
closed logical query for this amendment, whose endpoint is exactly
`disposition://SCHEDULE_DUPLICATE_GAMEPK_565534`. The query is
`DERIVED_DISPOSITION`, has empty parameters, no parent, no raw object, and no
HTTP attempt, reservation, retry, or network gate. It is published only through
the existing `V52EvidenceStore.publish_derived_disposition` lifecycle while its
query lease is held.

The module constructs the query and its semantics from the authenticated
superseding SourceRunSpec/contract plus the immutable old authorities in §1.
The complete `LogicalQuery` has source `mlb_schedule_game_feed`, grain
`season_schedule_and_game`, endpoint
`disposition://SCHEDULE_DUPLICATE_GAMEPK_565534`, empty parameters,
`expected_media_type="application/json"`, parser version `v5.2.1`, and
`parent_query_id=None`. Its `run_spec_sha256` and
`acquisition_contract_sha256` are computed from the authenticated new files,
never accepted from the caller. Its closed `requested_range` is exactly:

```json
{
  "year": 2019,
  "start_date": "2019-03-20",
  "end_date": "2019-09-29",
  "disposition": "SCHEDULE_DUPLICATE_GAMEPK_565534",
  "game_pk": 565534,
  "old_authority": {
    "query_id": "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8",
    "query_registration": {
      "path": "data/v5_2_qd/historical/raw/queries/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/query.json",
      "sha256": "5071511a96867ff6e16979d310de8ca4c597775dfda4cdad89906278400eaf95"
    },
    "quarantine_receipt": {
      "path": "data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/000001.json",
      "sha256": "a2f26dd297fc2f0de084c857e6deb575973f147e8c9712a724d82308a9a693f8"
    },
    "raw": {
      "path": "data/v5_2_qd/historical/raw/quarantine/mlb_schedule_game_feed/11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8/6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0.raw",
      "sha256": "6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0",
      "byte_count": 21182105
    }
  },
  "suppressed_occurrence": {
    "date_index": 54,
    "game_index": 5,
    "sha256": "5d2fc4945a5e562bb6676d8af0d1272dec29c946c293925a938655d15a000941"
  },
  "retained_occurrence": {
    "date_index": 161,
    "game_index": 12,
    "sha256": "6b07d5e4781c472ca1e0a22b7190acdef61c8fe3d8885524835d6f6c876a9dd6"
  }
}
```

The sidecar semantic projection records the same authority, the one
`gamePk=565534` disposition, its fixed-field predicates, and its
retained/suppressed positions. No caller may supply an old path, hash, raw
bytes, occurrence, disposition value, or either new authority hash.

The generic evidence store is rooted at the new raw namespace and cannot infer
or authenticate the old repository authority. Add a frozen
`DuplicateGamePkDispositionAuthority` adapter constructed only from repository
root, the exact new SourceRunSpec path, and the exact new contract path. Its
constructor authenticates both new files, proves the store root equals the new
raw namespace, and snapshots the three old files. The only schedule-disposition
entry point is `publish_duplicate_gamepk_disposition(store, authority)`. The
adapter supplies the closed query and recomputed semantics to the existing
publisher. Immediately before `success.json`, the publisher invokes the
adapter again and requires the reopened/recomputed authority to equal the
initial snapshot. The exact schedule endpoint is rejected by generic
`publish_derived_disposition` when this adapter is absent or cross-bound;
preseason publication remains unchanged.

Concretely, preserve the existing two-positional-argument call and add a
keyword-only `authority=None` to `publish_derived_disposition()` and
`recompute_query_sidecar()`. For the schedule endpoint, both functions locally
import and require an actual `DuplicateGamePkDispositionAuthority`, require
`authority.query == query`, and call `authority.recompute()`. The publisher
additionally requires `authority.evidence_root == self.root` and repeats the
recompute in the final pre-success rebind. They require `raw == b""`. For the preseason endpoint
they require `authority is None`, preserving all existing calls. The wrapper
calls `store.publish_derived_disposition(query, semantics,
authority=authority)` while holding the existing query lease.

Before constructing or replaying the sidecar, the adapter opens the old query
registration, quarantine receipt, and raw body with the existing no-follow,
regular-single-link, identity, path, byte-count, and SHA-256 checks. It confirms
the receipt binds the raw body and the old logical query. It then parses the
raw body and permits collapse only when all conditions below hold:

1. exactly two `gameType="R"` objects share `gamePk=565534`;
2. both have season `2019`, official date `2019-05-19`, away team ID `133`,
   home team ID `116`, terminal `Final`, away score `7`, home score `3`, and
   consistent unique winner evidence;
3. the `date_index=54`, `game_index=5` object has SHA-256
   `5d2fc4945a5e562bb6676d8af0d1272dec29c946c293925a938655d15a000941`,
   `resumeDate="2019-09-07T00:15:00Z"`, and
   `resumeGameDate="2019-09-06"`; and
4. the `date_index=161`, `game_index=12` object has SHA-256
   `6b07d5e4781c472ca1e0a22b7190acdef61c8fe3d8885524835d6f6c876a9dd6`,
   `resumedFrom="2019-05-19T17:10:00Z"`, and
   `resumedFromDate="2019-05-19"`.

The disposition retains only the superseding object: official date remains
`2019-05-19`, scheduled start is `2019-09-07T00:15:00Z`, venue is `10`, and
teams, scores, terminal status, and winner are copied only after the equality
checks. It suppresses only the first object. It never merges fields, alters raw
bytes, fabricates a response, infers a generic resumed-game policy, or relaxes
ordinary duplicate reconciliation for another ID. One/three-or-more special
copies, a changed field, missing resume linkage, wrong fingerprint, or any
duplicate that the unchanged ordinary parser rejects fails closed.

`source_parsers/schedule.py` remains unchanged. The authenticated response has
2,472 regular occurrences, 2,430 unique `gamePk` values, and 42 duplicate IDs,
each with one extra occurrence. Existing `parse_schedule` reconciles 41 pairs
under its ordinary rules; `_raw_schedule_metadata` fails earlier because it
incorrectly compares the raw occurrence total with the unique population.
Suppressing the first `565534` object is therefore necessary but insufficient
if the old metadata routine is reused.

Add `project_schedule_occurrences()` to the new module. It must:

1. authenticate untouched root/date totals of 2,472, every date total against
   its raw list length, 2,472 regular occurrences, 2,430 unique IDs, 42
   duplicate IDs, and one extra occurrence per duplicate;
2. validate the complete special pair and suppress only `(54, 5)`, leaving
   2,471 projected occurrences;
3. run unchanged `parse_schedule` over an in-memory view, where 41 ordinary
   duplicate pairs reconcile to exactly 2,430 normalized games;
4. mirror the existing reconciliation choice to select the raw occurrence for
   each normalized game and require its normalized population to equal
   `parse_schedule` in canonical JSON; and
5. return an immutable projection of raw counts, duplicate counts,
   suppressed/retained positions, normalized games, selected raw-object
   hashes, and site flags.

It never serializes replacement raw bytes, changes raw totals, or calls
`_raw_schedule_metadata` on a filtered document. The schedule inventory consumes
this authenticated projection directly.

## 4. Existing derived-disposition publication and schema branch

Do not create a fetched-success-attached duplicate sidecar, a new
`v5_2_schedule_duplicate_disposition` schema, or a raw/sidecar transactional
bundle. Extend the existing
`schemas/v5_2_derived_disposition_sidecar.schema.json` into two closed `oneOf`
branches: preserve the current preseason-pitching branch byte-for-byte in
meaning, and add the schedule-duplicate branch described in §3. Both branches
retain the existing envelope keys only:
`grain`, `parser_version`, `query_id`, `schema_version`, `semantics`, `source`,
and `storage_mode="DERIVED_DISPOSITION"`.

The new branch's `semantics` has exactly these keys:
`disposition`, `game_pk`, `old_authority`, `fixed_game_facts`,
`suppressed_occurrence`, `retained_occurrence`, `occurrence_counts`,
`normalized_games_sha256`, and
`selected_raw_game_object_sha256s_sha256`. `old_authority` is byte-for-byte the
closed query subobject in §3. `fixed_game_facts` has exactly `season=2019`,
`official_date="2019-05-19"`, `away_team_id=133`, `home_team_id=116`,
`terminal_status="Final"`, `away_score=7`, `home_score=3`, and
`winner_team_id=133`. Each occurrence record has exactly `date_index`,
`game_index`, `sha256`, `game_date`, `venue_id`, `calendar_event_id`, and the
two linkage fields applicable to that occurrence (`resume_date` plus
`resume_game_date`, or `resumed_from` plus `resumed_from_date`).
`occurrence_counts` is the exact nine-key object from §5.
`normalized_games_sha256` hashes the canonical list returned by unchanged
`parse_schedule` in stable first-seen order;
`selected_raw_game_object_sha256s_sha256` hashes a canonical array of exact-key
`{"game_pk": ..., "raw_game_object_sha256": ...}` rows sorted by numeric
`game_pk`. Values are recomputed from
the old quarantined raw
evidence, not accepted from the publication caller. The schema and
`v52_duplicate_gamepk_disposition.py` must reject unknown keys, a missing old
quarantine/raw/registration binding, a cross-run query ID, or a noncanonical
digest/path.

Extend the existing derived-disposition dispatch in `v52_evidence.py` and
`v52_source_contracts.py` so this second branch is accepted only for that exact
logical query, authenticated adapter, and replayed semantics.
`publish_duplicate_gamepk_disposition()` owns adapter construction and the
lease-scoped call into `publish_derived_disposition`; callers never invoke the
generic method directly for this endpoint. `publish_derived_disposition` already
requires an empty `attempts/` inventory and an empty `objects/` inventory,
writes the canonical sidecar through its no-follow/O_EXCL/single-link checks,
and writes `success.json` only after its final rebind. Reuse those semantics;
do not add an HTTP attempt, raw object, fetched success receipt, or alternate
publication transaction.

## 5. Execution registry, checkpoint, and inventory gates

`mlb_backtest/execution_registry.py` creates a fresh `DESIGN_ACCEPTED →
ACQUIRING` chain in the new audit root. The new phase receipts bind the new
SourceRunSpec/contract/design/plan/inventory and code closure. Their `supersedes`
linkage verifies the immutable values in §1 but does not join or alter the old
chain.

Define `SOURCE_ONLY_PHASES = ("DESIGN_ACCEPTED", "ACQUIRING",
"SOURCES_READY")`. Registry path construction, receipt parsing, load,
transition, idempotent retry, and quarantine-transition validation select
`PHASES` only for the old RunSpec and `SOURCE_ONLY_PHASES` only for the
SourceRunSpec. A new-run receipt with sequence greater than 2 or phase
`OOF_SEALED`, `CARDS_SEALED`, `TARGET_LOCKED`, `OUTCOMES_JOINED`,
`SAMPLE_500_SEALED`, or `VERIFIED` is invalid even if its hashes are otherwise
well formed. From new-run `SOURCES_READY`, every advance or quarantine attempt
fails before snapshotting or writing. Omitted downstream namespaces are not
the sole phase guard.

Extend the acquisition checkpoint schema, parser, semantic comparison, and
writer with an exact new checkpoint branch requiring sorted, duplicate-free
`dispositioned_query_ids`. Append
`dispositioned_query_ids: tuple[str, ...] = ()` as the final
`AcquisitionCheckpoint` dataclass field so existing constructors remain
compatible. The old parser accepts only the old key set and returns the empty
tuple without changing bytes. The old writer uses its existing default and
omits the JSON field. The new writer uses an internal unset sentinel: a new-run
call must explicitly provide the list (including `[]`), while an old-run call
that explicitly supplies it is rejected. Semantic projection and progression
compare branch-specific key sets; they never normalize an old payload by
injecting the new field.

The immutable existing checkpoint schema/receipts remain readable as the
old-run branch and are never rewritten. Every new
superseding-run checkpoint uses the new branch, records the one derived
logical-query ID there, and binds its existing `success.json` receipt/sidecar
through the normal `query_evidence_receipts` list. The derived ID is also a
completed query ID; it may not be pending, quarantined, or split. Conversely,
a query in `dispositioned_query_ids` must authenticate as
`DERIVED_DISPOSITION` with the new schedule-duplicate schema branch. The field
is required, including as an explicit empty list in a new non-disposition
checkpoint; missing it is never treated as an implicit empty list.

No superseding checkpoint may list the old
`11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8` as
pending, completed, quarantined, split, or dispositioned. The immutable old
query is provenance read by the derived query only. It is never registered,
published, or moved in the new raw root.

The superseding static batch contains exactly seven ordered season entries:
ordinary fetched/protected schedule queries for 2018 and 2020–2024, and the
closed derived query from §3 in the 2019 slot. The old quarantined query ID is
not a batch member.

The 2019 `season_sources` item is a separate closed schema branch. It retains
the existing season-source keys, has `season=2019`,
`storage_mode="DERIVED_DISPOSITION"`, and `raw_sha256=null`, and binds the new
query ID, sidecar, and success receipt. The old raw hash exists only within the
sidecar's old-authority provenance. This branch additionally requires:

```json
{
  "occurrence_counts": {
    "raw_total_games": 2472,
    "raw_date_total_games": 2472,
    "raw_regular_occurrences": 2472,
    "raw_unique_game_pks": 2430,
    "raw_duplicate_game_pks": 42,
    "special_suppressed_occurrences": 1,
    "projected_occurrences": 2471,
    "ordinary_reconciled_duplicate_game_pks": 41,
    "normalized_unique_games": 2430
  }
}
```

The six ordinary entries keep the existing closed non-null `raw_sha256` branch;
do not make the old schema broadly nullable. `v52_schedule_inventory.py`
authenticates the derived sidecar and projects the old 2019 raw population
before schedule-reference planning. It produces exactly one `565534` anchor
and binds the derived query receipt/sidecar as its authority instead of
requiring a fetched 2019 schedule success. Every normalized anchor carries the
selected occurrence's `raw_game_object_sha256`. The retained anchor alone
may create boxscore, venue, timezone, history, D-1-pitching, and career-BF
consumer edges; the suppressed record creates none. `v52_query_plan.py`,
`v52_game_evidence_inventory.py`, `v52_verify_sources.py`,
`v52_snapshot_store.py`, `v52_source_orchestration.py`, and
`v52_source_finalize.py` carry that derived binding forward and fail closed if
the sidecar, old evidence rebind, filtered population, or checkpoint
`dispositioned_query_ids` differs.

At every read, re-open the old authority and new sidecar through no-follow
descriptors; verify device/inode/link count, canonical bytes, and SHA-256;
recompute the selection; then rebind immediately before commit. Add the new
module and the existing amended schema to the exact execution closure; closure
tests require equality, not a superset.

## 6. The three bounded downstream neutralizations

These are the only exception-specific downstream neutralizations. They are
not inferred from a date/venue heuristic and do not apply to an adjacent game,
another team, another season, or a later rerun unless the exact schedule
disposition sidecar above is authenticated.

The values originally labeled `target_anchor_sha256` are canonical hashes of
selected raw MLB game objects, not normalized anchor hashes. Their corrected
field name is `target_raw_game_object_sha256`. Before adding that provenance
field, the current normalized anchor hashes are
`21ec843ae95fadd841b443f9e5ee55d752f9f96cc1923bfc432b855a110fcfa4`
for `565338` and
`737119d140f41cde1c5273b94edd69e793a77b5ccf42dcaa59913a17a67ef1a4`
for `566156`. After adding the exact `raw_game_object_sha256` field, the new
normalized anchor hashes are respectively
`5224b908bc7a340ac057bb9f56efa1e9ebdf3ac64972191611e0e0b802410c96`
and `3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29`.
Tests bind both the before-schema comparison fixtures and the final hashes so a
projection-field change cannot silently redefine either value.

For each listed `(game_pk, official_date, team_id,
target_raw_game_object_sha256)`,
the schedule-dependent value is exactly:

```json
{
  "value": 50.0,
  "sample_size": 0,
  "quality": 0.0,
  "reason_codes": ["schedule_neutral_duplicate_gamepk_dependency"]
}
```

| target `gamePk` | official date | team ID | target raw game-object SHA-256 | target normalized anchor SHA-256 |
|---:|---|---:|---|---|
| 565338 | 2019-05-20 | 133 (OAK) | `92900ec47f924a1a68d533d63ae068faabd42262b808461a543974b61f75d5ef` | `5224b908bc7a340ac057bb9f56efa1e9ebdf3ac64972191611e0e0b802410c96` |
| 566156 | 2019-09-06 | 133 (OAK) | `35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab` | `3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29` |
| 566156 | 2019-09-06 | 116 (DET) | `35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab` | `3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29` |

Add an exact-key `schedule_neutralizations` array to the relevant normalized
history/schedule-dependent projection. Every row has exactly `game_pk`,
`official_date`, `team_id`, `target_raw_game_object_sha256`,
`target_normalized_anchor_sha256`, `value`, `sample_size`, `quality`, and
`reason_codes`; rows sort by raw-object hash, then `game_pk`, then team ID. The
producer verifies every target exists exactly once, carries the stated raw
provenance, and hashes to its recomputed normalized value.
It rejects duplicate rows, changed reason codes, a nonzero sample/quality,
missing `50.0`, or a request to neutralize an unlisted target. The consumer
must preserve the frozen missing-weight/no-redistribution policy.

There is explicitly **no** neutralization for `565535` or `566157`. Their full
raw-object hashes are locally computable:
`11afba7dcd8ec0de2b78f8773e2c49c8f23a7d104e9b1a2b58d9a9f5893ff019`
and `db8427682b066341a9f204dd2832ff418d93946b1ff12e07b147e7e60398ca6d`.
No reviewed policy approves either row, so availability is not authorization.
Tests assert their absence with the complete hashes; truncated prefixes are
never code or schema literals.

## 7. File-level implementation order

1. Create the new SourceRunSpec and acquisition contract plus schema tests in
   `tests/test_v52_acquisition_contract.py` and `tests/test_protocols.py`.
   Add namespace-disjointness, old-artifact immutability, contract/plan/design
   binding, new parser-version tests, combined schema-writer tests, and
   `verify_v52_design.py` parity tests before implementation.
2. Refactor only the path factories in `execution_registry.py`,
   `v52_query_plan.py`, `v52_acquire.py`, `v52_schedule_inventory.py`,
   `v52_legacy_decision_inventory.py`, `v52_timezone_review.py`,
   `v52_game_evidence_inventory.py`, `v52_verify_sources.py`,
   `v52_snapshot_store.py`, `v52_source_orchestration.py`, and
   `v52_source_finalize.py` so authenticated authority namespaces flow through
   every artifact path. Add the explicit source-only registry phase guard.
   Source-only entry points accept the explicit
   `RunSpec | SourceRunSpec` authority union; keep old-path behavior
   byte-for-byte unchanged.
3. Create `v52_duplicate_gamepk_disposition.py`, add the second closed branch
   to the existing `v5_2_derived_disposition_sidecar` schema, and extend the
   existing derived-disposition dispatch in `v52_evidence.py` and
   `v52_source_contracts.py`. The authority-aware wrapper must use the existing
   `publish_derived_disposition` transaction, have neither raw bytes nor
   attempts, and retain the strict general schedule parser.
4. Add `dispositioned_query_ids` to checkpoint schema/registry validation and
   wire the authenticated derived binding into schedule-anchor inventory,
   schedule-reference batches, game-evidence inventory, snapshot replay, source
   inventory, final review/replay, and `SOURCES_READY`. Update
   `_BOUND_EXECUTION_CODE` and its exact-closure tests with the new module and
   amended existing schema.
5. Implement the narrowly enumerated neutralization projection in the existing
   schedule-history consumer, not in model constants or score calibration. The
   three literal anchors above are the complete allowlist.
6. After this corrected spec passes independent review, write the new
   implementation plan, then execute it only under the new SourceRunSpec. The
   2019 duplicate disposition is network-free; any remaining
   acquisition is last and is gated by the clean test suite plus an explicit
   operator invocation.

## 8. Required tests and acceptance gates

Add focused tests (names may be exact as listed) in
`tests/test_v52_source_schedule.py`, `tests/test_v52_evidence.py`,
`tests/test_v52_schedule_inventory.py`, `tests/test_v52_game_evidence_inventory.py`,
`tests/test_v52_query_plan.py`, `tests/test_v52_verify_sources.py`,
`tests/test_execution_registry.py`, `tests/test_v52_source_finalize.py`, and
the history/schedule consumer test module:

- `test_565534_exact_pair_retains_only_resumed_record_and_binds_disposition`;
- `test_565534_requires_both_complete_fingerprints_and_all_fixed_fields`;
- `test_nonordinary_duplicate_gamepk_or_third_special_copy_fails_closed`;
- `test_2019_projection_authenticates_2472_occurrences_42_duplicates_and_2430_unique`;
- `test_2019_schedule_source_is_closed_derived_branch_with_null_new_raw_sha`;
- `test_source_run_spec_is_a_closed_source_only_authority_not_old_run_spec`;
- `test_old_2019_query_quarantine_and_raw_are_immutable_and_read_only_disposition_authority`;
- `test_duplicate_disposition_is_rawless_and_has_no_attempt_or_reservation`;
- `test_publish_derived_disposition_rejects_nonempty_raw_or_attempt_inventory`;
- `test_disposition_sidecar_tamper_cross_run_binding_and_rebind_fail_closed`;
- `test_schedule_disposition_requires_authenticated_old_root_adapter_at_final_rebind`;
- `test_checkpoint_requires_exact_dispositioned_query_ids`;
- `test_old_checkpoint_bytes_parse_to_trailing_empty_tuple_and_old_writer_omits_field`;
- `test_new_checkpoint_never_contains_old_query_id_or_old_artifact_binding`;
- `test_source_run_registry_stops_at_sources_ready_and_rejects_later_phase_receipts`;
- `test_schedule_inventory_contains_one_565534_and_only_retained_children`;
- `test_three_exact_duplicate_dependency_neutralizations_and_no_others`;
- `test_565535_and_566157_complete_hashes_are_known_but_unapproved`;
- `test_replay_inventory_and_sources_ready_require_disposition_binding`; and
- `test_old_default_run_paths_and_receipts_remain_byte_identical`.

Use a fixture containing the exact two objects plus ordinary games and a
negative fixture for each failed predicate. Fixtures must carry the two object
fingerprints and use only local bytes; they are not production evidence. A
separate read-only integration fixture over the immutable old body asserts all
2,472/2,430/42/41 counts and never copies or rewrites that body. The
derived-publication tests use the existing `publish_derived_disposition` failure
hooks and assert that no raw object, attempt receipt, or reservation becomes
visible and that `success.json` is absent until its existing final rebind passes.

Before any network invocation run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_source_schedule tests.test_v52_evidence \
  tests.test_v52_schedule_inventory tests.test_v52_game_evidence_inventory \
  tests.test_v52_query_plan tests.test_v52_verify_sources \
  tests.test_execution_registry tests.test_v52_source_finalize -v
ruff check mlb_backtest tests
git diff --check
```

The rollout operator then runs `--plan-only` with the new SourceRunSpec, validates a
new `DESIGN_ACCEPTED` receipt and its new static batch, and starts acquisition
only after all hashes and roots are reported as new. Stop after
`REFERENCE_INPUTS`; independently inspect the one `565534` anchor and the
three neutralization records; then continue through the existing timezone,
participant, remaining, offline replay, review, and `SOURCES_READY` gates.
No output from the superseding run may replace an old canonical file, be called
a migration, or authorize fitting/OOF/cards/targets/outcomes/500 reporting.

## 9. Downstream-plan amendment requirements

The source-finalization design and plan
(`2026-07-19-mlb-v5-2-source-finalization-cli-design.md` and
`2026-07-20-mlb-v5-2-source-finalization-cli.md`) must consume the new
namespace-parametric authority context and require the disposition sidecar when
the new contract declares this policy. Their old-run behavior and receipt
selection semantics remain unchanged.

The diagnostic/500-pick design and plan
(`2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md` and
`2026-07-12-mlb-v5-2-diagnostic-and-500-pick.md`) must add a preflight rule:
only a `SOURCES_READY` source inventory bound to this superseding run may be
considered for any later v5.2 continuation; the old run remains blocked. Any
schedule-derived consumer must carry the exact neutralization projection.

No Plan B/C artifact is created in this amendment. A later plan must explicitly
choose whether it consumes the superseding run; it may not silently select a
"latest" registry, inventory, review, replay, or source root.

## 10. Residual risks and explicit non-assumptions

- The derived 2019 disposition performs no network request and expects no new
  2019 schedule response. It is based only on authenticated immutable old
  quarantine evidence; that evidence remains quarantine evidence in the old
  run and is never promoted there.
- The old local registry's documented tail-deletion limitation is unchanged.
  The new superseding chain adds provenance, not an external anti-rollback
  anchor.
- The complete raw-object SHA-256 values for intentionally non-neutralized
  `565535` and `566157` are known, but the policy decision to neutralize them is
  absent. This amendment deliberately rejects them.
- This amendment does not define any fourth neutralization, a generic suspended
  game heuristic, a changed model weight, or a result/selection outcome.
