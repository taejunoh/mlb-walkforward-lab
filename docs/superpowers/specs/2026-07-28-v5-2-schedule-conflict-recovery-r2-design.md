# MLB v5.2 Schedule-Conflict Recovery r2 Design

**Status:** approved source-only recovery authority. This design permits work only through `SOURCES_READY`; it does not permit fitting, features, locks, outcomes, cards, OOF, selections, targets, reports-500, or Plan B/C work.

## Decision

The r2 implementation starts from sealed old head `7344313` on a new
branch/worktree and appends a new Stage1 -> Stage2 -> Stage3 chain. The former
recovery authority is immutable forensic evidence. Its `DESIGN_ACCEPTED` and
`ACQUIRING` receipts and every old-namespace artifact are external immutable
runtime evidence at `FORENSIC_ROOT=/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest`, not commits in CODE_ROOT. Required files are
`000000-DESIGN_ACCEPTED-c10c89bd8739e883b2251032d15e9e41995c7c6c1cfce9cb0b2f0a57e7354bc7.json`
and
`000001-ACQUIRING-514ed262068cbc40477b4bd157d66266260d5834704a38fd169133929df22036.json`.
The current code cannot resume that chain because execution receipts bind the
Git head and complete execution-code snapshot. The former design, plan,
RunSpec, and contract remain unchanged at `7344313efa5c8181bd96fbd09fef934e8b40c4a0`
in CODE_ROOT and the old branch; external evidence is checked in place and is
never copied. No former recovery file is deleted, moved, copied as r2 output,
reclassified, or loaded as r2 state.

The complete former runtime-evidence scope is only
`audit/v5_2_qd/historical_recovery_schedule_conflicts`: its two named phase
receipts and two named locks. No former recovery raw, sources, or reports root
exists. The operator guard must assert those three roots are absent before and
after every r2 write, assert the two receipt files exist, and require each
receipt's `shasum -a 256` value to equal the SHA-256 embedded in its filename.

Create exactly one fresh source authority:

| Field | r2 value |
|---|---|
| `run_id` | `historical_walkforward_500_recovery_schedule_conflicts_r2` |
| RunSpec | `protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json` |
| contract | `protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json` |
| raw | `data/v5_2_qd/historical_recovery_schedule_conflicts_r2/raw` |
| sources | `data/v5_2_qd/historical_recovery_schedule_conflicts_r2/sources` |
| audit | `audit/v5_2_qd/historical_recovery_schedule_conflicts_r2` |
| reports | `reports/v5_2_qd/historical_recovery_schedule_conflicts_r2` |
| schedule parser | `v5.2.2` |
| timezone mapping | `audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/acquisition/venue_timezones.json` |

The r2 roots are lexically disjoint from, and cannot be symlinks, hard links, ancestors, or descendants of, historical, superseding, or former recovery roots. Every r2 failure remains in its r2 evidence slot; r2 cannot reuse an old success, checkpoint, phase receipt, raw object, or source inventory.

## Closed source semantics and provenance

`STATIC_SCHEDULES` has seven ordered entries: fresh HTTP schedule roots for 2018, 2022, 2023, and 2024, and raw-less `DERIVED_DISPOSITION` entries for 2019 gamePk 565534, 2020 gamePk 630882, and 2021 gamePk 633224. The HTTP roots create new r2 attempts, sidecars, objects, and successes. The three exact disposition names are:

```text
disposition://SCHEDULE_DUPLICATE_GAMEPK_565534
disposition://SCHEDULE_DUPLICATE_GAMEPK_630882
disposition://SCHEDULE_DUPLICATE_GAMEPK_633224
```

Each disposition has empty parameters and no HTTP attempt, reservation, retry, raw object, or network gate. It publishes only through a lease-held authority-aware `DERIVED_DISPOSITION` transaction. Immediately before `success.json`, it reopens and checks fixed parent registration, attempt where applicable, quarantine, raw bytes, SHA-256, byte count, regular/single-link/no-follow identity, and its fixed selection/suppression projection. It serializes no replacement parent JSON.

The contract preserves the existing three closed parent provenance objects and parser policy: `existing_source_parser_version` is `v5.2.1`; `recovery_schedule_parser_version` is `v5.2.2`; and only the three named dispositions are allowed. Parser v5.2.2 applies only to the seven recovery schedule LogicalQueries. Raw `totalGames` and date totals remain authoritative and normalized schedule population is checked separately; v5.2.2 is not a generic resumed-game, venue-conflict, date-conflict, or last-record-wins policy. Every non-schedule r2 query and every baseline/superseding query remains v5.2.1.

## Centralized authority selector

The authenticated selector compares the parsed authority with the canonical r2 `SourceRunSpec` by exact equality. It returns a single matching bundle of design, plan, acquisition contract, RunSpec path, raw namespace, audit namespace, source namespace, schedule-inventory path, timezone gate, query-batch root, and execution registry prefix.

| Authority | design/plan | contract | canonical raw root |
|---|---|---|---|
| baseline | 2026-07-13 approvals | historical contract | `data/v5_2_qd/historical/raw` |
| superseding 565534 | 2026-07-21 approvals | superseding contract | `data/v5_2_qd/historical_superseding_565534/raw` |
| recovery r2 | this design and r2 plan | r2 contract | `data/v5_2_qd/historical_recovery_schedule_conflicts_r2/raw` |

No broad `isinstance(SourceRunSpec)` branch may route r2 as superseding. Unknown source authorities fail authentication rather than falling through to r2 or superseding behavior. The r2 timezone mapping is authority-owned audit evidence at the path above, never shared `config/v5_2_qd/venue_timezones.json`; the selector, RunSpec, contract, schemas, consumers, and tests must bind that same r2 mapping. Baseline and superseding behavior remain unchanged.

## Lifecycle and acceptance

The registry permits only `DESIGN_ACCEPTED`, `ACQUIRING`, and `SOURCES_READY`. r2 phase receipts bind the r2 design, plan, contract, Git head, and execution snapshot. Plan-only, network, parser, review, replay, or advance failures remain r2 evidence and never authorize former-recovery reuse.

Acceptance requires all of the following:

1. Former recovery protocols and receipts remain present and unchanged, while no r2 command targets their paths.
2. Stage2 creates only the r2 RunSpec; Stage3 creates only the r2 contract, binding the committed r2 design, plan, and RunSpec blobs.
3. Real r2 initialization binds r2 design, plan, and contract before advancing to `ACQUIRING`.
4. Raw-parent checks accept baseline, superseding, and r2 only in their own namespaces and reject cross-authority substitution.
5. Downstream source-only artifacts, schemas, inventories, timezone evidence, replays, reviews, and finalization use the r2 ID and r2 prefixes exclusively.
6. Plan-only, reference, full acquisition, timezone, publish, replay, review, and `SOURCES_READY` advance complete without modifying protected parents or former recovery evidence.
