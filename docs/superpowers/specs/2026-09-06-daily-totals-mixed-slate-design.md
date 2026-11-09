# Daily Totals Mixed-Slate Exclusion Design

## Decision

Daily totals must authenticate the complete requested-date live schedule before
deciding which games can receive a forecast. It then partitions that immutable,
fully bound population into strict-prestart targets and explicit exclusions.
This permits a mixed slate without retrospectively admitting an already-started
game.

The report stays `PROVISIONAL_DAILY_TOTALS_V1`: `EXCLUDED` is an additive,
closed game-row variant. Existing V1 reports and receipts remain valid
byte-for-byte. The model receipt remains unchanged; no weights, rolling
windows, history admission, candidate selection, frozen model, or historical
artifact changes.

## Admission and partition

`load_totals_sources_bound` retains its existing receipt/claim/completion/
manifest/payload chain, regular-file rebinds, and trusted-root assertions. It
must parse every requested-date regular-season MLB row and compare sorted IDs
for **all parsed live rows** with `LIVE_SCHEDULE.binding.game_pks` before the
partition. It must never compare only eligible IDs.

| Condition | Output |
| --- | --- |
| status is `Scheduled`, `Pre-Game`, or `Warmup`, and durable observation time is strictly before start | `TargetTotalGame` |
| status is not accepted | `ExcludedTargetGame`, `PREGAME_STATUS_NOT_ACCEPTED` |
| accepted status but durable observation time is at/after start | `ExcludedTargetGame`, `OBSERVATION_AT_OR_AFTER_START` |

Status rejection takes precedence, so a row has one deterministic reason. The
strict `durable_at_utc < scheduled_start_utc` rule is unchanged. A fresh source
that still includes an already-started row does not make it eligible.

`TotalsSourceBundle` gains `excluded_target_games`. An
`ExcludedTargetGame` carries game ID, official date, scheduled UTC start, team
and venue IDs, the source detailed status, and one closed reason. `target_games`
contains only eligible `TargetTotalGame` values.

Each report exclusion also carries `source_observed_at_utc`, the live binding
`durable_at_utc`. The standalone semantic validator requires
`PREGAME_STATUS_NOT_ACCEPTED` to have a non-accepted `source_status`, and
`OBSERVATION_AT_OR_AFTER_START` to have an accepted status with observation
time at or after start. Persistence re-admits the source and proves every
exclusion's identity, source status, reason, and observation time equal the
sealed `ExcludedTargetGame`; a valid-looking enum/digest combination cannot
relabel the reason.

## Report contract

The `games` array gains this exact closed V1 variant:

```json
{
  "game_pk": 823577,
  "official_date": "2026-09-05",
  "scheduled_start_utc": "2026-09-05T20:10:00Z",
  "away_team_id": 1,
  "home_team_id": 2,
  "venue_id": 3,
  "status": "EXCLUDED",
  "source_status": "In Progress",
  "source_observed_at_utc": "2026-09-05T21:41:40Z",
  "reason": "PREGAME_STATUS_NOT_ACCEPTED",
  "source_digest": "<LIVE_SCHEDULE payload SHA-256>",
  "data_quality_flags": ["ADMITTED_SCHEDULE", "EXCLUDED", "PREGAME_STATUS_NOT_ACCEPTED"]
}
```

An exclusion has no candidate, feature cutoff, expected runs, total, interval,
or residual evidence. Its digest must equal `live_source.payload_sha256`.
Projected and unprojected rows retain their current exact fields and require
the history digest. Validation retains chronological ordering and unique IDs,
and accepts a nonempty exclusion-only `games` list.

Markdown retains its research-only labels and renders exclusions as em dashes
with `EXCLUDED (<reason>; source_status=<source_status>)`. It must never call one a
projection, pick, prediction, or market comparison.

## Build and overlay boundaries

All source/history/window/walk-forward/minima/receipt checks still occur before
report construction. The builder projects only `bundle.target_games`, appends
one exclusion row per `bundle.excluded_target_games`, validates/hashes the
whole report, and preserves append-only publishing. All-excluded therefore
means a sealed exclusion-only report, not false picks or omission.

No new prediction is backdated for 2026-09-05; a later attempt needs a newly
admitted report and has distinct hashes.

The market overlay remains the frozen 2026-09-01 15-projected-game authority.
Its exact report/trace hashes and `_fixed_model_gates` must reject an
`EXCLUDED` row before accessing forecast fields. General mixed-slate market
comparison is out of scope; excluded rows cannot be silently treated as
projections or used to alter the frozen authority.

## Proof obligations

Tests prove full binding before partition, mixed-slate eligibility/exclusions,
strict equality rejection, exclusion-only output, live-digest binding, closed
schema rejection of forecast fields, old V1 fixture validity, no write after a
binding failure, renderer text, and frozen-overlay rejection of a resealed
synthetic authority containing an exclusion.
