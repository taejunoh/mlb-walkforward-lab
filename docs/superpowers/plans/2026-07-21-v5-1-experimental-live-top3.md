# v5.1 Experimental Live Daily Top 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver one local command that acquires result-free current MLB evidence, scores today's complete pregame candidates with the frozen v5.1 card, and writes independent Verified and Preliminary Top 3 reports.

**Architecture:** Add a new experimental-live path instead of routing through the historical stage pipeline. Keep source observation, D-1 history, participant adaptation, scoring/ranking, and rendering in focused modules; reuse audited pure parsers and scoring math only where their historical assumptions do not leak into live provenance.

**Tech Stack:** Python 3.11 standard library, existing `mlb_backtest` modules, JSON/JSON Schema, `unittest`, Ruff, SHA-256, MLB Stats API, Baseball Savant CSV.

**Governing design:** `docs/superpowers/specs/2026-07-21-v5-1-experimental-live-top3-design.md`

---

## File map

- Create `mlb_backtest/live_top3_contracts.py`: immutable result types, status constants, canonical JSON, exact validation.
- Create `schemas/experimental_daily_top3.schema.json`: final run-payload schema.
- Create `mlb_backtest/live_model_card.py`: exact frozen-card authentication and typed projection.
- Create `mlb_backtest/live_sources.py`: current schedule/boxscore observation, whitelisted result-free projection, raw receipt bindings.
- Create `mlb_backtest/live_history.py`: verified current-season D-1 schedule, Statcast, ERA, career-BF cache and bundle.
- Create `mlb_backtest/live_features.py`: live participant adaptation and shared feature construction.
- Create `mlb_backtest/live_top3_service.py`: classification, scoring, independent ranking, run assembly.
- Create `mlb_backtest/daily_top3.py`: one-command CLI, report persistence, terminal rendering.
- Create focused tests matching each module plus `tests/test_daily_top3.py`.
- Add frozen network fixtures under `tests/fixtures/live_top3/`.
- Modify `mlb_backtest/feature_engine.py` only to extract a participant-policy-neutral feature-construction core while preserving the historical public function byte-for-byte in behavior.
- Modify `mlb_backtest/scoring_prep.py` only if a narrow public helper is needed to convert one already-scored raw row into probability inputs; do not change historical results.
- Create `docs/experimental_live_top3.md`: operator instructions and limitations.

Protected duplicate files whose names end in ` 2` are outside every task and must never be staged or edited.

### Task 1: Freeze live contracts and canonical output

**Files:**
- Create: `mlb_backtest/live_top3_contracts.py`
- Create: `schemas/experimental_daily_top3.schema.json`
- Create: `tests/test_live_top3_contracts.py`

- [ ] **Step 1: Write failing contract tests**

```python
class LiveTop3ContractTests(unittest.TestCase):
    def test_candidate_states_are_exact_and_mutually_exclusive(self):
        self.assertEqual(
            live_top3_contracts.CANDIDATE_STATES,
            frozenset({"VERIFIED", "PRELIMINARY", "UNSCORED", "EXCLUDED"}),
        )

    def test_canonical_json_rejects_nonfinite_values(self):
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            live_top3_contracts.canonical_json_bytes({"p": float("nan")})

    def test_run_payload_rejects_ranked_unscored_row(self):
        payload = valid_run_payload()
        payload["unscored_games"][0]["rank"] = 1
        with self.assertRaisesRegex(ValueError, "unscored"):
            live_top3_contracts.validate_run_payload(payload)
```

- [ ] **Step 2: Run RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_top3_contracts -v
```

Expected: import failure because `live_top3_contracts` does not exist.

- [ ] **Step 3: Implement exact immutable types and validation**

Define these public interfaces:

```python
MODEL_LABEL = "v5.1-EXPERIMENTAL-LIVE"
CANDIDATE_STATES = frozenset({"VERIFIED", "PRELIMINARY", "UNSCORED", "EXCLUDED"})
REQUIRED_RUN_KEYS = frozenset({
    "schema_version", "model_label", "target_date", "observed_at_utc",
    "history_cutoff_date", "model_card_sha256", "source_bindings",
    "verified_top3", "preliminary_top3", "unscored_games",
    "excluded_games", "warnings", "content_sha256",
})

class IntegrityError(ValueError):
    """Authenticated input or output differs from the frozen contract."""

@dataclass(frozen=True)
class SourceBinding:
    source: str
    url: str
    final_url: str
    observed_at_utc: str
    http_status: int
    content_type: str | None
    etag: str | None
    last_modified: str | None
    sha256: str
    byte_length: int
    payload_relative: Path
    manifest_sha256: str

@dataclass(frozen=True)
class LiveHistoryBundle:
    target_date: str
    cutoff_date: str
    feature_history: object
    league_era_by_asof: Mapping[str, Mapping[str, object]]
    team_code_by_name: Mapping[str, str]
    source_bindings: tuple[SourceBinding, ...]

def canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be finite JSON") from exc

def validate_target_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("target date must be canonical YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("target date must be canonical YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("target date must be canonical YYYY-MM-DD")
    return value

def validate_run_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != REQUIRED_RUN_KEYS:
        raise ValueError("run payload keys differ")
    safe = dict(value)
    if safe["schema_version"] != 1 or safe["model_label"] != MODEL_LABEL:
        raise ValueError("run payload identity differs")
    validate_target_date(safe["target_date"])
    validate_target_date(safe["history_cutoff_date"])
    _validate_utc(safe["observed_at_utc"], "observed_at_utc")
    _validate_sha(safe["model_card_sha256"], "model_card_sha256")
    _validate_ranked(safe["verified_top3"], "VERIFIED")
    _validate_ranked(safe["preliminary_top3"], "PRELIMINARY")
    _validate_unranked(safe["unscored_games"], "UNSCORED")
    _validate_unranked(safe["excluded_games"], "EXCLUDED")
    _validate_unique_game_population(safe)
    _validate_content_sha(safe)
    canonical_json_bytes(safe)
    return safe
```

Implement the named private validators in the same module. They enforce canonical UTC, 64-character lowercase hashes, independent ranks `1..N`, at most three ranked rows, no duplicated `gamePk` across populations, no probability/rank on unscored rows, exact state-to-array agreement, and recomputation of `content_sha256` with that field omitted.

- [ ] **Step 4: Add the JSON Schema and cross-check tests**

The schema must set `additionalProperties: false` at every object boundary and require these top-level fields:

```json
[
  "schema_version", "model_label", "target_date", "observed_at_utc",
  "history_cutoff_date", "model_card_sha256", "source_bindings",
  "verified_top3", "preliminary_top3", "unscored_games",
  "excluded_games", "warnings", "content_sha256"
]
```

Test `jsonschema.validate()` against one valid fixture and mutations for an unknown field, missing digest, ranked unscored row, and more than three selected rows.

- [ ] **Step 5: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_top3_contracts -v
ruff check mlb_backtest/live_top3_contracts.py tests/test_live_top3_contracts.py
git diff --check
git add mlb_backtest/live_top3_contracts.py tests/test_live_top3_contracts.py schemas/experimental_daily_top3.schema.json
git commit -m "feat: define experimental live top3 contracts"
```

### Task 2: Authenticate the frozen v5.1 card without historical date limits

**Files:**
- Create: `mlb_backtest/live_model_card.py`
- Create: `tests/test_live_model_card.py`
- Read only: `audit/v5_1_bt/model_card.json`

- [ ] **Step 1: Write failing digest and projection tests**

```python
EXPECTED = "9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759"

class LiveModelCardTests(unittest.TestCase):
    def test_loads_exact_frozen_projection(self):
        card = load_frozen_live_card(ROOT / "audit/v5_1_bt/model_card.json")
        self.assertEqual(card.file_sha256, EXPECTED)
        self.assertEqual(card.model_version, "v5.1-BT")
        self.assertEqual(set(card.slot_weights), set(range(1, 10)))
        self.assertAlmostEqual(sum(card.slot_weights.values()), 1.0)

    def test_rejects_one_byte_tampering(self):
        path = self.path / "card.json"
        path.write_bytes(CARD.read_bytes().replace(b'"beta": 0.612', b'"beta": 0.613'))
        with self.assertRaisesRegex(ValueError, "digest"):
            load_frozen_live_card(path)
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_model_card -v
```

Expected: missing module/function failure.

- [ ] **Step 3: Implement the typed loader**

```python
EXPECTED_MODEL_CARD_SHA256 = (
    "9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759"
)

@dataclass(frozen=True)
class FrozenLiveCard:
    model_version: str
    file_sha256: str
    normalization: Mapping[str, object]
    slot_weights: Mapping[int, float]
    alpha: float
    beta: float

def load_frozen_live_card(path: str | Path) -> FrozenLiveCard:
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_MODEL_CARD_SHA256:
        raise ValueError("frozen model card digest differs")
    payload = json.loads(raw)
    if payload.get("model_version") != "v5.1-BT":
        raise ValueError("frozen model card version differs")
    validate_normalization(payload.get("normalization"))
    calibration = payload.get("calibration")
    slot_payload = payload.get("slot_weights")
    if not isinstance(calibration, Mapping) or set(calibration) != {"alpha", "beta"}:
        raise ValueError("frozen calibration differs")
    if not isinstance(slot_payload, Mapping):
        raise ValueError("frozen slot weights differ")
    slots = {int(key): float(value) for key, value in slot_payload.items()}
    if set(slots) != set(range(1, 10)) or not math.isclose(sum(slots.values()), 1.0):
        raise ValueError("frozen slot weights differ")
    alpha, beta = float(calibration["alpha"]), float(calibration["beta"])
    if not math.isfinite(alpha) or not math.isfinite(beta) or beta <= 0:
        raise ValueError("frozen calibration differs")
    return FrozenLiveCard(
        model_version="v5.1-BT",
        file_sha256=digest,
        normalization=MappingProxyType(dict(payload["normalization"])),
        slot_weights=MappingProxyType(slots),
        alpha=alpha,
        beta=beta,
    )
```

Do not call `config.load_config()` or `pipeline._load_model_card()`: both bind the historical 2025 window and stage layout.

- [ ] **Step 4: Run regression parity**

Use one existing raw-feature fixture and assert `score_raw_game()` receives byte-equivalent normalization and produces the same component edge under the old card payload and `FrozenLiveCard.normalization`.

- [ ] **Step 5: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_model_card tests.test_scoring_prep -v
ruff check mlb_backtest/live_model_card.py tests/test_live_model_card.py
git diff --check
git add mlb_backtest/live_model_card.py tests/test_live_model_card.py
git commit -m "feat: authenticate frozen live model card"
```

### Task 3: Capture result-free schedule and participant observations

**Files:**
- Create: `mlb_backtest/live_sources.py`
- Create: `tests/test_live_sources.py`
- Create: `tests/fixtures/live_top3/schedule_complete.json`
- Create: `tests/fixtures/live_top3/schedule_partial.json`
- Create: `tests/fixtures/live_top3/boxscore_complete.json`
- Create: `tests/fixtures/live_top3/boxscore_partial.json`

- [ ] **Step 1: Write failing parser and receipt tests**

```python
class LiveSourceTests(unittest.TestCase):
    def test_http_observation_binds_status_final_url_and_selected_headers(self):
        observed = fetch_live_http(
            "https://statsapi.mlb.com/api/v1/schedule?sportId=1",
            opener=fake_opener(
                status=200,
                final_url="https://statsapi.mlb.com/api/v1/schedule?sportId=1",
                headers={"Content-Type": "application/json", "ETag": '"abc"'},
                body=b"{}",
            ),
            clock=fixed_clock,
        )
        self.assertEqual(observed.http_status, 200)
        self.assertEqual(observed.etag, '"abc"')
        self.assertEqual(observed.body, b"{}")

    def test_projection_strips_scores_winners_and_linescore(self):
        games = parse_live_schedule(SCHEDULE_COMPLETE.read_bytes())
        forbidden = {"away_score", "home_score", "away_is_winner", "home_is_winner"}
        self.assertFalse(forbidden & set(games[0]))

    def test_complete_boxscore_projects_two_starters_and_two_ordered_nines(self):
        participants = parse_pregame_boxscore(BOX_COMPLETE.read_bytes(), game_pk=777001)
        self.assertEqual(len(participants.away_lineup), 9)
        self.assertEqual(len(participants.home_lineup), 9)
        self.assertGreater(participants.away_starter_id, 0)

    def test_partial_boxscore_never_invents_participants(self):
        participants = parse_pregame_boxscore(BOX_PARTIAL.read_bytes(), game_pk=777001)
        self.assertFalse(participants.complete)
        self.assertIn("MISSING_HOME_LINEUP", participants.reason_codes)
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_sources -v
```

- [ ] **Step 3: Implement whitelisted schedule projection**

First define the bounded HTTP observation used by every experimental fetch:

```python
@dataclass(frozen=True)
class LiveHttpObservation:
    requested_url: str
    final_url: str
    started_at_utc: str
    observed_at_utc: str
    http_status: int
    content_type: str | None
    etag: str | None
    last_modified: str | None
    body: bytes

def fetch_live_http(
    url: str,
    *,
    timeout: float = 90,
    max_bytes: int = 64 * 1024 * 1024,
    opener: Callable[..., object] = urlopen,
    clock: Callable[[], datetime] = utc_now,
) -> LiveHttpObservation:
    started = clock()
    response = opener(Request(url, headers={"User-Agent": "mlb-experimental-live/1.0"}), timeout=timeout)
    with response:
        body = read_bounded_response(response, max_bytes=max_bytes)
        return LiveHttpObservation(
            requested_url=url,
            final_url=response.geturl(),
            started_at_utc=canonical_utc(started),
            observed_at_utc=canonical_utc(clock()),
            http_status=int(response.status),
            content_type=response.headers.get("Content-Type"),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            body=body,
        )
```

`read_bounded_response()` uses the existing bounded-read rules: reject non-HTTP(S) redirects, non-200 status, invalid/oversized `Content-Length`, non-bytes chunks, incomplete bodies, and bodies exceeding `max_bytes`.

```python
def parse_live_schedule(raw: bytes) -> tuple[dict[str, object], ...]:
    parsed = parse_schedule(raw)
    allowed = (
        "game_pk", "official_date", "game_type", "scheduled_start_utc", "status",
        "away_team", "away_team_id", "home_team", "home_team_id",
        "away_probable_pitcher_id", "away_probable_pitcher_name",
        "home_probable_pitcher_id", "home_probable_pitcher_name",
        "venue_id", "venue_name", "doubleheader_marker",
        "away_lineup", "home_lineup",
    )
    return tuple({field: game[field] for field in allowed} for game in parsed)
```

Reject duplicate/conflicting `gamePk`, a schedule date different from the requested date, non-regular games in the result, and any normalized game whose start is not canonical UTC.

- [ ] **Step 4: Implement a pregame-only boxscore parser**

Define `PregameParticipants` with game ID, two optional observed starter IDs, ordered lineups, completeness, and reason codes. Parse only `teams.{away,home}.team.id`, `pitchers[0]`, and player `battingOrder/person.id`. Do not parse or retain runs, winners, innings, plays, or target-game performance. A starter is `ANNOUNCED_LOCAL_OBSERVATION` only when the saved boxscore starter and saved schedule probable ID agree; a mismatch remains scoreable only under an explicit Preliminary reason.

- [ ] **Step 5: Add immutable observation storage**

```python
def observe_live_slate(
    root: Path,
    target_date: str,
    observed_at_utc: str,
    *,
    fetcher: Callable[[str], LiveHttpObservation] = fetch_live_http,
) -> LiveSlateObservation:
    raw_store = RawStore(root / "data/experimental_daily/raw")
    schedule_request = schedule_url(target_date, target_date)
    schedule_http = fetcher(schedule_request)
    schedule_artifact = raw_store.put(
        "mlb_live_schedule",
        {"target_date": target_date, "url": schedule_request},
        schedule_http.body,
    )
    schedule_raw = schedule_artifact.payload_path.read_bytes()
    games = parse_live_schedule(schedule_raw)
    bindings = [_binding_from_observation(schedule_artifact, schedule_http)]
    participants = {}
    for game in games:
        game_pk = int(game["game_pk"])
        request = boxscore_url(game_pk)
        http = fetcher(request)
        artifact = raw_store.put(
            "mlb_live_boxscore",
            {"game_pk": game_pk, "url": request},
            http.body,
        )
        participants[game_pk] = parse_pregame_boxscore(
            artifact.payload_path.read_bytes(), game_pk=game_pk
        )
        bindings.append(_binding_from_observation(artifact, http))
    return LiveSlateObservation(
        target_date=target_date,
        observed_at_utc=observed_at_utc,
        games=games,
        participants=MappingProxyType(participants),
        source_bindings=tuple(bindings),
    )
```

Define frozen `LiveSlateObservation` and `PregameParticipants` dataclasses in this module. `_binding_from_observation()` computes both payload and manifest SHA-256, requires repository-relative paths, and copies only the selected HTTP fields into `SourceBinding`. It also writes a canonical observation receipt under `audit/experimental_daily/http/` so a verified cached artifact retains its original HTTP authority. Repeated runs append observations and never replace previous raw bytes or receipts.

- [ ] **Step 6: Test start revision and stale-current-day behavior**

Fixtures must prove a changed scheduled start creates a new observation identity; a failed current-day fetch raises and never falls back to an older schedule or boxscore.

- [ ] **Step 7: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_sources tests.test_sources -v
ruff check mlb_backtest/live_sources.py tests/test_live_sources.py
git diff --check
git add mlb_backtest/live_sources.py tests/test_live_sources.py tests/fixtures/live_top3
git commit -m "feat: capture live pregame observations"
```

### Task 4: Build and verify the current-season D-1 cache

**Files:**
- Create: `mlb_backtest/live_history.py`
- Create: `tests/test_live_history.py`
- Read only: `mlb_backtest/career_bf.py`
- Regression test: `tests/test_career_bf.py`

- [ ] **Step 1: Write failing arbitrary-2026 and cutoff tests**

```python
class LiveHistoryTests(unittest.TestCase):
    def test_target_date_uses_exact_previous_day_cutoff(self):
        bundle = build_live_history(
            self.root,
            "2026-07-21",
            frozen_card(),
            fetcher=self.fixture_fetcher,
        )
        self.assertEqual(bundle.cutoff_date, "2026-07-20")
        self.assertLess(bundle.feature_history.max_pitch_row["game_date"], "2026-07-21")

    def test_target_day_statcast_poison_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "same-day leakage"):
            build_live_history(
                self.root,
                "2026-07-21",
                frozen_card(),
                fetcher=self.fetcher_with_target_day_pitch,
            )

    def test_validated_d1_cache_can_be_reused_without_network(self):
        first = build_live_history(
            self.root, "2026-07-21", frozen_card(), fetcher=self.fixture_fetcher
        )
        second = build_live_history(
            self.root, "2026-07-21", frozen_card(), fetcher=network_must_not_run
        )
        self.assertEqual(first.source_bindings, second.source_bindings)
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_history -v
```

- [ ] **Step 3: Implement a date-generic acquisition plan**

`build_live_history()` must:

1. derive `cutoff = target_date - 1 day`;
2. acquire a regular-season schedule range broad enough to derive the actual 2026 opening date through cutoff;
3. call `monthly_ranges(opening_date, cutoff)` and `fetch_monthly()` rather than historical `regular_season_ranges()`;
4. load verified Statcast manifests into one `StatcastData` with complete non-overlapping coverage;
5. derive the exact 30-team name/code map from current-season schedule and Statcast evidence;
6. build result-free schedule history using played `gamePk` and dates proven by D-1 Statcast, never target-day status/results;
7. fetch/load cumulative league ERA for the one D-1 as-of date;
8. construct `FeatureHistory` using the frozen slot weights; and
9. call `assert_feature_dates(target_date, every_bound_source_date)` before returning.

Use exact public signature:

```python
def build_live_history(
    root: Path,
    target_date: str,
    card: FrozenLiveCard,
    *,
    fetcher: Callable[[str], LiveHttpObservation] = fetch_live_http,
    sleeper: Callable[[float], None] = time.sleep,
) -> LiveHistoryBundle:
    target = date.fromisoformat(validate_target_date(target_date))
    cutoff = target - timedelta(days=1)
    raw_root = root / "data/experimental_daily/raw"
    store = RawStore(raw_root)
    schedule, schedule_bindings = _load_or_fetch_schedule_history(
        store, target.year, cutoff, fetcher=fetcher
    )
    opening = min(date.fromisoformat(row["official_date"]) for row in schedule)
    statcast, statcast_bindings = _load_or_fetch_statcast(
        store, opening, cutoff, fetcher=fetcher, sleeper=sleeper
    )
    team_map = derive_team_code_map(schedule, statcast, target.year).mapping
    schedule_build = _build_result_free_live_schedule_history(
        schedule, statcast, team_map, opening, cutoff
    )
    era, era_bindings = _load_or_fetch_league_era(
        store, opening, cutoff, fetcher=fetcher, sleeper=sleeper
    )
    history = build_pregame_history(
        statcast, {"game_pk": 1, "official_date": target_date},
        card.slot_weights, schedule_build
    )
    assert_feature_dates(target_date, _bound_source_dates(history, cutoff))
    return LiveHistoryBundle(
        target_date=target_date,
        cutoff_date=cutoff.isoformat(),
        feature_history=history,
        league_era_by_asof=MappingProxyType(era),
        team_code_by_name=MappingProxyType(dict(team_map)),
        source_bindings=tuple(schedule_bindings + statcast_bindings + era_bindings),
    )
```

Implement each named private helper in `live_history.py`; each returns only verified objects plus exact `SourceBinding` values and refuses overlapping or incomplete coverage. When calling existing `fetch_monthly()` or `fetch_league_era_records()`, supply a recording adapter that returns `LiveHttpObservation.body`, persists the observation receipt, and later pairs each returned raw artifact with that exact observation. A cache hit must reload and authenticate the persisted HTTP receipt; it must not invent metadata.

- [ ] **Step 4: Add D-1 career-BF evidence for observed starters**

Add a date-generic live query separate from the frozen through-2021 helper:

```python
def live_career_bf_url(pitcher_id: int, as_of: str) -> str:
    return f"{MLB_BASE}/people/{pitcher_id}/stats?" + urlencode({
        "stats": "byDateRange", "group": "pitching", "gameType": "R",
        "sportIds": 1, "startDate": CAREER_START, "endDate": as_of,
    })

def load_live_career_bf(
    root: Path,
    target_date: str,
    pitcher_ids: Iterable[int],
    *,
    fetcher: Callable[[str], LiveHttpObservation] = fetch_live_http,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[Mapping[int, int], tuple[SourceBinding, ...]]:
    target = date.fromisoformat(validate_target_date(target_date))
    cutoff = (target - timedelta(days=1)).isoformat()
    store = RawStore(root / "data/experimental_daily/raw")
    values, bindings = {}, []
    for pitcher_id in sorted(set(pitcher_ids)):
        request = live_career_bf_url(pitcher_id, cutoff)
        artifact, http = _fetch_and_store_with_retries(
            store, "mlb_live_career_bf",
            {"pitcher_id": pitcher_id, "as_of": cutoff, "url": request},
            request, fetcher=fetcher, sleeper=sleeper,
        )
        values[pitcher_id] = parse_prior_career_bf(
            artifact.payload_path, pitcher_id
        )
        bindings.append(_binding_from_observation(artifact, http))
    return MappingProxyType(values), tuple(bindings)
```

Store the exact response and use its total career BF as the final starter sample size; do not add it to current-season BF and double count. Reject any `as_of >= target_date`. The CLI calls this function only after observing today's known announced/probable pitcher IDs.

- [ ] **Step 5: Test cache integrity and source failure**

Tamper one cached raw file or manifest and require a global integrity failure. A transient failure may reuse only an already verified D-1 cache with the exact same target/cutoff contract. A new cutoff always requires new evidence.

- [ ] **Step 6: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_history tests.test_career_bf tests.test_statcast tests.test_mlb_stats -v
ruff check mlb_backtest/live_history.py tests/test_live_history.py
git diff --check
git add mlb_backtest/live_history.py tests/test_live_history.py
git commit -m "feat: build experimental d1 history cache"
```

### Task 5: Extract shared feature math and add honest live provenance

**Files:**
- Modify: `mlb_backtest/feature_engine.py`
- Create: `mlb_backtest/live_features.py`
- Create: `tests/test_live_features.py`
- Test: `tests/test_feature_engine.py`

- [ ] **Step 1: Freeze historical parity before refactoring**

Add a characterization test that loads the existing feature fixture, calls `build_raw_game_features()`, and asserts the complete returned mapping equals the pre-refactor expected fixture. Run it once and record GREEN before changing production code.

- [ ] **Step 2: Write failing live participant tests**

```python
class LiveFeatureTests(unittest.TestCase):
    def test_announced_participants_build_eligible_live_raw_row(self):
        raw = build_live_raw_game_features(
            live_game(announced=True), history(), team_map(), era(), career_bf()
        )
        self.assertTrue(raw["eligible"])
        self.assertEqual(raw["starter_provenance"], "ANNOUNCED_LOCAL_OBSERVATION")

    def test_probable_starters_preserve_preliminary_provenance(self):
        raw = build_live_raw_game_features(
            live_game(announced=False), history(), team_map(), era(), career_bf()
        )
        self.assertTrue(raw["eligible"])
        self.assertEqual(raw["starter_provenance"], "PROBABLE_LOCAL_OBSERVATION")

    def test_partial_lineup_is_unscoreable(self):
        with self.assertRaisesRegex(ValueError, "nine.*lineup"):
            build_live_raw_game_features(
                live_game(home_lineup=()),
                history(),
                team_map(),
                era(),
                career_bf(),
            )
```

- [ ] **Step 3: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_features -v
```

- [ ] **Step 4: Extract a participant-neutral internal builder**

In `feature_engine.py`, preserve the existing public historical eligibility and return behavior. Extract only the component-construction body after eligibility/as-of checks into:

```python
def _build_raw_components(
    game: Mapping[str, object],
    history: FeatureHistory,
    team_code_by_name: Mapping[str, str],
    resolved_league_era: Mapping[str, object],
    *,
    starter_ids: Mapping[str, int],
    starter_provenance: str,
    lineup_provenance: str,
) -> dict[str, object]:
    result = _base_raw_result(game, history, starter_provenance, lineup_provenance)
    for side in ("away", "home"):
        result[side] = _build_side_components(
            game,
            side,
            starter_ids[side],
            history,
            team_code_by_name,
            resolved_league_era,
        )
    result["eligible"] = True
    result["ineligibility_reasons"] = []
    return result
```

Extract `_base_raw_result()` and `_build_side_components()` mechanically from the current `build_raw_game_features()` body without changing arithmetic or output fields. `build_raw_game_features()` continues to require reconstructed actual starters and calls this helper with its existing values. No historical output field, reason, ordering, or number changes.

- [ ] **Step 5: Implement the live adapter**

`build_live_raw_game_features()` validates complete ordered lineups, pregame status, start identity, current live provenance, 30-team map, D-1 history, and exact D-1 career BF. It then calls the shared component helper. It must never write `RECONSTRUCTED_ACTUAL_STARTER` or infer a starter from target-game Statcast.

- [ ] **Step 6: Run parity, leakage, and GREEN tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_features tests.test_feature_engine tests.test_leakage -v
ruff check mlb_backtest/feature_engine.py mlb_backtest/live_features.py tests/test_live_features.py
git diff --check
```

- [ ] **Step 7: Commit**

```bash
git add mlb_backtest/feature_engine.py mlb_backtest/live_features.py tests/test_live_features.py tests/test_feature_engine.py
git commit -m "feat: build live result-free features"
```

### Task 6: Classify, score, and rank independent populations

**Files:**
- Create: `mlb_backtest/live_top3_service.py`
- Create: `tests/test_live_top3_service.py`

- [ ] **Step 1: Write failing state and ranking tests**

```python
class LiveTop3ServiceTests(unittest.TestCase):
    def test_four_verified_candidates_select_exact_top_three(self):
        result = build_live_top3_run(
            slate=four_verified(), history=history(), card=frozen_card(),
            career_bf_by_pitcher=career_bf(), career_bf_bindings=(),
            observed_at_utc="2026-07-21T16:00:00Z",
        )
        self.assertEqual([row["game_pk"] for row in result["verified_top3"]], [4, 2, 3])
        self.assertEqual([row["rank"] for row in result["verified_top3"]], [1, 2, 3])

    def test_populations_never_compete(self):
        result = build_live_top3_run(
            slate=mixed_states(), history=history(), card=frozen_card(),
            career_bf_by_pitcher=career_bf(), career_bf_bindings=(),
            observed_at_utc="2026-07-21T16:00:00Z",
        )
        self.assertEqual(result["verified_top3"][0]["game_pk"], VERIFIED_LOW_P)
        self.assertEqual(result["preliminary_top3"][0]["game_pk"], PRELIMINARY_HIGH_P)

    def test_missing_lineup_is_unscored_without_probability(self):
        row = build_live_top3_run(
            slate=missing_lineup(), history=history(), card=frozen_card(),
            career_bf_by_pitcher=career_bf(), career_bf_bindings=(),
            observed_at_utc="2026-07-21T16:00:00Z",
        )["unscored_games"][0]
        self.assertNotIn("pick_probability", row)
        self.assertNotIn("rank", row)
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_top3_service -v
```

- [ ] **Step 3: Implement exact state classification**

Use this public boundary:

```python
def build_live_top3_run(
    *,
    slate: LiveSlateObservation,
    history: LiveHistoryBundle,
    card: FrozenLiveCard,
    career_bf_by_pitcher: Mapping[int, int],
    career_bf_bindings: tuple[SourceBinding, ...],
    observed_at_utc: str,
) -> dict[str, object]:
    """Return one validated, content-hashed experimental run payload."""
```

Classification order is:

1. started/final/canceled/postponed or date mismatch -> `EXCLUDED`;
2. incomplete lineup, missing starter, invalid history, or feature failure -> `UNSCORED`;
3. complete lineups plus two locally announced observed starters -> `VERIFIED`;
4. complete lineups plus one or two explicit probable starters -> `PRELIMINARY`.

Each row retains deterministic reason codes. A start-time revision invalidates an older participant observation for that game.

- [ ] **Step 4: Reuse frozen scoring math and implement live ranking tuple**

For each eligible raw row, call `score_raw_game()`, `home_probability()`, `predicted_pick()`, confidence/uncertainty/tier helpers, and calculate data completeness. Rank each state independently by:

```python
key=lambda row: (
    -row["pick_probability"],
    -row["data_completeness"],
    -row["starter_edge_abs"],
    row["scheduled_start_utc"],
    row["game_pk"],
)
```

Do not call historical `rank_daily()` because its second key is legacy confidence, not the approved live completeness key.

- [ ] **Step 5: Bind row and source digests**

Each scoreable row binds the exact model-card digest, raw-feature digest, current observation digests, D-1 history digests, participant provenance, observation time, and scheduled-start identity. The run payload's `content_sha256` is computed over the canonical payload with that field omitted, then inserted and revalidated.

- [ ] **Step 6: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_live_top3_service tests.test_scoring tests.test_scoring_prep tests.test_ranking -v
ruff check mlb_backtest/live_top3_service.py tests/test_live_top3_service.py
git diff --check
git add mlb_backtest/live_top3_service.py tests/test_live_top3_service.py
git commit -m "feat: rank experimental live top3"
```

### Task 7: Add report persistence and the one-command CLI

**Files:**
- Create: `mlb_backtest/daily_top3.py`
- Create: `tests/test_daily_top3.py`

- [ ] **Step 1: Write failing CLI and write-boundary tests**

```python
class DailyTop3CliTests(unittest.TestCase):
    def test_default_date_is_eastern_today(self):
        with patch("mlb_backtest.daily_top3.now_eastern", return_value=FIXED_NOW):
            self.assertEqual(parse_args([]).date, "2026-07-21")

    def test_success_writes_json_and_markdown_only_under_experimental_root(self):
        code = main(["--root", str(self.root), "--date", "2026-07-21"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(relative_files(self.root)), expected_run_files())

    def test_global_integrity_failure_returns_two_and_writes_no_ranking(self):
        with patch(
            "mlb_backtest.daily_top3.run_daily_top3",
            side_effect=IntegrityError("card digest differs"),
        ):
            self.assertEqual(main(["--root", str(self.root)]), 2)
        self.assertFalse(list((self.root / "reports").rglob("*.json")))
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_top3 -v
```

- [ ] **Step 3: Implement the exact CLI**

```python
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m mlb_backtest.daily_top3")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--date", type=canonical_date, default=None)
    parser.add_argument(
        "--model-card",
        type=Path,
        default=Path("audit/v5_1_bt/model_card.json"),
    )
    parser.add_argument("--json-only", action="store_true")
    return parser

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = _parser().parse_args(argv)
    if args.date is None:
        args.date = now_eastern().date().isoformat()
    args.date = validate_target_date(args.date)
    return args
```

Resolve a missing date from `ZoneInfo("America/New_York")`. Exit codes are `0` for a valid report even with fewer than three rows, `1` for operational source failure, `2` for integrity/contract failure, and `130` for keyboard interruption.

The command orchestration boundary is:

```python
def run_daily_top3(
    root: Path,
    target_date: str,
    observed_at_utc: str,
    model_card_path: Path,
    *,
    fetcher: Callable[[str], LiveHttpObservation] = fetch_live_http,
) -> tuple[dict[str, object], Path, Path]:
    card = load_frozen_live_card(model_card_path)
    slate = observe_live_slate(
        root, target_date, observed_at_utc, fetcher=fetcher
    )
    history = build_live_history(
        root, target_date, card, fetcher=fetcher
    )
    pitcher_ids = known_observed_pitcher_ids(slate)
    career_bf, career_bindings = load_live_career_bf(
        root, target_date, pitcher_ids, fetcher=fetcher
    )
    payload = build_live_top3_run(
        slate=slate,
        history=history,
        card=card,
        career_bf_by_pitcher=career_bf,
        career_bf_bindings=career_bindings,
        observed_at_utc=observed_at_utc,
    )
    json_path, markdown_path = persist_live_top3_report(root, payload)
    return payload, json_path, markdown_path
```

- [ ] **Step 4: Implement append-only run reports**

Use a run filename composed of canonical observation UTC plus the run content digest. Write canonical JSON first with `secure_fs.write_exclusive()`, rebind it, render Markdown only from the validated JSON payload, and write the Markdown under the same run ID. Never write `latest`, overwrite an earlier report, or delete failure residue.

Use these exact boundaries:

```python
def render_live_top3_markdown(payload: Mapping[str, object]) -> bytes:
    safe = validate_run_payload(payload)
    lines = [
        "# Experimental MLB Daily Top 3",
        "",
        "**RESEARCH ONLY — NON-OFFICIAL SAMPLE — PROFITABILITY NOT VALIDATED**",
        "",
        f"Target date: `{safe['target_date']}`",
        f"Observed: `{safe['observed_at_utc']}`",
        "",
    ]
    lines.extend(_render_population("Verified Top 3", safe["verified_top3"]))
    lines.extend(_render_population("Preliminary Top 3", safe["preliminary_top3"]))
    lines.extend(_render_unscored(safe["unscored_games"], safe["excluded_games"]))
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")

def persist_live_top3_report(
    root: Path, payload: Mapping[str, object]
) -> tuple[Path, Path]:
    safe = validate_run_payload(payload)
    stamp = safe["observed_at_utc"].replace(":", "").replace("-", "")
    run_id = f"{stamp}-{safe['content_sha256'][:16]}"
    relative = Path("reports/experimental_daily") / safe["target_date"]
    json_relative = relative / f"{run_id}.json"
    markdown_relative = relative / f"{run_id}.md"
    with open_trusted_root(root) as trusted:
        written = write_exclusive(
            trusted, json_relative, canonical_json_bytes(safe), "daily top3 JSON"
        )
        rebind_regular_snapshot(trusted, written, "daily top3 JSON")
        rendered = render_live_top3_markdown(safe)
        report = write_exclusive(
            trusted, markdown_relative, rendered, "daily top3 Markdown"
        )
        rebind_regular_snapshot(trusted, report, "daily top3 Markdown")
    return root / json_relative, root / markdown_relative
```

`_render_population()` emits only fields present in the JSON row and escapes Markdown cell delimiters. `_render_unscored()` prints reason codes without probabilities or ranks.

The terminal summary prints Verified Top 3, Preliminary Top 3, the unscored/excluded counts, warnings, and absolute report paths. The Markdown header includes `RESEARCH ONLY`, `NON-OFFICIAL SAMPLE`, and `PROFITABILITY NOT VALIDATED`.

- [ ] **Step 5: Test report fidelity and repeat runs**

Parse the JSON, recompute its content hash, and assert every displayed Markdown row comes from it. Two runs with different observation times must preserve both report pairs. Same run identity must conflict rather than overwrite.

- [ ] **Step 6: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_top3 tests.test_secure_fs -v
ruff check mlb_backtest/daily_top3.py tests/test_daily_top3.py
git diff --check
git add mlb_backtest/daily_top3.py tests/test_daily_top3.py
git commit -m "feat: add one-command daily top3 report"
```

### Task 8: Prove the full fixture flow and regression boundary

**Files:**
- Create: `tests/test_daily_top3_e2e.py`
- Modify fixtures only: `tests/fixtures/live_top3/`

- [ ] **Step 1: Write a failing sealed-fixture E2E test**

The fixture represents at least six games: four Verified, one Preliminary, one partial-lineup Unscored, plus one already-started Excluded. The fake fetcher must serve schedule, per-game boxscore, D-1 schedule history, Statcast chunks, league ERA, and career BF.

```python
def test_fixture_e2e_is_outcome_isolated_and_deterministic(self):
    first = run_daily_top3_fixture(self.root / "one")
    second = run_daily_top3_fixture(self.root / "two")
    self.assertEqual(canonical_without_paths(first), canonical_without_paths(second))
    self.assertEqual(len(first["verified_top3"]), 3)
    self.assertEqual(len(first["preliminary_top3"]), 1)
    self.assertEqual(len(first["unscored_games"]), 1)
    self.assert_no_fields(first, {"score", "winner", "outcome", "odds", "market"})
```

- [ ] **Step 2: Run RED, then add only the missing fixture integration**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_top3_e2e -v
```

Expected RED must be an integration wiring gap, not a malformed fixture.

- [ ] **Step 3: Add tamper and failure-matrix tests**

Cover card tamper, raw cache tamper, manifest tamper, target-day Statcast poison, incomplete current schedule response, boxscore start revision, missing starter, partial lineup, no scheduled games, transient D-1 cache reuse, forbidden stale current-day reuse, and output conflict.

- [ ] **Step 4: Run the focused release gate**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_live_top3_contracts \
  tests.test_live_model_card \
  tests.test_live_sources \
  tests.test_live_history \
  tests.test_live_features \
  tests.test_live_top3_service \
  tests.test_daily_top3 \
  tests.test_daily_top3_e2e -v
ruff check mlb_backtest tests
git diff --check
```

Expected: all focused tests `OK`, Ruff clean, no whitespace errors.

- [ ] **Step 5: Run affected historical regression tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_feature_engine tests.test_scoring_prep tests.test_scoring \
  tests.test_ranking tests.test_pipeline tests.test_leakage tests.test_sources \
  tests.test_statcast tests.test_mlb_stats tests.test_career_bf -v
```

Expected: all existing v5.1 behavior remains green.

- [ ] **Step 6: Commit E2E coverage**

```bash
git add tests/test_daily_top3_e2e.py tests/fixtures/live_top3
git commit -m "test: prove daily top3 end to end"
```

### Task 9: Document, independently review, and perform the first live smoke run

**Files:**
- Create: `docs/experimental_live_top3.md`
- Create: `reports/experimental_daily/implementation_record.md`
- Modify only when generated by a successful run: ignored `data/experimental_daily/`, `audit/experimental_daily/`, `reports/experimental_daily/YYYY-MM-DD/`

- [ ] **Step 1: Write operator instructions**

Document the exact command:

```bash
cd "/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest"
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.daily_top3
```

Explain first-run bootstrap, later incremental verification, the three candidate states, why fewer than three rows is valid, exit codes, artifact locations, how to rerun after lineups are posted, and all research-only limitations.

- [ ] **Step 2: Run independent spec review**

Give the reviewer the governing design, this plan, and the full implementation range. Require exact confirmation that every accepted state, data boundary, digest, output, failure, and non-goal is implemented without expanding into formal prospective validation.

- [ ] **Step 3: Run independent code-quality and security review**

Review network bounds, raw-store writes, cache selection, target-day leakage, mutable default/date behavior, participant provenance, descriptor handling, deterministic output, and platform behavior. Fix every Critical or Important issue and repeat both reviews.

- [ ] **Step 4: Run the first real same-day smoke command**

After focused and regression gates pass, execute the one command for the current Eastern date. Network use is allowed only in this step. Preserve the exact raw receipts and report even if the valid result has zero Verified rows.

- [ ] **Step 5: Inspect and record the live result**

Validate the JSON against `schemas/experimental_daily_top3.schema.json`, recompute every SHA-256, confirm all game starts were future at observation or explicitly excluded, confirm no target-date event rows, and compare Markdown rows to JSON.

Write `implementation_record.md` with commit, command, observation time, source counts, selected counts, test commands/results, review verdicts, limitations, and exact report paths.

- [ ] **Step 6: Commit documentation and implementation record**

```bash
git add docs/experimental_live_top3.md reports/experimental_daily/implementation_record.md
git commit -m "docs: release experimental daily top3"
```

- [ ] **Step 7: Push without waiting for hardening Task 14**

Push the feature branch after the Daily Top 3 focused gate and independent reviews pass. Keep PR #1 draft if its separate hardening gates remain open; local experimental use begins from the verified feature commit.

---

## Completion boundary

Daily Top 3 is locally usable when Tasks 1-9 pass, a real same-day command produces a validated report, and independent spec and quality reviewers approve. This boundary does not require PR #1 Task 14, v5.2 `SOURCES_READY`, historical 500 fitting, automatic scheduling, or formal T-45 admission.
