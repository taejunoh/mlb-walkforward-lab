# Single-book market intake

Approved: the user accepted changing actual collection to one sportsbook on
2026-09-06, while preserving freshness, paired prices and game identity checks.

## Scope

New normalized boards may contain the actual outcomes from one through five
distinct supported books. A book not attempted is absent, not UNAVAILABLE.
UNAVAILABLE continues to mean a real recorded unsuccessful collection attempt.
The existing minimum eligible quote coverage remains one; an unavailable-only
board supplies no eligible quotes and cannot by itself produce PLAY.

Use a V2 board schema to distinguish the new intake contract. V1 boards retain
their exact-five-outcome requirement and must remain readable without rewriting
historical bytes. New normalization emits V2 and records only supplied outcomes.
All supplied outcomes must still authenticate, be unique by supported book, and
map to the complete admitted official schedule. Preserve deterministic ordering.

Keep quote freshness at 300 seconds, paired-price validation, event/team/start
identity, duplicate rejection, source hashes, append-only persistence and final
rebind checks. No evaluator threshold or model probability changes are included.
Do not promote the DKNetwork observation-only capture to direct-book evidence.
Provider update times must not be invented or substituted for collection time.

## Boundaries

This change removes the requirement to supply four extra book outcomes. It does
not implement network collection or the missing official-schedule producer, and
does not claim a newly admitted real market board, profitability or independent
O/U calibration. Preserve all user edits and sealed model/report evidence. No
network, installations, commits, push, merge, full suite or protected-path access.

## Acceptance

- One captured supported-book outcome normalizes, persists, reloads and reaches
  daily evaluation without extra unavailable placeholders.
- Two through five actual distinct outcomes remain accepted; input order does
  not change normalized bytes.
- Empty, duplicate and unsupported outcomes fail, with no successful board.
- Valid V1 five-outcome boards load unchanged; incomplete V1 boards reject.
- V2 tampering, stale/unpaired quotes and identity mismatches remain rejected or
  PASS according to the existing boundary that owns each check.
- Focused unit/schema/integration tests, independent reviews and final checks
  pass; no historical artifact is rewritten.
