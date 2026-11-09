# Postseason ML focused pre-change baseline

Captured 2026-09-30 in `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix`, before the requested implementation work. Tests were run serially with the exact command form shown below; no source or test files were changed by this baseline run.

## Focused unittest discovery

All commands used `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p '<pattern>'`.

| Pattern | Ran | Result |
| --- | ---: | --- |
| `test_live_sources.py` | 63 | PASS |
| `test_live_history.py` | 60 | PASS |
| `test_live_features.py` | 26 | PASS |
| `test_live_top3_contracts.py` | 32 | PASS |
| `test_live_top3_trace.py` | 8 | PASS |
| `test_live_top3_service.py` | 39 | PASS |
| `test_daily_top3.py` | 25 | PASS |
| `test_daily_top3_e2e.py` | 13 | PASS |
| `test_prospective_ml_daily_flow.py` | 0 | DISCOVERY FOUND NO TESTS |
| `test_prospective_ml_settlement.py` | 10 | PASS |
| `test_market_play_authorities.py` | 8 | PASS |
| `test_daily_totals.py` | 41 | PASS |

Total: 325 tests run, 325 passed, 0 failed/errors; one discovery pattern selected zero tests. `test_daily_top3.py` emitted expected CLI/integrity/offline diagnostic text while its tests passed. No network test was invoked by this focused set.

### Pytest-only file follow-up

`tests/test_prospective_ml_daily_flow.py` imports `pytest` and uses pytest fixtures (`tmp_path`, `monkeypatch`); unittest discovery therefore collected 0 tests. Pytest was already installed (`pytest 9.0.2`), so this gap was closed with the file-only command below. `TMPDIR` points inside the workspace so pytest's temporary fixtures remain scoped to the project; no dependency was installed.

```sh
TMPDIR="$PWD/.pytest-tmp-baseline" PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest -p no:cacheprovider tests/test_prospective_ml_daily_flow.py
```

Result: 23 passed in 1.63s, 0 failed/errors. This brings the focused verified total to 348 passed, 0 failed/errors across the 12 unittest patterns and this pytest file; the unittest discovery entry remains 0 tests as expected for a pytest-only module.

## SHA-256 baseline

Frozen files:

| File | SHA-256 |
| --- | --- |
| `mlb_backtest/normalize.py` | `3fe64a38e931e620f376908a6efc042488f8aa353e091b278fc52d436ab02e1a` |
| `mlb_backtest/scoring.py` | `0018dc28f8ee5163b62d3d1b4c0460f7b3371c9b5172b1b0b2373be913b185e0` |
| `mlb_backtest/scoring_prep.py` | `b41cbafc4ad13d3cc9f4d1cd16de8a734f23e903b2ffaef246a0e85607e1ef6b` |
| `mlb_backtest/live_model_card.py` | `e689df8d331fbe1342b810b5bef16e6dceb5e08460524eb1336ca5d77235abf0` |

Expected implementation files, hashed to distinguish pre-existing dirty/untracked state from subsequent changes:

| File | SHA-256 |
| --- | --- |
| `mlb_backtest/daily_top3.py` | `e2dcff568944fd27664902edb3cddca43b71a21ce71e65f6e36cc6a33b67bf52` |
| `mlb_backtest/live_sources.py` | `36cec4b0039d9b175da3e3b9831fe177884c46779e3fe56ddaae7e22b63e87a1` |
| `mlb_backtest/live_history.py` | `87722fafba5c073a6dd806422bce724302258eacf5eaee14c672f16fc9a14bc2` |
| `mlb_backtest/live_features.py` | `8194cd44599777baf28522c3995423c175126e81c2065504dad7544232b6bcc5` |
| `mlb_backtest/source_parsers/schedule.py` | `de3f3fbbfb0fbd38e4841eccbeeedd0c70f3eb769106c933f2ad5be045417e2a` |
| `mlb_backtest/source_parsers/statcast.py` | `eb97ab41e6dc93374af8cc46e9ff07451d2328f223c55b17c77b40ac3b9ddea2` |
| `mlb_backtest/source_parsers/mlb_stats.py` | MISSING (no such file under `mlb_backtest/source_parsers/`) |
| `mlb_backtest/prospective_ml_daily_flow.py` | `b633c346271c136710e3897d863e22ed9f162db5f18ad46e6ff2b5e0c17f0e4e` |

Existing untracked prospective modules:

| File | SHA-256 |
| --- | --- |
| `mlb_backtest/prospective_ml_observation.py` | `e5115dae280e0dd05675068d6ff79bf982aff7457a9e7a9b591ff9897660d255` |
| `mlb_backtest/prospective_ml_results.py` | `d2d8ad220e9d5db3159be4af332d88eae1133da727482105a3dad4a4571f6886` |
| `mlb_backtest/prospective_ml_settlement.py` | `69b10194549a9cb734d617d5e0e538ec64cbd03e463fa9e6d9f1e8877c931361` |
| `mlb_backtest/prospective_ml_shadow.py` | `61aa7dbb248d955b3b5cc36ac2230fdd9fda889881d496a27764e48fffa94d68` |
| `mlb_backtest/prospective_ml_shadow_report.py` | `2d863f0331f8fb9066c2d9f567ca87d9a8d5abad65dc4d1ba86d708b4c184e87` |
