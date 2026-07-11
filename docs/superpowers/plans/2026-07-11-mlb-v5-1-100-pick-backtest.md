# MLB Winner Model v5.1 100-Pick Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a deterministic 2025 historical replay that produces 100 graded `Retrospective Daily Top 1` picks for frozen model version `v5.1-BT`, then publish an audited Excel report.

**Architecture:** A Python standard-library pipeline downloads immutable MLB/Statcast snapshots, enforces D-1 feature cutoffs, calculates six frozen v5.1 components, locks daily rankings before outcomes are readable, and evaluates the locked picks. A separate Node.js `@oai/artifact-tool` builder imports the audited CSV outputs into the existing tracking workbook format and exports the final `.xlsx`.

**Tech Stack:** Bundled Python 3 (`unittest`, `urllib`, `csv`, `json`, `hashlib`, `statistics`, `dataclasses`), bundled `numpy` for two-parameter logistic fitting, MLB Stats API, Baseball Savant CSV, bundled Node.js, `@oai/artifact-tool`, Microsoft Excel for final recalculation verification.

---

## File map

- `config/v5_1_bt.json` — frozen dates, weights, subweights, shrinkage constants, thresholds, and tie-break policy.
- `mlb_backtest/config.py` — validates and loads the frozen config.
- `mlb_backtest/models.py` — typed records shared across pipeline stages.
- `mlb_backtest/manifest.py` — immutable raw-file writes and SHA-256 manifests.
- `mlb_backtest/sources.py` — MLB Stats API and Baseball Savant HTTP clients.
- `mlb_backtest/clock.py` — target dates, T-45 locks, D-1 assertions, and void-extension policy.
- `mlb_backtest/normalize.py` — shrinkage, direction-aware z-scores, clipping, and frozen training statistics.
- `mlb_backtest/features/starter.py` — starting-pitcher component.
- `mlb_backtest/features/offense.py` — confirmed-lineup component.
- `mlb_backtest/features/bullpen.py` — bullpen quality and availability component.
- `mlb_backtest/features/context.py` — environment, defense/catcher, schedule/travel components.
- `mlb_backtest/scoring.py` — final 35/25/20/10/5/5 score, probability, Confidence, Uncertainty, Tier, agreement.
- `mlb_backtest/ranking.py` — eligible-candidate filtering and deterministic Daily Top 1/Top 3 ranking.
- `mlb_backtest/locking.py` — outcome-isolated lock files and hashes.
- `mlb_backtest/outcomes.py` — post-lock official result and optional market joins.
- `mlb_backtest/metrics.py` — accuracy, Wilson interval, Brier, log loss, calibration, streaks, and baselines.
- `mlb_backtest/pipeline.py` — stage orchestration and command-line entry point.
- `scripts/build_backtest_workbook.mjs` — artifact-tool report builder.
- `scripts/verify_backtest_workbook.mjs` — workbook schema, formulas, error scan, and render checks.
- `tests/` — unit, leakage, determinism, and end-to-end fixture tests.

## Frozen implementation clarifications

These rules remove remaining implementation discretion before any 2025 outcome is opened:

- **FIP:** `FIP = (13×HR + 3×(BB+HBP) - 2×K) / IP + cFIP`; `cFIP` is calculated by season from all MLB regular-season pitching through D-1 so league FIP equals league ERA. Outs are stored as integers and converted to innings only at the formula boundary.
- **Expected starter innings:** median innings of the previous five starts, clipped to `[3.0,7.0]`; subtract `1.0` when median pitch count is below 70 and `0.5` when rest is fewer than four full days; clip again to `[2.0,7.0]`. With fewer than three prior starts, shrink the median toward `5.0` using `n/(n+3)` and mark a small-sample flag.
- **Batting-order weights:** calculate `PA in slot / all PA` for slots 1-9 using 2022-2024 regular seasons, normalize the nine values to sum to 1, store them in the frozen model card, and never recompute them from 2025.
- **Metric quality:** `0` when the metric is absent; otherwise use its shrinkage reliability `n/(n+k)`, with non-rate metadata quality equal to `1` when source-backed and `0` when absent.
- **Component quality:** the frozen internal-weighted mean of metric qualities. **Overall completeness:** the final `35/25/20/10/5/5` weighted mean of component qualities.
- **Confidence:** `2.5×overall + 2.0×qStarter + 1.5×qOffense + 1.5×qBullpen + 1.5×agreement + 1.0×((2×qEnvironment+qSchedule)/3)`, clipped to `[0,10]`.
- **Small-sample starter:** fewer than 150 career MLB batters faced before D-1; this forces at least Medium uncertainty.
- **Environment fallback:** if training-frozen park factors or recorded pregame weather/roof fields cannot be computed from source-backed rows, their exact internal subcomponent is score 50 and quality 0; no inferred or result-derived replacement is permitted.
- **Defense/catcher fallback:** each missing OAA, framing, or blocking/throwing subcomponent is independently score 50 and quality 0; remaining subweights are not rescaled.
- **Logistic calibration:** minimize unweighted binary cross-entropy on all eligible 2022-2024 regular-season games with L2 penalty `1e-6×beta²`; do not penalize `alpha`; Newton iterations stop at parameter change below `1e-10` or 100 iterations.
- **Higher-season-win% baseline:** use each team's D-1 regular-season winning percentage; ties are missing for this baseline, not forced to home or away.
- **Suspended games:** retain the original official selection date and grade the eventual MLB official winner; if no official final exists, mark VOID and extend the date window.
- **Canonical lock format:** UTF-8 JSON Lines, keys sorted, rows sorted by `(official_date,daily_rank,game_pk)`, floats formatted to 10 decimal places before serialization, newline after every row.
- **HTTP policy:** timeout 90 seconds; retry only 429/500/502/503/504 with delays `1,2,4,8,16` seconds; resume by immutable content hash and fail after the fifth retry.

## Task 1: Freeze and validate the v5.1-BT model card

**Files:**
- Create: `config/v5_1_bt.json`
- Create: `mlb_backtest/__init__.py`
- Create: `mlb_backtest/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing config test**

```python
# tests/test_config.py
import unittest
from mlb_backtest.config import load_config

class ConfigTest(unittest.TestCase):
    def test_frozen_weights_and_dates(self):
        cfg = load_config("config/v5_1_bt.json")
        self.assertEqual(cfg["model_version"], "v5.1-BT")
        self.assertEqual(cfg["initial_start"], "2025-05-01")
        self.assertEqual(cfg["initial_end"], "2025-08-12")
        self.assertEqual(cfg["lock_minutes"], 45)
        self.assertEqual(cfg["weights"], {
            "starter": .35, "offense": .25, "bullpen": .20,
            "environment": .10, "defense": .05, "schedule": .05,
        })
        self.assertAlmostEqual(sum(cfg["weights"].values()), 1.0)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify the import fails**

Run:

```bash
PYTHONPATH=. /Users/taejunoh/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m unittest tests.test_config -v
```

Expected: `ModuleNotFoundError: No module named 'mlb_backtest'`.

- [ ] **Step 3: Add the frozen JSON and loader**

```json
{
  "model_version": "v5.1-BT",
  "protocol_phase": "Backtest",
  "training_start": "2022-01-01",
  "training_end": "2024-12-31",
  "initial_start": "2025-05-01",
  "initial_end": "2025-08-12",
  "graded_target": 100,
  "lock_minutes": 45,
  "weights": {"starter": 0.35, "offense": 0.25, "bullpen": 0.20, "environment": 0.10, "defense": 0.05, "schedule": 0.05},
  "shrinkage_k": {"starter_split": 150, "hitter_split": 150, "recent": 100, "bullpen": 300, "defense": 500},
  "probability_clip": [0.10, 0.90],
  "ranking": ["pick_probability_desc", "confidence_desc", "starter_edge_abs_desc", "scheduled_start_utc_asc", "game_pk_asc"]
}
```

```python
# mlb_backtest/config.py
import json
from pathlib import Path

REQUIRED_COMPONENTS = {"starter", "offense", "bullpen", "environment", "defense", "schedule"}

def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(cfg["weights"]) != REQUIRED_COMPONENTS:
        raise ValueError("component set does not match frozen v5.1 model")
    if abs(sum(cfg["weights"].values()) - 1.0) > 1e-12:
        raise ValueError("component weights must sum to 1")
    if cfg["model_version"] != "v5.1-BT" or cfg["lock_minutes"] != 45:
        raise ValueError("model identity or lock rule changed")
    return cfg
```

- [ ] **Step 4: Run the test and verify it passes**

Expected: `OK` with one passing test.

- [ ] **Step 5: Commit**

```bash
git add config/v5_1_bt.json mlb_backtest/__init__.py mlb_backtest/config.py tests/test_config.py
git commit -m "feat: freeze v5.1 backtest model card"
```

## Task 2: Define pipeline records and immutable raw manifests

**Files:**
- Create: `mlb_backtest/models.py`
- Create: `mlb_backtest/manifest.py`
- Create: `tests/test_manifest.py`

Store payload bytes once at `<root>/<source>/<payload_sha>.raw`. Store each
provenance record at
`<root>/<source>/<payload_sha>.<canonical_query_sha>.manifest.json`, where
`canonical_query_sha` is the SHA-256 of compact UTF-8 JSON with sorted keys.
Manifest identity is deterministic and insertion-order-independent: equivalent
query dictionaries always resolve to the same path, while distinct canonical
queries for the same payload resolve to distinct manifests. This storage rule
does not change any frozen model behavior.

- [ ] **Step 1: Write failing tests for deterministic hashes and write-once behavior**

```python
# tests/test_manifest.py
import tempfile, unittest
from pathlib import Path
from mlb_backtest.manifest import RawStore

class ManifestTest(unittest.TestCase):
    def test_same_payload_has_same_hash_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            first = store.put("mlb_schedule", {"date": "2025-05-01"}, b'{"games":[]}')
            second = store.put("mlb_schedule", {"date": "2025-05-01"}, b'{"games":[]}')
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.data_path.read_bytes(), b'{"games":[]}')
            self.assertEqual(first.row_count, 1)
```

- [ ] **Step 2: Run and verify failure because `RawStore` is absent**

- [ ] **Step 3: Implement typed records and atomic manifest writes**

```python
# mlb_backtest/models.py
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

@dataclass(frozen=True)
class RawArtifact:
    source: str
    sha256: str
    data_path: Path
    manifest_path: Path
    retrieved_at_utc: str
    row_count: int

@dataclass(frozen=True)
class GameClock:
    game_pk: int
    official_date: date
    scheduled_start_utc: datetime
    lock_at_utc: datetime
    feature_as_of: date
```

```python
# mlb_backtest/manifest.py
import hashlib, json, os, tempfile
from datetime import datetime, timezone
from pathlib import Path
from .models import RawArtifact

class RawStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, source: str, query: dict, payload: bytes) -> RawArtifact:
        digest = hashlib.sha256(payload).hexdigest()
        folder = self.root / source
        folder.mkdir(parents=True, exist_ok=True)
        data_path = folder / f"{digest}.raw"
        canonical_query = json.dumps(
            query, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        query_digest = hashlib.sha256(canonical_query).hexdigest()
        manifest_path = folder / f"{digest}.{query_digest}.manifest.json"
        if not data_path.exists():
            fd, tmp = tempfile.mkstemp(dir=folder)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp, data_path)
        if data_path.read_bytes() != payload:
            raise RuntimeError("immutable raw artifact hash collision")
        manifest = {
            "source": source, "query": query, "sha256": digest,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "row_count": max(1, payload.count(b"\n")), "schema_version": 1,
        }
        if not manifest_path.exists():
            manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")
        return RawArtifact(source, digest, data_path, manifest_path, manifest["retrieved_at_utc"], manifest["row_count"])
```

- [ ] **Step 4: Run `tests.test_manifest` and verify `OK`**

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/models.py mlb_backtest/manifest.py tests/test_manifest.py
git commit -m "feat: add immutable raw data manifests"
```

## Task 3: Add source clients and prove the 100-date schedule window

**Files:**
- Create: `mlb_backtest/sources.py`
- Create: `mlb_backtest/clock.py`
- Create: `tests/fixtures/schedule_2025_05_01.json`
- Create: `tests/test_clock.py`
- Create: `tests/test_sources.py`

- [ ] **Step 1: Add fixture-based failing tests**

```python
# tests/test_clock.py
import unittest
from datetime import datetime, timezone
from mlb_backtest.clock import make_game_clock, assert_feature_dates

class ClockTest(unittest.TestCase):
    def test_t45_and_d_minus_one(self):
        clock = make_game_clock(778498, "2025-05-01", "2025-05-01T23:10:00Z", 45)
        self.assertEqual(clock.lock_at_utc, datetime(2025, 5, 1, 22, 25, tzinfo=timezone.utc))
        self.assertEqual(clock.feature_as_of.isoformat(), "2025-04-30")

    def test_same_day_feature_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "same-day leakage"):
            assert_feature_dates("2025-05-01", ["2025-04-30", "2025-05-01"])
```

```python
# tests/test_sources.py
import unittest
from mlb_backtest.sources import parse_schedule

class SourceTest(unittest.TestCase):
    def test_regular_season_game_is_normalized(self):
        games = parse_schedule("tests/fixtures/schedule_2025_05_01.json")
        self.assertGreater(len(games), 0)
        self.assertEqual(games[0]["game_type"], "R")
        self.assertIsInstance(games[0]["game_pk"], int)
```

- [ ] **Step 2: Run and verify both modules are missing**

- [ ] **Step 3: Implement standard-library HTTP and clock functions**

```python
# mlb_backtest/sources.py
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MLB_BASE = "https://statsapi.mlb.com/api/v1"

def fetch_bytes(url: str, timeout: int = 90) -> bytes:
    req = Request(url, headers={"User-Agent": "mlb-v5.1-backtest/1.0"})
    with urlopen(req, timeout=timeout) as response:
        return response.read()

def schedule_url(start: str, end: str) -> str:
    return f"{MLB_BASE}/schedule?{urlencode({'sportId': 1, 'gameType': 'R', 'startDate': start, 'endDate': end, 'hydrate': 'probablePitcher,lineups,venue'})}"

def parse_schedule(path: str) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [{
        "game_pk": g["gamePk"], "official_date": d["date"], "game_type": g["gameType"],
        "scheduled_start_utc": g["gameDate"], "status": g["status"]["detailedState"],
        "away_team": g["teams"]["away"]["team"]["name"],
        "home_team": g["teams"]["home"]["team"]["name"],
    } for d in payload["dates"] for g in d["games"] if g["gameType"] == "R"]
```

```python
# mlb_backtest/clock.py
from datetime import date, datetime, timedelta, timezone
from .models import GameClock

def make_game_clock(game_pk: int, official_date: str, start_utc: str, lock_minutes: int) -> GameClock:
    start = datetime.fromisoformat(start_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    day = date.fromisoformat(official_date)
    return GameClock(game_pk, day, start, start - timedelta(minutes=lock_minutes), day - timedelta(days=1))

def assert_feature_dates(target_date: str, source_dates: list[str]) -> None:
    target = date.fromisoformat(target_date)
    if any(date.fromisoformat(value) >= target for value in source_dates):
        raise ValueError("same-day leakage detected")
```

- [ ] **Step 4: Add a network smoke command that stores, never mutates, the 2025 schedule**

Run the client for `2025-05-01` through `2025-08-12`; assert the normalized schedule contains exactly 100 distinct dates and print the first/last date. Expected: `100 2025-05-01 2025-08-12`.

- [ ] **Step 5: Run all Task 3 tests and commit**

```bash
git add mlb_backtest/sources.py mlb_backtest/clock.py tests/fixtures/schedule_2025_05_01.json tests/test_clock.py tests/test_sources.py
git commit -m "feat: add historical schedule and replay clocks"
```

## Task 4: Implement normalization and pre-2025 probability calibration

**Files:**
- Create: `mlb_backtest/normalize.py`
- Create: `tests/test_normalize.py`

- [ ] **Step 1: Write failing numeric golden tests**

```python
# tests/test_normalize.py
import math, unittest
from mlb_backtest.normalize import shrink, metric_score, fit_logistic, sigmoid

class NormalizeTest(unittest.TestCase):
    def test_shrink_and_direction(self):
        self.assertAlmostEqual(shrink(0.320, 150, 0.300, 150), 0.310)
        self.assertAlmostEqual(metric_score(0.310, 0.300, 0.010, -1), 30.0)

    def test_two_parameter_logistic_fit(self):
        alpha, beta = fit_logistic([-2, -1, 1, 2], [0, 0, 1, 1])
        self.assertLess(sigmoid(alpha + beta * -1), 0.5)
        self.assertGreater(sigmoid(alpha + beta * 1), 0.5)
```

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement exact math with clipped Newton updates**

```python
# mlb_backtest/normalize.py
import math
import numpy as np

def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

def shrink(observed: float, n: float, mean: float, k: float) -> float:
    r = n / (n + k) if n > 0 else 0.0
    return r * observed + (1.0 - r) * mean

def metric_score(value: float, mean: float, sd: float, direction: int) -> float:
    if sd <= 0:
        raise ValueError("training standard deviation must be positive")
    z = direction * clip((value - mean) / sd, -2.5, 2.5)
    return clip(50.0 + 20.0 * z, 0.0, 100.0)

def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-clip(x, -30.0, 30.0)))

def fit_logistic(x_values: list[float], y_values: list[int], iterations: int = 50) -> tuple[float, float]:
    x = np.column_stack([np.ones(len(x_values)), np.asarray(x_values, dtype=float)])
    y = np.asarray(y_values, dtype=float)
    theta = np.zeros(2)
    for _ in range(iterations):
        p = 1.0 / (1.0 + np.exp(-np.clip(x @ theta, -30, 30)))
        w = np.maximum(p * (1 - p), 1e-8)
        hessian = x.T @ (x * w[:, None])
        gradient = x.T @ (y - p)
        theta += np.linalg.solve(hessian + np.eye(2) * 1e-8, gradient)
    return float(theta[0]), float(theta[1])
```

- [ ] **Step 4: Add a test that rejects any calibration row dated in 2025**

The public calibration entry point must accept dated rows and raise `ValueError("calibration data crosses frozen boundary")` when a date is later than `2024-12-31`.

- [ ] **Step 5: Run and commit**

```bash
git add mlb_backtest/normalize.py tests/test_normalize.py
git commit -m "feat: add frozen normalization and calibration"
```

## Task 5: Build starter and confirmed-lineup components

**Files:**
- Create: `mlb_backtest/features/__init__.py`
- Create: `mlb_backtest/features/starter.py`
- Create: `mlb_backtest/features/offense.py`
- Create: `tests/test_starter_features.py`
- Create: `tests/test_offense_features.py`

- [ ] **Step 1: Write failing weighted-score tests**

```python
# tests/test_starter_features.py
import unittest
from mlb_backtest.features.starter import starter_score

class StarterTest(unittest.TestCase):
    def test_frozen_subweights(self):
        scores = {"season_xwoba": 80, "k_minus_bb": 70, "fip": 60, "last5_xwoba": 50, "expected_ip": 40, "velo_change": 30}
        self.assertAlmostEqual(starter_score(scores), 64.0)
```

```python
# tests/test_offense_features.py
import unittest
from mlb_backtest.features.offense import offense_score

class OffenseTest(unittest.TestCase):
    def test_nine_hitters_required(self):
        with self.assertRaisesRegex(ValueError, "nine starting hitters"):
            offense_score([{"player_id": n}] for n in range(8), {})
```

- [ ] **Step 2: Run and verify missing modules**

- [ ] **Step 3: Implement frozen weighted sums and lineup validation**

```python
# mlb_backtest/features/starter.py
WEIGHTS = {"season_xwoba": .35, "k_minus_bb": .20, "fip": .15, "last5_xwoba": .15, "expected_ip": .10, "velo_change": .05}

def starter_score(metric_scores: dict[str, float]) -> float:
    if set(metric_scores) != set(WEIGHTS):
        raise ValueError("starter metric set changed")
    return sum(WEIGHTS[name] * metric_scores[name] for name in WEIGHTS)
```

```python
# mlb_backtest/features/offense.py
WEIGHTS = {"platoon_xwoba": .50, "last14_xwoba": .20, "bb_minus_k": .15, "barrel_rate": .10, "iso": .05}

def offense_score(lineup: list[dict], metric_scores: dict[str, float]) -> float:
    if len(lineup) != 9 or len({p["player_id"] for p in lineup}) != 9:
        raise ValueError("exactly nine starting hitters are required")
    if set(metric_scores) != set(WEIGHTS):
        raise ValueError("offense metric set changed")
    return sum(WEIGHTS[name] * metric_scores[name] for name in WEIGHTS)
```

- [ ] **Step 4: Add D-1 fixture tests for recent-five-start and recent-14-day windows**

Use events on `2025-04-15`, `2025-04-30`, and `2025-05-01`; assert the May 1 event is rejected and excluded for a May 1 target.

- [ ] **Step 5: Run and commit**

```bash
git add mlb_backtest/features tests/test_starter_features.py tests/test_offense_features.py
git commit -m "feat: add starter and lineup components"
```

## Task 6: Build bullpen, environment, defense, and schedule components

**Files:**
- Create: `mlb_backtest/features/bullpen.py`
- Create: `mlb_backtest/features/context.py`
- Create: `tests/test_bullpen_features.py`
- Create: `tests/test_context_features.py`

- [ ] **Step 1: Write failing availability and travel tests**

```python
# tests/test_bullpen_features.py
import unittest
from mlb_backtest.features.bullpen import reliever_availability

class BullpenTest(unittest.TestCase):
    def test_availability_thresholds(self):
        self.assertEqual(reliever_availability([25, 0, 0]), 0.0)
        self.assertEqual(reliever_availability([18, 0, 0]), 0.5)
        self.assertEqual(reliever_availability([0, 0, 0]), 1.0)
        self.assertEqual(reliever_availability([10, 10, 10]), 0.0)
```

```python
# tests/test_context_features.py
import unittest
from mlb_backtest.features.context import schedule_score

class ContextTest(unittest.TestCase):
    def test_schedule_adjustments_are_clipped(self):
        self.assertEqual(schedule_score(rest_day=True, home_days=3, prior_innings=9, miles=0, time_zones=0, doubleheader=False), 67)
        self.assertEqual(schedule_score(rest_day=False, home_days=0, prior_innings=12, miles=1200, time_zones=3, doubleheader=True), 14)
```

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement exact availability and schedule rules**

```python
# mlb_backtest/features/bullpen.py
def reliever_availability(pitches_by_prior_day: list[int]) -> float:
    d1, d2, d3 = (pitches_by_prior_day + [0, 0, 0])[:3]
    if d1 >= 25 or d1 + d2 >= 40 or all(v > 0 for v in (d1, d2, d3)):
        return 0.0
    if 15 <= d1 <= 24 or 30 <= d1 + d2 <= 39:
        return 0.5
    return 1.0
```

```python
# mlb_backtest/features/context.py
def schedule_score(*, rest_day: bool, home_days: int, prior_innings: int, miles: float, time_zones: int, doubleheader: bool) -> float:
    score = 50 + 12 * rest_day + 5 * (home_days >= 3) - 12 * (prior_innings >= 10)
    score -= 10 * (miles >= 1000 and not rest_day)
    score -= 6 * (time_zones >= 2 and not rest_day)
    score -= 8 * doubleheader
    return max(0.0, min(100.0, float(score)))
```

- [ ] **Step 4: Add neutral-missing tests**

Assert missing weather, OAA, and framing values each return score `50` and quality `0`, without redistributing their frozen internal weights.

- [ ] **Step 5: Run and commit**

```bash
git add mlb_backtest/features/bullpen.py mlb_backtest/features/context.py tests/test_bullpen_features.py tests/test_context_features.py
git commit -m "feat: add bullpen and context components"
```

## Task 7: Implement final scoring, Confidence, Uncertainty, Tier, and ranking

**Files:**
- Create: `mlb_backtest/scoring.py`
- Create: `mlb_backtest/ranking.py`
- Create: `tests/test_scoring.py`
- Create: `tests/test_ranking.py`

- [ ] **Step 1: Write failing golden tests**

```python
# tests/test_scoring.py
import unittest
from mlb_backtest.scoring import component_edge, tier

class ScoringTest(unittest.TestCase):
    def test_original_weights_are_preserved(self):
        home = {"starter": 60, "offense": 60, "bullpen": 60, "environment": 60, "defense": 60, "schedule": 60}
        away = {name: 50 for name in home}
        self.assertAlmostEqual(component_edge(home, away), 10.0)

    def test_tier_thresholds(self):
        self.assertEqual(tier(.68, 9.0, "Low"), "S")
        self.assertEqual(tier(.63, 8.5, "Medium"), "A")
        self.assertEqual(tier(.58, 8.0, "Low"), "B")
        self.assertEqual(tier(.53, 9.5, "Low"), "Toss-up")
        self.assertEqual(tier(.70, 9.5, "High"), "Toss-up")
```

```python
# tests/test_ranking.py
import unittest
from mlb_backtest.ranking import rank_daily

class RankingTest(unittest.TestCase):
    def test_ties_follow_frozen_order(self):
        rows = [
            {"game_pk": 2, "pick_probability": .60, "confidence": 8.5, "starter_edge_abs": 5, "scheduled_start_utc": "2025-05-01T23:00:00Z"},
            {"game_pk": 1, "pick_probability": .60, "confidence": 8.5, "starter_edge_abs": 5, "scheduled_start_utc": "2025-05-01T23:00:00Z"},
        ]
        ranked = rank_daily(rows)
        self.assertEqual([row["game_pk"] for row in ranked], [1, 2])
        self.assertEqual(ranked[0]["daily_rank"], 1)
```

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement scoring and ranking**

```python
# mlb_backtest/scoring.py
import math
WEIGHTS = {"starter": .35, "offense": .25, "bullpen": .20, "environment": .10, "defense": .05, "schedule": .05}

def component_edge(home: dict[str, float], away: dict[str, float]) -> float:
    return sum(WEIGHTS[k] * (home[k] - away[k]) for k in WEIGHTS)

def home_probability(edge: float, alpha: float, beta: float) -> float:
    raw = 1 / (1 + math.exp(-max(-30, min(30, alpha + beta * edge / 10))))
    return max(.10, min(.90, raw))

def tier(probability: float, confidence: float, uncertainty: str) -> str:
    if uncertainty == "High" or probability < .54: return "Toss-up"
    if probability >= .68 and confidence >= 9.0: return "S"
    if probability >= .63 and confidence >= 8.5: return "A"
    if probability >= .58 and confidence >= 8.0: return "B"
    return "Lean"

def confidence_score(overall: float, starter: float, offense: float, bullpen: float, agreement: float, environment: float, schedule: float) -> float:
    value = 2.5*overall + 2.0*starter + 1.5*offense + 1.5*bullpen + 1.5*agreement + ((2*environment + schedule)/3)
    return max(0.0, min(10.0, value))

def uncertainty(overall: float, critical_failure: bool, starter_career_bf: int, unresolved_context: bool) -> str:
    if critical_failure or overall < .75:
        return "High"
    if overall < .90 or starter_career_bf < 150 or unresolved_context:
        return "Medium"
    return "Low"

def full_agreement(edges: dict[str, float], predicted_side: str, uncertainty_label: str) -> str:
    sign = 1 if predicted_side == "home" else -1
    agreed = all(sign * edges[name] > 0 for name in ("starter", "offense", "bullpen"))
    return "Yes" if agreed and uncertainty_label != "High" else "No"
```

```python
# mlb_backtest/ranking.py
def rank_daily(rows: list[dict]) -> list[dict]:
    ranked = sorted(rows, key=lambda r: (-r["pick_probability"], -r["confidence"], -r["starter_edge_abs"], r["scheduled_start_utc"], r["game_pk"]))
    return [dict(row, daily_rank=i, top1="Yes" if i == 1 else "No", top3="Yes" if i <= 3 else "No") for i, row in enumerate(ranked, 1)]
```

- [ ] **Step 4: Add exact Confidence and uncertainty boundary tests**

```python
from mlb_backtest.scoring import confidence_score, uncertainty, full_agreement

def test_confidence_and_uncertainty_boundaries(self):
    self.assertEqual(confidence_score(1, 1, 1, 1, 1, 1, 1), 10.0)
    self.assertEqual(confidence_score(0, 0, 0, 0, 0, 0, 0), 0.0)
    self.assertEqual(uncertainty(.7499, False, 500, False), "High")
    self.assertEqual(uncertainty(.90, False, 149, False), "Medium")
    self.assertEqual(uncertainty(.90, False, 150, False), "Low")
    edges = {"starter": 2.0, "offense": 1.0, "bullpen": .5}
    self.assertEqual(full_agreement(edges, "home", "Low"), "Yes")
    self.assertEqual(full_agreement(dict(edges, bullpen=-.5), "home", "Low"), "No")
    self.assertEqual(full_agreement(edges, "home", "High"), "No")
```

- [ ] **Step 5: Run and commit**

```bash
git add mlb_backtest/scoring.py mlb_backtest/ranking.py tests/test_scoring.py tests/test_ranking.py
git commit -m "feat: add frozen scoring and daily ranking"
```

## Task 8: Enforce lock-before-outcome stage isolation

**Files:**
- Create: `mlb_backtest/locking.py`
- Create: `mlb_backtest/outcomes.py`
- Create: `tests/test_locking.py`
- Create: `tests/test_leakage.py`

- [ ] **Step 1: Write failing isolation tests**

```python
# tests/test_leakage.py
import tempfile, unittest
from pathlib import Path
from mlb_backtest.locking import lock_predictions
from mlb_backtest.outcomes import join_outcomes

class LeakageTest(unittest.TestCase):
    def test_outcomes_cannot_join_before_lock_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "lock manifest"):
                join_outcomes(Path(td) / "missing.json", [{"game_pk": 1, "actual_winner": "Home"}])

    def test_locked_hash_is_order_stable(self):
        rows = [{"game_pk": 2, "daily_rank": 2}, {"game_pk": 1, "daily_rank": 1}]
        with tempfile.TemporaryDirectory() as td:
            one = lock_predictions(rows, Path(td) / "one")
            two = lock_predictions(list(reversed(rows)), Path(td) / "two")
            self.assertEqual(one["sha256"], two["sha256"])
```

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement canonical JSONL locking**

Canonicalize rows by `(official_date, daily_rank, game_pk)`, serialize with sorted keys and compact separators, hash bytes, write `predictions.jsonl` and `lock_manifest.json`, and refuse overwrite when an existing manifest hash differs.

```python
payload = b"\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() for row in ordered) + b"\n"
digest = hashlib.sha256(payload).hexdigest()
```

- [ ] **Step 4: Implement post-lock outcome joining**

Require a valid manifest whose SHA-256 matches `predictions.jsonl`; reject outcome rows whose `game_pk` is absent; never modify prediction columns; write a new evaluated file.

- [ ] **Step 5: Run and commit**

```bash
git add mlb_backtest/locking.py mlb_backtest/outcomes.py tests/test_locking.py tests/test_leakage.py
git commit -m "feat: isolate locked predictions from outcomes"
```

## Task 9: Add metrics and 100-graded-pick completion rules

**Files:**
- Create: `mlb_backtest/metrics.py`
- Create: `tests/test_metrics.py`

- [ ] **Step 1: Write failing metric golden tests**

```python
# tests/test_metrics.py
import math, unittest
from mlb_backtest.metrics import brier, log_loss, wilson, max_losing_streak

class MetricsTest(unittest.TestCase):
    def test_metrics(self):
        self.assertAlmostEqual(brier(.60, False), .36)
        self.assertAlmostEqual(log_loss(.70, True), -math.log(.70))
        low, high = wilson(20, 30)
        self.assertAlmostEqual(low, .4877972581, places=9)
        self.assertEqual(max_losing_streak([True, False, False, True, False]), 2)
```

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement bounded probability metrics and Wilson 95% interval**

Use probability clipping `[0.001,0.999]` for log loss, return blank calibration rates for empty bins, exclude `Pick'em` and missing odds from paired-market denominators, and keep void picks out of all graded denominators.

- [ ] **Step 4: Add completion tests**

Create 99 graded plus one void and assert the date-extension function requests another date; create 100 graded and assert completion. Assert exactly one Top 1 per selection date.

- [ ] **Step 5: Run and commit**

```bash
git add mlb_backtest/metrics.py tests/test_metrics.py
git commit -m "feat: add backtest metrics and completion rules"
```

## Task 10: Orchestrate acquisition, calibration, replay, lock, and evaluation

**Files:**
- Create: `mlb_backtest/pipeline.py`
- Create: `tests/fixtures/mini_replay/`
- Create: `tests/test_pipeline.py`

- [ ] **Step 1: Write a failing three-date end-to-end fixture test**

```python
# tests/test_pipeline.py
import tempfile, unittest
from pathlib import Path
from mlb_backtest.pipeline import run_fixture_replay

class PipelineTest(unittest.TestCase):
    def test_predictions_lock_before_results(self):
        with tempfile.TemporaryDirectory() as td:
            result = run_fixture_replay(Path("tests/fixtures/mini_replay"), Path(td))
            self.assertEqual(result["selection_dates"], 3)
            self.assertEqual(result["top1_count"], 3)
            self.assertTrue((Path(td) / "locked/lock_manifest.json").exists())
            self.assertTrue((Path(td) / "reports/summary.json").exists())
            self.assertLess(result["lock_mtime_ns"], result["outcome_mtime_ns"])
```

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement explicit CLI stages**

```text
python -m mlb_backtest.pipeline fetch --config config/v5_1_bt.json --data-dir data/v5_1_bt
python -m mlb_backtest.pipeline calibrate --config config/v5_1_bt.json --data-dir data/v5_1_bt
python -m mlb_backtest.pipeline features --config config/v5_1_bt.json --data-dir data/v5_1_bt
python -m mlb_backtest.pipeline lock --config config/v5_1_bt.json --data-dir data/v5_1_bt
python -m mlb_backtest.pipeline outcomes --config config/v5_1_bt.json --data-dir data/v5_1_bt
python -m mlb_backtest.pipeline report --config config/v5_1_bt.json --data-dir data/v5_1_bt
```

Each command must read only its declared upstream directories. `features` and `lock` must fail if an `outcomes/` path is passed or imported.

- [ ] **Step 4: Run the fixture test twice and compare locked SHA-256 values**

Expected: both runs produce the same `lock_manifest.json.sha256`.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/pipeline.py tests/fixtures/mini_replay tests/test_pipeline.py
git commit -m "feat: orchestrate deterministic historical replay"
```

## Task 11: Run the real 2022-2025 data pipeline and audit locked picks

**Files:**
- Create: `data/v5_1_bt/.gitignore`
- Create: `audit/v5_1_bt/model_card.json`
- Create: `audit/v5_1_bt/lock_manifest.json`
- Create: `audit/v5_1_bt/manual_audit.md`

- [ ] **Step 1: Run the complete unit suite before network acquisition**

```bash
PYTHONPATH=. /Users/taejunoh/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m unittest discover -s tests -v
```

Expected: all tests pass with zero failures/errors.

- [ ] **Step 2: Fetch and manifest training/backtest raw data**

Run the `fetch` stage. Retry transient HTTP 429/5xx responses with bounded exponential delays of 1, 2, 4, 8, and 16 seconds; never replace a hash-addressed raw file.

- [ ] **Step 3: Calibrate and freeze the model card**

Write training means, standard deviations, lineup-slot weights, `alpha`, and `beta` to `audit/v5_1_bt/model_card.json`. Assert its maximum training date is `2024-12-31` or earlier and record its SHA-256 in the lock manifest.

- [ ] **Step 4: Generate and lock all daily candidate rankings**

Assert initial dates equal `2025-05-01..2025-08-12`, every candidate feature date is D-1 or earlier, every non-void date has exactly one Top 1, and market fields are absent.

- [ ] **Step 5: Manually audit three fixed dates**

Audit `2025-05-01`, `2025-06-15`, and `2025-08-12`. Record source hashes, starters, nine hitters, component scores, selected winner/probability, daily rank, and the later result join in `manual_audit.md`.

- [ ] **Step 6: Join outcomes and extend dates only for void picks**

Continue beyond August 12 only until 100 graded Top 1 picks exist. Print `graded=100`, `void=<count>`, and the actual last selection date.

- [ ] **Step 7: Commit only reproducibility metadata, not bulk raw data**

```bash
git add data/v5_1_bt/.gitignore audit/v5_1_bt
git commit -m "data: lock v5.1 100-pick backtest"
```

## Task 12: Build the audited Excel report

**Files:**
- Create: `scripts/build_backtest_workbook.mjs`
- Create: `scripts/verify_backtest_workbook.mjs`
- Modify: `config/v5_1_bt.json` only if an output path key is needed before the real run; do not change any model rule.
- Output: `/Users/taejunoh/Documents/Codex/2026-07-11/files-mentioned-by-the-user-mlb/outputs/mlb_v5_1_100_pick_backtest.xlsx`

- [ ] **Step 1: Read the spreadsheet skill's required API and style references**

Read `API_QUICK_START.md` and `style_guidelines.md` completely. Load the bundled workspace dependencies and create the required local `node_modules` symlink.

- [ ] **Step 2: Write the failing workbook verifier first**

The verifier must import the expected output and assert sheets:

```js
const expected = ["README", "MODEL_CARD", "DAILY_RANKINGS", "TOP1_100", "METRICS", "CALIBRATION", "DATA_QUALITY", "SOURCES"];
```

It must assert `TOP1_100` contains exactly 100 graded rows, all rows have `Model Version=v5.1-BT`, exactly one rank 1 per selection date, and no formula error matches `#REF!|#DIV/0!|#VALUE!|#NAME?|#N/A`.

- [ ] **Step 3: Run verifier and confirm it fails because the workbook is absent**

- [ ] **Step 4: Implement the artifact-tool workbook builder**

Use only `@oai/artifact-tool` for workbook authoring. Import locked predictions, evaluated outcomes, summary metrics, calibration bins, data-quality counts, and source manifests. Match the existing navy/blue/yellow/gray/green tracking workbook conventions. Keep raw predictions as typed dates/numbers/percentages, formula-drive displayed W/L and score fields where practical, freeze headers when supported, and add filters/tables.

- [ ] **Step 5: Run builder and verifier**

```bash
NODE_OPTIONS=--max-old-space-size=8192 /Users/taejunoh/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node scripts/build_backtest_workbook.mjs
NODE_OPTIONS=--max-old-space-size=8192 /Users/taejunoh/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node scripts/verify_backtest_workbook.mjs
```

Expected: workbook verifier reports all schema/value/error assertions passing.

- [ ] **Step 6: Render and inspect all eight sheets**

Render the used range of every sheet. Confirm headers, percentages, long source URLs, 100-row table, calibration bins, and metric cards are visible and not clipped.

- [ ] **Step 7: Commit**

```bash
git add scripts/build_backtest_workbook.mjs scripts/verify_backtest_workbook.mjs
git commit -m "feat: publish v5.1 backtest workbook"
```

## Task 13: Final cross-check in Microsoft Excel

**Files:**
- Verify: `/Users/taejunoh/Documents/Codex/2026-07-11/files-mentioned-by-the-user-mlb/outputs/mlb_v5_1_100_pick_backtest.xlsx`

- [ ] **Step 1: Run the complete Python and Node verification suites fresh**

Expected: zero failures and zero workbook formula-error matches.

- [ ] **Step 2: Validate the XLSX package**

Run `unzip -t` and `xmllint --noout` against every XML part. Expected: no ZIP or XML errors.

- [ ] **Step 3: Open the workbook in Microsoft Excel**

Confirm no recovery dialog appears. Force a full calculation and verify the displayed Top 1 count is 100, W+L is 100, and headline accuracy/Brier/log-loss values equal `reports/summary.json`.

- [ ] **Step 4: Verify anti-leakage evidence one final time**

Check the workbook's `MODEL_CARD` and `SOURCES` sheets show training end no later than 2024-12-31, the lock-manifest hash, the reconstructed-lineup limitation, and the `Retrospective Daily Top 1` label.

- [ ] **Step 5: Record final verification commit**

```bash
git add audit/v5_1_bt/manual_audit.md
git commit -m "test: verify v5.1 100-pick backtest"
```
