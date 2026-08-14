# v5.2 Source Finalization CLI — Implementation Record

## Implementation scope and boundary

- Governing plan: `docs/superpowers/plans/2026-07-20-mlb-v5-2-source-finalization-cli.md` at `bdbeb7df84aec4cf8f3d3b00bd9d8a1dd112b6ae`.
- Audited implementation range (plan-exclusive): `bdbeb7df84aec4cf8f3d3b00bd9d8a1dd112b6ae..c8e47c4820e3564fb8067de32b187ca811198582` (51 commits); first implementation commit: `90c55d5`.
- Implementation HEAD: `c8e47c4820e3564fb8067de32b187ca811198582`.

The range delivers the hash-bound source-receipt schemas, verified snapshot replay inputs, trusted finalization context, source inventory publication, review/replay/`SOURCES_READY` stages, staged CLI, exact runtime closure, and adversarial path/network/`dir_fd` E2E guards. This record covers implementation and verification only; it is not an acquisition or model-performance result.

No canonical historical acquisition, source publication, review, replay, or phase transition was performed by implementation tests.

## Execution-closure evidence

The exact runtime authority invariant is:

```text
bound == reachable | extras | schemas
49 == 34 reachable Python modules + 1 explicit extra + 14 schemas
```

The explicit extra is `mlb_backtest/verify_v52_design.py`. The closure test rejects a missing reachable module, a stale extra, or a schema-set mismatch.

## Bound schema digests

| Schema path | SHA-256 |
|---|---|
| `schemas/v5_2_acquisition_checkpoint.schema.json` | `1f5c4e7a99c020c55474e971ca4659eb73d681f4340b8ec7553491aa048925b5` |
| `schemas/v5_2_schedule_anchor_inventory.schema.json` | `66698b643ad186434d0930a7f369b3b292f664e1152ddaaa727e6d0003a13c31` |
| `schemas/v5_2_execution_receipt.schema.json` | `0b3c442248c8f64616f78ce4f755be2e97514aee178f9f6a7c9ac30f72ca427d` |
| `schemas/v5_2_game_evidence_inventory.schema.json` | `9460307dccfc65f82f5a0f837c0139eb4be7dc0337483a6337264c104f85bd70` |
| `schemas/v5_2_legacy_decision_inventory.schema.json` | `39864790fa275262bbdb932efaa9f52138aa6d29b98881666b01b9fbb80a68e5` |
| `schemas/v5_2_split_receipt.schema.json` | `b9de7114410e036cd43a2530a8440dcdbc1b85d78699281e1c623d234102aeea` |
| `schemas/v5_2_timezone_candidate.schema.json` | `9cc9d49ec18a7e538d37e6b00bf8acb1185aaeeba4ad97e836e12f200ab5f91b` |
| `schemas/v5_2_timezone_review_receipt.schema.json` | `f809ef68ed1ececb70fd33ddad15e4728eab0ad821243303aa317e69c2d11727` |
| `schemas/v5_2_timezone_seal_receipt.schema.json` | `4f63caad4371e39aa41fea81dc2825cd71b3a9de4087d14f2e590963cd763126` |
| `schemas/v5_2_venue_timezones.schema.json` | `beb1469e4e6860b1670cbb05e0a3f4b59d82b3e753025701eb04ef25d6a026a0` |
| `schemas/v5_2_source_inventory.schema.json` | `d091e9d34d2c4c668f1c90ef749e6deca4caa468f41ad8281faf56bb401ab60a` |
| `schemas/v5_2_source_review_receipt.schema.json` | `13c137f5c143bf3c3e9140ae6f940ac72bf6d11909d736a60acc718fb93943cf` |
| `schemas/v5_2_clean_source_replay_receipt.schema.json` | `f233957be1aae7c60a32a0af6ac539c5c4b39d983ade094dd8044d922fd9b663` |
| `schemas/v5_2_source_receipt_commit.schema.json` | `4f21f79fca44d7ec3ed2efe0403159754cfbf2101ad3bc5256982bfdfaf65a46` |

## Verification ledger

| Gate | Command | Result |
|---|---|---|
| Static | `ruff check mlb_backtest tests` | Exit 0 — `All checks passed!` |
| Static | `git diff --check` | Exit 0 |
| Targeted finalization gate | `python3 -m unittest tests.test_v52_source_finalize tests.test_execution_registry tests.test_v52_snapshot_store tests.test_v52_verify_sources -v` | Exit 0 — `Ran 349 tests in 8017.934s`; `OK` |
| Full stable suite | `python3 -m unittest discover -s tests -p 'test*.py' -v` | Exit 0 — `Ran 1613 tests in 16937.516s`; `OK` |

## Protected repository snapshot

At the implementation HEAD, `git diff --name-only --cached` was empty. The only untracked paths were the four protected duplicates:

- `mlb_backtest/v52_query_plan 2.py`
- `tests/test_v52_query_plan 2.py`
- `reports/v5_2_qd/walkforward_500_preflight 2.md`
- `schemas/v5_2_preflight.schema 2.json`

| Command | Result |
|---|---|
| `git status --short` | Protected untracked duplicates: 4 only |
| `git diff --name-only --cached` | Exit 0; no paths |
| `git ls-files --error-unmatch 'mlb_backtest/v52_query_plan 2.py' >/dev/null 2>&1; test $? -ne 0` | Exit 0; untracked |
| `git ls-files --error-unmatch 'tests/test_v52_query_plan 2.py' >/dev/null 2>&1; test $? -ne 0` | Exit 0; untracked |

Both canonical historical namespaces were absent:

- `audit/v5_2_qd/historical/`
- `data/v5_2_qd/historical/`

## Limitations

These gates establish code, schema, fixture, and repository-state behavior only. They do not establish source availability, historical acquisition completeness, fitted-model quality, walk-forward performance, or a prospective operational claim. Any such claim requires separately produced canonical evidence under the guarded historical namespace and its own evaluation record.
