# MLB v5.2 Schedule-Conflict Recovery Run Design

**Status:** approved single recovery-run design.  This document authorizes a
source-only recovery through `SOURCES_READY`; it does not authorize fitting or
any downstream Plan B/C work.

## Decision and scope

Create one new, namespace-isolated source authority:

| Authority | Exact value |
|---|---|
| `run_id` | `historical_walkforward_500_recovery_schedule_conflicts` |
| SourceRunSpec | `protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json` |
| acquisition contract | `protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json` |
| raw root | `data/v5_2_qd/historical_recovery_schedule_conflicts/raw` |
| source root | `data/v5_2_qd/historical_recovery_schedule_conflicts/sources` |
| audit root | `audit/v5_2_qd/historical_recovery_schedule_conflicts` |
| report root | `reports/v5_2_qd/historical_recovery_schedule_conflicts` |
| schedule parser version | `v5.2.2` |

The roots must be pairwise lexically disjoint from, and must not be a symlink,
hard link, ancestor, or descendant of, either immutable evidence tree:

- `data/v5_2_qd/historical/` and `audit/v5_2_qd/historical/`; and
- `data/v5_2_qd/historical_superseding_565534/` and
  `audit/v5_2_qd/historical_superseding_565534/`.

This is within the source-only Plan A scope in
`2026-07-14-mlb-v5-2-plan-a-contract-amendment.md`: the only allowed registry
phases are `DESIGN_ACCEPTED`, `ACQUIRING`, and `SOURCES_READY`.  Features,
locks, outcomes, OOF, cards, targets, selections, 500 results, and historical
reports are forbidden.  The 2026-07-21 amendment authorizes only its named
superseding authority; this document is the additional approval for the new
authority above.

## Why a new run is mandatory

The prior superseding run is append-only forensic evidence, not a resumable
recovery target.  Its authenticated state is:

| Item | SHA-256 / identity |
|---|---|
| RunSpec | `88a4253d76b0f73694fbdc5ddbe64217c37ae379d7ee80931a64ab7742014850` |
| contract | `3ddf83fdec9a328f5005fe8f62f4f680f4ef81516acd1aef2f90845b11c11d58` |
| DESIGN_ACCEPTED receipt | `3dc364c75f9aa0e2bcc131fd144eab77c7f31ef589c0125e5a270bd91f3e3e57` |
| ACQUIRING receipt | `05d1259c7cddadb06fbc162ce36c26487e5d122e10d999e3d5105e4be038efa7` |
| STATIC_SCHEDULES batch ID / artifact | `d4d07954d69d91d7f721aee9554e83b925c8d3c3cd60f7dc7efeba9f5ed5c661` / `0f84cee97be9764d9f5b1ec349352520f4dc066573daf0d2b38cf6b2c8479c9a` |
| terminal checkpoint | `4ad9e740c7de1e7878a63bb5f763f06028249bb414fb91ddad98286b0cf8d352` |

That checkpoint is `STATIC_SCHEDULES`, has one completed/dispositioned query
`8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095`,
six quarantined HTTP queries, and no pending queries.  Its receipts are bound
to Git head `baff610d72627585a9564ef40adaa81189b58376`; the approved audit
head is `7eab3a6c2d6cbffb6b432bdce65cafda7eadeab7`.  The execution registry
requires receipt/checkpoint Git snapshots to match the current complete
execution-code snapshot.  A corrected implementation therefore cannot append
to that chain without invalidating its trust binding.

Neither old tree may be edited, deleted, reclassified, promoted, copied into a
new object slot, or used as a hard-linked success.  A recovery adapter may read
the listed evidence only as immutable provenance after no-follow, regular-file,
single-link, device/inode, byte-count, canonical-byte, and SHA-256 checks.

## Parser boundary

The v5.2.2 change is narrowly limited to ordinary duplicate accounting:
`totalGames` and each date total continue to bind the raw occurrence
population, while the normalized `parse_schedule` population is checked
separately.  Existing ordinary reconciliation remains authoritative.

The change must not add a generic resumed-game, venue-conflict, date-conflict,
or “last record wins” heuristic.  In particular, the raw HTTP parser continues
to reject the terminal conflicts for `630882` and `633224`; only the three
closed dispositions below may select an occurrence.  Changing parser semantics
requires the v5.2.2 contract value for every recovery schedule query, including
derived queries.  Existing v5.2.1 authorities, queries, sidecars, and receipts
remain unchanged.

`v5.2.2` is limited to `mlb_schedule_game_feed` recovery LogicalQueries: the
four recovery HTTP schedule roots and the three recovery schedule dispositions.
The other five recovery source contracts and every LogicalQuery they produce
remain parser version `v5.2.1`. A recovery batch with v5.2.2 on a non-schedule
source, v5.2.1 on a recovery schedule query, or either version substituted into
an older authority is rejected before query registration.

The raw audit establishes that 2018, 2022, 2023, and 2024 safely pass the
ordinary reconciliation once raw-versus-normalized counting is separated.
2020 `630882` and 2021 `633224` do not: each has a real terminal venue
conflict.  They are not candidates for a broad parser relaxation.

## Closed static batch

The new STATIC_SCHEDULES batch has exactly seven ordered season entries:

| Season | Storage | Authority |
|---:|---|---|
| 2018 | HTTP | a new recovery logical query and new recovery raw/attempt/success evidence |
| 2019 | `DERIVED_DISPOSITION` | exact `565534` authority below |
| 2020 | `DERIVED_DISPOSITION` | exact `630882` authority below |
| 2021 | `DERIVED_DISPOSITION` | exact `633224` authority below |
| 2022 | HTTP | a new recovery logical query and new recovery raw/attempt/success evidence |
| 2023 | HTTP | a new recovery logical query and new recovery raw/attempt/success evidence |
| 2024 | HTTP | a new recovery logical query and new recovery raw/attempt/success evidence |

The four HTTP queries use the existing schedule endpoint and their season's
closed dates/parameters, but have new query IDs because the recovery RunSpec,
contract, and parser version differ.  Even byte-identical remote responses are
new regular files in the recovery raw root with new registrations, attempts,
sidecars, and successes.  The six earlier quarantined IDs are never entered in
any recovery batch, checkpoint population, or source inventory.

The contract endpoint list adds exactly these three names in addition to the
ordinary schedule endpoint:

```text
disposition://SCHEDULE_DUPLICATE_GAMEPK_565534
disposition://SCHEDULE_DUPLICATE_GAMEPK_630882
disposition://SCHEDULE_DUPLICATE_GAMEPK_633224
```

Each derived query is raw-less: it has empty parameters, no HTTP attempt,
reservation, retry, object, or network gate.  It is published only via a
lease-held, authority-aware `DERIVED_DISPOSITION` transaction and must re-open
and recompute its parent provenance immediately before `success.json`.

The validator treats this table as an endpoint/season allowlist, not merely a
count: its ordered endpoint sequence is HTTP schedule (2018), disposition
`565534` (2019), disposition `630882` (2020), disposition `633224` (2021),
then HTTP schedule (2022, 2023, 2024). It rejects HTTP schedule queries for
2019--2021, a disposition in 2018/2022/2023/2024, any reordering, and any
additional endpoint even if the batch still has seven entries.

## Exact disposition catalog

The recovery implementation owns a closed three-entry catalog.  A caller may
not supply a parent path, hash, gamePk, index, retained object, suppressed
object, parser version, or disposition value.  Each entry verifies the complete
raw response population and all stated records before creating an in-memory
projection; it never serializes replacement raw JSON.

### 2019 — gamePk 565534

This entry retains the already-approved original-run provenance from the
2026-07-21 amendment:

- parent query `11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8`;
- registration `5071511a96867ff6e16979d310de8ca4c597775dfda4cdad89906278400eaf95`;
- quarantine receipt `a2f26dd297fc2f0de084c857e6deb575973f147e8c9712a724d82308a9a693f8`;
- raw `6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0`,
  21,182,105 bytes;
- suppress `(date_index=54, game_index=5)`,
  `5d2fc4945a5e562bb6676d8af0d1272dec29c946c293925a938655d15a000941`;
- retain `(date_index=161, game_index=12)`,
  `6b07d5e4781c472ca1e0a22b7190acdef61c8fe3d8885524835d6f6c876a9dd6`.

Both records must be 2019 regular-season `565534`, official date `2019-05-19`,
away `133`, home `116`, Final, 7–3 with unique winner `133`.  The suppressed
record has start `2019-05-19T17:10:00Z`, venue `2394`,
`resumeDate=2019-09-07T00:15:00Z`, and `resumeGameDate=2019-09-06`; the
retained record has start `2019-09-07T00:15:00Z`, venue `10`,
`resumedFrom=2019-05-19T17:10:00Z`, and `resumedFromDate=2019-05-19`.
The source response must have 2,472 regular occurrences, 2,430 unique IDs, 42
duplicate IDs, one suppressed occurrence, 2,471 projected occurrences, 41
ordinary reconciliations, and 2,430 normalized games.

### 2020 — gamePk 630882

The parent is the failed superseding 2020 HTTP query.  Its closed binding is:

```text
query ID:            8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816
registration SHA-256: e5af77c4750d9de564d6a7203564b30056a89627f70c868e7ff0a1753d674106
attempt SHA-256:      fe37c66d26cc435d1375432b4f31c4b21546f28e46648de6379eeb67b1c527e2
quarantine SHA-256:   47bd466897b87b23a3c625737c0cae5aad1475491fdee93454489df9c38752cc
raw SHA-256 / bytes:  b085d068a09a4101a77a0f3c2fabe6b159fd8c9df564ee31994aa5d10271fb1b / 7898917
```

The parent artifacts are exactly:

- `data/v5_2_qd/historical_superseding_565534/raw/queries/mlb_schedule_game_feed/8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816/query.json`,
  675 bytes, whose `parent_query_id` is JSON `null`;
- `data/v5_2_qd/historical_superseding_565534/raw/queries/mlb_schedule_game_feed/8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816/attempts/000001.json`,
  662 bytes;
- `data/v5_2_qd/historical_superseding_565534/raw/quarantine/mlb_schedule_game_feed/8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816/000001.json`,
  936 bytes; and
- `data/v5_2_qd/historical_superseding_565534/raw/quarantine/mlb_schedule_game_feed/8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816/b085d068a09a4101a77a0f3c2fabe6b159fd8c9df564ee31994aa5d10271fb1b.raw`,
  7,898,917 bytes.

All four path, SHA-256, byte-count, query-ID, and `parent_query_id` bindings
are part of the disposition's closed old-authority object; a matching raw body
without its registered attempt and quarantine chain is not authority.

There must be exactly two regular `630882` records, both Final, official date
`2020-08-09`, away `110`, home `120`, score 6–2, and unique away winner `110`.
Suppress `(17,0)`, object
`2a5b251745546e643897cfc01eaa4c63c949bb8d8243b7fec44d3a23484f9be4`:
start `2020-08-09T16:35:00Z`, venue `3309`,
`resumeDate=2020-08-14T21:05:00Z`, `resumeGameDate=2020-08-14`.
Retain `(22,0)`, object
`0d961f12abb78e3b833c9f5c5bbbff76cad80a5370f57786904c7b42e82b204e`:
start `2020-08-14T21:05:00Z`, venue `2`,
`resumedFrom=2020-08-09T16:35:00Z`, `resumedFromDate=2020-08-09`.

The source response must have 973 regular occurrences, 900 unique IDs, 71
duplicate IDs, 144 duplicate-member occurrences, and 73 duplicate extras
(that is, occurrences beyond the 900 unique IDs). It has one suppression, 972
projected occurrences, and 900 normalized games. Retaining venue `2` is a fact
of this exact response, not a venue-selection policy. The two object digests
above are SHA-256 of the committed evidence checker's
`canonical_json_bytes` representation with `ensure_ascii=False`; no other JSON
serialization is an accepted identity.

### 2021 — gamePk 633224

The parent is the failed superseding 2021 HTTP query.  Its closed binding is:

```text
query ID:            91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2
registration SHA-256: b915233fb3c3ea796330486490f3b90d6746ce82e1f03b30b9c964ba6569cfd3
attempt SHA-256:      b7d76d96191bc80c578965ec5242a3abeb44dade601a5ca3c76c30d4c45f1652
quarantine SHA-256:   d3c9d9f197cab51d5ab509887002e4df3f300cb20c23a888120395e9b7042c36
raw SHA-256 / bytes:  84f103b39485151f816726e6a6943ea2498b8ac769c1a27d9e8603a7ca2e4b17 / 21223271
```

The parent artifacts are exactly:

- `data/v5_2_qd/historical_superseding_565534/raw/queries/mlb_schedule_game_feed/91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2/query.json`,
  675 bytes, whose `parent_query_id` is JSON `null`;
- `data/v5_2_qd/historical_superseding_565534/raw/queries/mlb_schedule_game_feed/91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2/attempts/000001.json`,
  664 bytes;
- `data/v5_2_qd/historical_superseding_565534/raw/quarantine/mlb_schedule_game_feed/91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2/000001.json`,
  938 bytes; and
- `data/v5_2_qd/historical_superseding_565534/raw/quarantine/mlb_schedule_game_feed/91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2/84f103b39485151f816726e6a6943ea2498b8ac769c1a27d9e8603a7ca2e4b17.raw`,
  21,223,271 bytes.

All four path, SHA-256, byte-count, query-ID, and `parent_query_id` bindings
are part of the disposition's closed old-authority object; a matching raw body
without its registered attempt and quarantine chain is not authority.

There must be exactly three regular `633224` records, all official date
`2021-07-21`, away `135`, and home `144`:

- suppress `(106,12)`,
  `b5d52997a362f89e669e1454960b5230d4977f30370ad3f3ad7ca1b12be5221d`:
  Postponed for Rain, start `2021-07-19T23:20:00Z`, venue `4705`;
- suppress `(108,2)`,
  `25f8ab0769219c592779cd800bb2b3278c9c75644647bd51ae5db3051bcf5a61`:
  Final 6–5, away winner `135`, start `2021-07-21T21:20:00Z`, venue `4705`,
  `resumeDate=2021-09-25T00:10:00Z`, `resumeGameDate=2021-09-24`;
- retain `(173,11)`,
  `109a5d145731938e08f516f19345557894316b3a72687f4ac63029df07afe94c`:
  Final 6–5, away winner `135`, start `2021-09-25T00:10:00Z`, venue `2680`,
  `resumedFrom=2021-07-21T21:20:00Z`, `resumedFromDate=2021-07-21`.

The source response must have 2,512 regular occurrences, 2,430 unique IDs, 80
duplicate IDs, 162 duplicate-member occurrences, and 82 duplicate extras
(that is, occurrences beyond the 2,430 unique IDs). It has two suppressions,
2,510 projected occurrences, and 2,430 normalized games. Neither the
postponed nor the first terminal record may create an anchor or child edge.

## Closed resumed-result availability boundary (2020 and 2021 only)

`official_date` remains immutable schedule provenance. It is not an as-of
availability assertion for the two retained resumed terminal records below.
This section adds no general resumed-game rule and does not alter the existing
2019 `565534` disposition or its three approved exact schedule neutralizations.

The recovery disposition sidecar for exactly the following two retained
occurrences has two additional exact semantics keys, `played_date` and
`result_available_on`. Both are canonical ISO dates and are authenticated from
the already-required retained/suppressed resume linkage; callers cannot supply
or derive them from a timestamp, venue, status, or calendar heuristic.

| gamePk | immutable `official_date` | exact `played_date` | exact `result_available_on` | authenticated source field |
|---:|---|---|---|---|
| 630882 | 2020-08-09 | 2020-08-14 | 2020-08-14 | `resumeGameDate` on the suppressed occurrence |
| 633224 | 2021-07-21 | 2021-09-24 | 2021-09-24 | `resumeGameDate` on the first terminal suppressed occurrence |

Only those two recovery rows may carry the boundary fields. Every other
recovery schedule anchor, including `565534` and all four HTTP seasons, retains
the existing exact field set and semantics. A missing field, either field on an
unlisted row, a value different from the table, a changed parent linkage, or an
attempt to set a value earlier than the listed date fails closed.

The schedule-anchor and game-evidence loaders accept a retained
`scheduled_start_utc` outside the existing `official_date` plus or minus one
day rule only when the row is one of these two authenticated disposition
bindings and its exact retained timestamp is paired with the table's
authenticated local MLB schedule date (`resumeGameDate`) as `played_date`.
For example, `633224` retains `2021-09-25T00:10:00Z` but has
`played_date=2021-09-24`; the UTC calendar date is not an accepted substitute.
The ordinary validation stays unchanged for every other row. The Statcast
completeness/replay join for these two gamePks is against the raw Savant
`game_date` string and requires an independently authenticated equality to
`played_date`, not `official_date`; a different raw Statcast date is rejected.
This is a join and availability rule only: it does not rewrite `official_date`,
target identity, season membership, or any outcome identity, and it authorizes
no downstream execution.

For any future, separately approved as-of consumer, a result-bearing schedule
row for either game is admissible on target date `D` only if
`result_available_on < D`. Therefore the first permissible date targets are
`2020-08-15` for `630882` and `2021-09-25` for `633224`. Before that boundary,
the entire row is absent from schedule-history ordering and from D-1 rest,
travel, home-city, innings, latest-game, and every other schedule-derived
input; no neutral metric is substituted. This is deliberately distinct from
the existing three exact 2019 neutralizations, which remain unchanged and do
not authorize an additional 2020 or 2021 neutralization.

## Authority, schemas, and Git binding

Add one exact third `SourceRunSpec` branch and one exact third acquisition
contract branch.  The old RunSpec and existing superseding SourceRunSpec,
their parsers, their writer output, and their schema branches remain byte-for-
byte and semantically unchanged.  Dispatch selects a branch only from the
exact approved RunSpec path paired with its exact contract path; run-ID
prefixes, inferred roots, and fallback selection are forbidden.

The recovery contract binds this design, its implementation plan, the new
RunSpec, their working-tree SHA-256 values, and their `HEAD:path` Git blob IDs.
It also contains a closed recovery-provenance object binding the prior
superseding authorities, phase heads, static batch, terminal checkpoint, and
three distinct named parent objects: `original_2019_565534`,
`superseding_2020_630882`, and `superseding_2021_633224`. Each object has the
exact query registration, attempt, quarantine receipt, raw-body path, SHA-256,
byte count, query ID, and parent-query-ID binding stated in its catalog entry;
the two superseding objects also bind the prior superseding RunSpec and contract
identities. A merged parent list, an omitted attempt binding, or a reused 2019
object is invalid. Provenance grants no mutation or promotion right.

The implementation, schemas, tests, this design, the implementation plan, and
the new authority files must be committed before evidence creation.  The full
40-hex HEAD containing that exact execution closure is captured in every new
phase receipt and checkpoint.  The working tree must be clean for every bound
file.  A later code/authority commit, changed blob, dirty bound file, path
race, or source identity change invalidates recovery loading/advancement; it
does not authorize amendment of the recovery chain.

The execution-closure seal is intentionally ordered: first bind and commit the
recovery code, schemas, tests, and these two approval documents; then commit
the canonical RunSpec; finally commit the contract containing the committed
blob identities and digests. The exact-suite runner authenticates the two
superseding parent chains through a separate API, so its legacy baseline input
sets remain unchanged.

The derived-sidecar schema preserves its existing branches and adds three
closed recovery `oneOf` branches, one for each endpoint above.  Each new branch
has exact parent authority, occurrence counts, retained/suppressed object
facts, normalized-game digest, and selected-raw-object digest.  The checkpoint, schedule-anchor, game-evidence, normalized
snapshot, source-inventory, review, replay, and finalization schemas gain a
closed third authority branch.  No open common-property subset is accepted.

The `630882` and `633224` recovery sidecar branches additionally require
exactly `played_date` and `result_available_on` with the table values above;
the recovery `565534` branch rejects both keys. The source-only
schedule-anchor, game-evidence, normalized-snapshot, and history payload
schemas carry the two fields only for those two rows and bind them to the
corresponding three-entry disposition catalog. They reject a copied standalone
boundary, an unbound row, or a generic `resumed`/date-range flag. Recomputed
canonical hashes include the fields wherever they appear. Loaders and replay
first authenticate the derived sidecar, then project the boundary; they never
accept it as caller input.

The recovery-only branch replaces the current singular disposition representation
with one closed, canonically ordered three-binding catalog across checkpoint,
inventory, review, replay, source-finalization, and `SOURCES_READY` loading.
The existing superseding branch retains its singular `565534` field, schema,
query ID, hashes, and behavior byte-for-byte; it is not widened or interpreted
as a recovery catalog.

## Checkpoint, inventory, and finalization invariants

The recovery registry is a new chain:

```text
DESIGN_ACCEPTED -> ACQUIRING -> SOURCES_READY
```

STATIC_SCHEDULES and later acquisition stages are append-only checkpoints while
the registry remains `ACQUIRING`.  The first recovery checkpoint has seven
pending IDs and explicit `dispositioned_query_ids: []`.  After derived
publication it has exactly three new derived IDs in both `completed_query_ids`
and sorted `dispositioned_query_ids`; each authenticates as its exact raw-less
branch.  After the four HTTP successes, the static checkpoint has exactly all
seven new IDs completed and no pending, quarantined, or split ID.  An old or
prior-superseding ID is invalid in every recovery population.

The schedule inventory has seven season sources, exactly one retained anchor
for each of `565534`, `630882`, and `633224`, and no anchor/boxscore/venue/
timezone/history/pitching/career child edge from a suppressed occurrence.  It
carries the selected raw-object hash and the three derived success/sidecar
bindings forward.  The three existing approved downstream neutralizations
remain exact and available only when the authenticated 565534 disposition is
present; no additional neutralization is authorized.

The recovery preserves the source-inventory cardinality contract: exactly 81
snapshots, including exactly seven schedule season partitions, with the
existing source/season totals unchanged. Its inventory additionally requires
the canonically ordered three derived bindings; an 81-snapshot payload with one,
two, four, stale, or differently ordered bindings is not source-gate-ready.

For `630882` and `633224`, the inventory additionally carries the boundary only
through their authenticated derived sidecar bindings and the canonical special
row projections. Checkpoint population, review selection, both clean replays,
the source inventory, and the `SOURCES_READY` transition must each rebind all
three disposition sidecars and recompute the two boundary-bearing rows. They
reject a missing boundary, an extra boundary, a stale sidecar, a boundary copied
from another run, or an inventory/replay that has the three query IDs but not
the exact `played_date`/`result_available_on` semantics.

The recovery may advance to `SOURCES_READY` only after a terminal `REMAINING`
checkpoint with zero pending/quarantined IDs, a canonical source inventory,
one or more explicit APPROVED independent review receipts, and exactly two
explicit clean replay receipts with distinct path, SHA-256, and canonical UUIDv4
execution ID.  All selected receipts bind the same recovery inventory,
authorities, Git head, snapshot hashes, and zero network attempts.  Direct
registry calls enforce these rules; no glob, directory scan, timestamp, or
“latest” convention selects evidence.

## Failure behavior and acceptance

Any unexpected duplicate, changed field, missing/extra special occurrence,
wrong linkage, wrong parent receipt/raw identity, noncanonical JSON, hash
mismatch, symlink/hard-link, changed Git closure, or final-rebind race fails
closed.  A failed derived publication cleans only a newly created recovery
sidecar after verifying ownership; it never deletes provenance.  A failed HTTP
query is quarantined only in the recovery root and blocks progress.  No retry
may turn prior quarantine into success.

Implementation acceptance requires all of the following:

1. Byte/hash/file-count manifests prove both pre-existing evidence trees are
   unchanged before and after every test and operator action.
2. Exact old/new and prior-superseding/recovery cross-binding, namespace
   overlap, symlink, hard-link, traversal, and changed-HEAD tests fail.
3. v5.2.2 accepts the audited ordinary duplicate populations for 2018/2022/
   2023/2024, while raw HTTP parsing still rejects 630882 and 633224.
4. Each disposition accepts only its catalog record set and proves the stated
   counts, selected normalized output, sidecar digest, and final rebind; every
   altered predicate and a generic-resume bypass fails.
5. The recovery static batch/checkpoint has four HTTP and three raw-less
   derived query IDs only; all rawless stores have no object, attempt,
   reservation, retry, or network call.
6. Schedule, game, snapshot, inventory, review/replay, and `SOURCES_READY`
   tests require all three derived bindings and reject a missing, extra, stale,
   cross-run, or tampered binding.
7. A `630882` history consumer rejects the retained row for every target date
   from `2020-08-10` through `2020-08-14`, including every final score, venue,
   innings, rest, travel, and latest-game derivative, and admits it first on
   `2020-08-15`. A `633224` consumer does the same for every target date from
   `2021-07-22` through `2021-09-24` and admits it first on `2021-09-25`.
   The existing 2019 three-neutralization fixture is byte-for-byte and
   behaviorally unchanged.
8. The two exceptional schedule rows accept only their listed retained UTC
   timestamps paired with their authenticated local `resumeGameDate` values;
   `633224` specifically rejects using `2021-09-25` as `played_date`. The
   independently authenticated raw Savant `game_date` for each game must equal
   its exact `played_date`; default schedule--Statcast equality remains
   `official_date` for every other row. Missing, changed, or cross-boundary
   Statcast rows fail the inventory and replay gates.
9. The complete Plan A source test suite and exact execution-code closure pass
   at the committed recovery head, and no forbidden downstream artifact exists.
10. Publication and a clean rerun are idempotent for each of the three
    dispositions only when the existing final sidecar and `success.json` bytes
    are identical. A changed provenance inode/bytes, sidecar, boundary field,
    or final-rebind race fails closed without deleting any parent evidence.
11. Checkpoint, replay, review, inventory, and `SOURCES_READY` tests reject
    every partial or overfull recovery catalog (zero, one, two, or four
    bindings), duplicated IDs, a correct ID paired with another disposition's
    sidecar, and any attempt to satisfy the recovery branch with the unchanged
    singular superseding representation.

The source-finalization workflow remains the approved 2026-07-19 design,
parameterized by this exact authority.  It stops at `SOURCES_READY`; any use of
this inventory for fitting or later continuation requires a separate approved
plan that explicitly selects this recovery run.
