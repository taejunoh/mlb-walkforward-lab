# MLB v5.2-QD Hybrid Acquisition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build and run the fail-closed Plan A pipeline that promotes semantically valid 2022–2024 legacy evidence, acquires missing 2018–2024 official-source children, recomputes every grain-specific contract from raw bytes, and opens the H22 fitting gate only after all 2018–2024 minimum sources are READY.

**Architecture:** Keep the frozen RunSpec declarative. Store mutable progress in an append-only phase-receipt chain. Seal an acquisition policy before generating query IDs, publish HTTP attempts and source evidence through a separate v5.2 store, recompute source-specific sidecars from owned or protected legacy bytes, and let an offline verifier create the only inventory trusted by preflight. The verifier never trusts publisher declarations without rereading the evidence.

**Tech Stack:** Python 3 standard library, existing mlb_backtest package, MLB Stats API, Baseball Savant CSV, canonical JSON/SHA-256, fcntl.flock, unittest, Ruff, Git.

---

## Scope and fixed roots

This plan implements and executes only Plan A:

- canonical acquisition policy and deterministic query batches;
- HTTP capture, retry, split, quarantine, resume, and atomic publication;
- protected 2022–2024 legacy promotion by reference;
- schedule/game, Statcast, pitching, career-BF, team/venue/timezone, and prior-schedule semantics;
- offline source inventory verification;
- DESIGN_ACCEPTED → ACQUIRING → SOURCES_READY;
- grain-aware preflight with may_fit_H22=true only after all 2018–2024 minimum evidence is READY.

It does not implement O19–O23 fitting, H22/H23/H24 cards, target locking, outcome joining, the 500-date membership ledger, metrics, or workbook generation. Those belong to Plan B and Plan C.

Canonical roots:

    data/v5_2_qd/historical/raw
    data/v5_2_qd/historical/sources
    audit/v5_2_qd/historical/acquisition
    audit/v5_2_qd/historical/execution
    reports/v5_2_qd/walkforward_500_preflight.json
    reports/v5_2_qd/walkforward_500_preflight.md

## Worker protocol

- Deep-reasoner owns Tasks 1, 2, 4–8, 10, and 11.
- Fast-worker owns repetitive fixtures, schema wiring, CLI wiring, and regression expansion in Tasks 0, 3, 9, and 12.
- No workers edit the same production file concurrently.
- Every implementation task follows red → green → focused regression → review → commit.
- Unit/integration tests inject fetchers, clocks, and sleepers. Only Task 13 may use the network.
- Existing user-owned untracked duplicate files are not modified, removed, staged, or committed.
- Public interface blocks below are signature notation; implementation bodies are supplied during the named TDD task.
- The existing v5.1 RawStore remains unchanged; v5.2 uses a separate evidence store and calls RawStore only through a read-only legacy adapter.

## Task 0: Freeze the approved handoff and baseline

**Files:**

- Create: protocols/v5_2_qd/historical_source_acquisition.json
- Create: schemas/v5_2_acquisition_contract.schema.json
- Create: tests/test_v52_acquisition_contract.py
- Create: mlb_backtest/v52_protected_inventory.py
- Create: schemas/v5_1_protected_raw_inventory.schema.json
- Create: tests/test_v52_protected_inventory.py
- Produce: audit/v5_1_bt/protected_raw_inventory.json
- Verify: docs/superpowers/specs/2026-07-13-mlb-v5-2-acquire-fit-500-design.md
- Verify: docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md
- Verify: protocols/v5_2_qd/historical_walkforward_500.json

- [ ] Write test_acquisition_contract_binds_approved_design_plan_and_run_spec, test_contract_uses_canonical_raw_root, test_contract_covers_2018_through_2024_and_forbids_2025, and test_contract_rejects_unknown_keys.
- [ ] Commit this plan before creating the contract. Require the contract to verify both its working-tree SHA-256 and Git blob ID, then bind lowercase 64-character SHA-256 values for the approved design, RunSpec, and protected legacy inventory. It must freeze source names, grains, endpoints, parser versions, season boundaries, host concurrency, 20 GiB reserve, permitted legacy years [2022, 2023, 2024], required years [2018..2024], legacy_root=data/v5_1_bt/raw, and protected_inventory_path=audit/v5_1_bt/protected_raw_inventory.json.
- [ ] Implement build_protected_raw_inventory(root, legacy_root) and verify_protected_raw_inventory(root, inventory_path). Inventory every legacy manifest, seal, and raw object with relative path, source, canonical query, SHA-256, size, inode fingerprint, and RawStore verification result. Write canonical JSON once and bind its hash into the acquisition contract.
- [ ] The contract hash is computed before query generation. Query IDs bind this contract hash, not the generated query inventory hash; this prevents a circular hash dependency.
- [ ] Run the failing test, create canonical JSON and schema, rerun, and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_acquisition_contract tests.test_v52_protected_inventory -v
    git add protocols/v5_2_qd/historical_source_acquisition.json schemas/v5_2_acquisition_contract.schema.json tests/test_v52_acquisition_contract.py mlb_backtest/v52_protected_inventory.py schemas/v5_1_protected_raw_inventory.schema.json tests/test_v52_protected_inventory.py audit/v5_1_bt/protected_raw_inventory.json
    git commit -m "test: freeze v5.2 acquisition contract"

## Task 1: Add the append-only execution registry

**Files:**

- Create: mlb_backtest/execution_registry.py
- Create: schemas/v5_2_execution_receipt.schema.json
- Create: tests/test_execution_registry.py
- Modify: mlb_backtest/verify_v52_design.py
- Modify: tests/test_verify_v52_design.py

- [ ] Write failing tests named:

    test_initializes_design_accepted_receipt_once
    test_transition_requires_immediate_next_phase
    test_transition_binds_prior_receipt_commit_specs_and_inventory
    test_failed_validation_quarantines_without_advancing
    test_rejects_chain_gap_fork_tamper_symlink_hardlink_special_and_rollback
    test_concurrent_transition_has_one_winner
    test_registry_load_is_read_only
    test_design_verifier_reads_phase_from_registry_not_file_presence

- [ ] Implement these public contracts:

    PHASES: tuple[str, ...] = (
        "DESIGN_ACCEPTED", "ACQUIRING", "SOURCES_READY", "OOF_SEALED",
        "CARDS_SEALED", "TARGET_LOCKED", "OUTCOMES_JOINED",
        "SAMPLE_500_SEALED", "VERIFIED",
    )

    @dataclass(frozen=True)
    class ExecutionRegistryState:
        phase: str
        sequence: int
        receipt_sha256: str
        inventory_sha256: str

    def initialize_execution_registry(
        root: Path, run_spec: RunSpec, *,
        design_path: Path, plan_path: Path,
        acquisition_contract_path: Path,
    ) -> ExecutionRegistryState

    def load_execution_registry(
        root: Path, run_spec: RunSpec,
    ) -> ExecutionRegistryState

    def advance_execution_phase(
        root: Path, run_spec: RunSpec, *, expected_phase: str,
        next_phase: str, inventory_path: Path,
        validation_receipts: Sequence[Path],
    ) -> ExecutionRegistryState

    def quarantine_execution_transition(
        root: Path, run_spec: RunSpec, *, attempted_phase: str,
        validation_receipts: Sequence[Path],
    ) -> Path

- [ ] Registry functions read trusted UTC and git HEAD internally, rehash artifact paths themselves, and reject caller-supplied digest/time/commit values. Store successful receipts under audit/v5_2_qd/historical/execution/phase_receipts/000000-DESIGN_ACCEPTED-{hash}.json and failed attempts under execution/quarantine. Use a registry flock, O_EXCL, fsync, no-follow reads, one final newline, and st_nlink == 1.
- [ ] Document the rollback boundary precisely: surviving later anchors detect gaps/forks/tail rollback; coordinated deletion of the entire local tail is outside a local-only registry threat model until an external anchor is added. Tests must not claim otherwise.
- [ ] Preserve design-only verification at DESIGN_ACCEPTED. Allow only raw/source/acquisition/execution namespaces at ACQUIRING and SOURCES_READY. Continue rejecting OOF, card, lock, and outcome artifacts in Plan A.
- [ ] Run focused tests and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry tests.test_verify_v52_design -v
    git add mlb_backtest/execution_registry.py schemas/v5_2_execution_receipt.schema.json tests/test_execution_registry.py mlb_backtest/verify_v52_design.py tests/test_verify_v52_design.py
    git commit -m "feat: add v5.2 execution registry"

## Task 2: Implement the v5.2 evidence store

**Files:**

- Create: mlb_backtest/v52_evidence.py
- Create: schemas/v5_2_evidence_manifest.schema.json
- Create: schemas/v5_2_query_sidecar.schema.json
- Create: tests/test_v52_evidence.py
- Do not modify: mlb_backtest/manifest.py

- [ ] Write failing tests for canonical parameter ordering, identity-field sensitivity, append-only attempt numbering, selected headers, exact byte count, success binding, exact-one-of fetched raw/protected reference, retry/resume, duplicate ambiguity, concurrent publication, partial interruption, and path traversal/symlink/hardlink/special-file attacks.
- [ ] Implement:

    @dataclass(frozen=True)
    class LogicalQuery:
        source: str
        grain: str
        endpoint: str
        parameters: Mapping[str, object]
        requested_range: Mapping[str, object]
        expected_media_type: str
        parser_version: str
        parent_query_id: str | None
        run_spec_sha256: str
        acquisition_contract_sha256: str

    @dataclass(frozen=True)
    class AttemptCapture:
        started_at_utc: str
        ended_at_utc: str
        final_url: str | None
        http_status: int | None
        response_headers: tuple[tuple[str, str], ...]
        body: bytes | None
        error_classification: str | None

    @dataclass(frozen=True)
    class PublishedEvidence:
        query_id: str
        success_receipt_path: Path
        raw_sha256: str
        query_sidecar_sha256: str
        storage_mode: Literal["FETCHED_RAW", "PROTECTED_LEGACY_REFERENCE"]

    def canonical_query_bytes(query: LogicalQuery) -> bytes
    def logical_query_id(query: LogicalQuery) -> str

    class V52EvidenceStore:
        def __init__(self, root: Path) -> None
        def register_query(self, query: LogicalQuery) -> str
        def load_success(self, query: LogicalQuery) -> PublishedEvidence | None
        def acquire_query(self, query: LogicalQuery) -> ContextManager[QueryLease]
        def next_attempt_number(self, lease: QueryLease) -> int
        def record_attempt(
            self, query: LogicalQuery, attempt_number: int,
            capture: AttemptCapture, *, outcome: str,
        ) -> Path
        def quarantine_attempt(
            self, query: LogicalQuery, attempt_number: int,
            capture: AttemptCapture, *, error_classification: str,
        ) -> Path
        def publish_fetched(
            self, query: LogicalQuery, attempt_number: int,
            capture: AttemptCapture,
            query_sidecar: Mapping[str, object],
        ) -> PublishedEvidence
        def publish_legacy_reference(
            self, query: LogicalQuery,
            legacy_reference: Mapping[str, object],
            query_sidecar: Mapping[str, object],
        ) -> PublishedEvidence
        def verify_success(self, query: LogicalQuery) -> PublishedEvidence

- [ ] QuerySemanticSidecar covers exactly one HTTP response and can be recomputed without dependent queries. SeasonSourceSnapshot is a separate Task 4/8 aggregate under the sources namespace. Use raw/queries/{source}/{query_id}/ with query.json, attempts/, objects/, sidecars/, and success.json. Keep attempt receipts beside the query and invalid body bytes/receipts under raw/quarantine/{source}/{query_id}/. Query batches and human review receipts live under audit/v5_2_qd/historical/acquisition. Publish success.json last.
- [ ] QueryLease holds the per-query flock across load-success, attempt reservation, network fetch, receipt finalization, and publication so concurrent workers cannot issue duplicate requests.
- [ ] Canonical JSON files have one final newline; query IDs hash canonical bytes without that newline.
- [ ] Run tests and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_evidence -v
    git add mlb_backtest/v52_evidence.py schemas/v5_2_evidence_manifest.schema.json schemas/v5_2_query_sidecar.schema.json tests/test_v52_evidence.py
    git commit -m "feat: add immutable v5.2 evidence store"

## Task 3: Add HTTP attempt capture and deterministic query batches

**Files:**

- Create: mlb_backtest/v52_http.py
- Create: mlb_backtest/v52_query_plan.py
- Create: tests/test_v52_http.py
- Create: tests/test_v52_query_plan.py
- Modify: mlb_backtest/sources.py
- Modify: tests/test_sources.py

- [ ] Write transport tests for final URL, status, selected Content-Type/Content-Length/ETag/Last-Modified/Retry-After headers, length disagreement, maximum bytes, transient/permanent classification, Retry-After delta and HTTP date, deterministic jitter, request URL matching canonical endpoint/parameters, redirect host allowlist, and expected Content-Type compatibility.
- [ ] Implement:

    TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})

    @dataclass(frozen=True)
    class RetryPolicy:
        base_delays: tuple[float, ...] = (1, 2, 4, 8, 16)
        jitter_fraction: float = 0.25

    def parse_retry_after(value: str | None, *, now_utc: datetime) -> float | None
    def deterministic_jitter(
        query_id: str, attempt_number: int, base_delay: float,
        fraction: float = 0.25,
    ) -> float
    def fetch_attempt(
        query: LogicalQuery, *, timeout: float, max_bytes: int,
        opener=urlopen, clock=_utc_now,
    ) -> AttemptCapture

- [ ] Keep sources.fetch_bytes backward-compatible; use v52_http only for v5.2 evidence capture.
- [ ] Write deterministic query tests for 2018–2024 only, 2025 exclusion, official endpoints, monthly Statcast parents, child-parent binding, split gap/overlap rejection, stable bytes, and schedule-derived query deduplication.
- [ ] Implement:

    @dataclass(frozen=True)
    class QueryBatch:
        batch_id: str
        stage: str
        derived_from_sha256: str | None
        queries: tuple[LogicalQuery, ...]

    def build_static_batch(
        run_spec_path: Path, acquisition_contract_path: Path,
    ) -> QueryBatch

    def build_schedule_reference_batch(
        anchor_inventory: Mapping[str, object], *,
        run_spec_sha256: str, acquisition_contract_sha256: str,
    ) -> QueryBatch

    def build_participant_derived_batch(
        game_evidence_inventory: Mapping[str, object], *,
        run_spec_sha256: str, acquisition_contract_sha256: str,
    ) -> QueryBatch

    def build_legacy_partition_batch(
        parents: Sequence[LogicalQuery],
        protected_catalog: Sequence[Mapping[str, object]],
        *, protected_inventory_sha256: str,
    ) -> QueryBatch

    def bisect_statcast_query(
        parent: LogicalQuery,
    ) -> tuple[LogicalQuery, LogicalQuery]

    def publish_query_batch(root: Path, batch: QueryBatch) -> Path

- [ ] Static schedule responses first produce schedule anchors. The schedule-reference batch then contains boxscores, D-1 pitching, teams, and venues. Only after verified final game evidence identifies actual starters does the participant-derived batch create career-BF queries. When opening-day D-1 precedes season open, emit PRESEASON_NO_CURRENT_SEASON_PITCHING instead of an invalid MLB Stats query.
- [ ] The legacy-partition batch maps verified 2022–2024 legacy Statcast split children into exact planned child queries. It binds the protected inventory hash, requires ordered/nonoverlapping child ranges inside each monthly parent, fails on mixed parent/child ambiguity, and plans fetch children only for uncovered gaps.
- [ ] Run tests and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_http tests.test_v52_query_plan tests.test_sources -v
    git add mlb_backtest/v52_http.py mlb_backtest/v52_query_plan.py mlb_backtest/sources.py tests/test_v52_http.py tests/test_v52_query_plan.py tests/test_sources.py
    git commit -m "feat: plan resumable v5.2 source queries"

## Task 4: Add closed grain-specific semantic sidecars

**Files:**

- Create: mlb_backtest/v52_source_contracts.py
- Create: schemas/v5_2_season_source_snapshot.schema.json
- Create: mlb_backtest/v52_sources/__init__.py
- Create: mlb_backtest/v52_sources/schedule.py
- Create: mlb_backtest/v52_sources/statcast.py
- Create: mlb_backtest/v52_sources/pitching.py
- Create: mlb_backtest/v52_sources/references.py
- Create: mlb_backtest/v52_sources/history.py
- Create: tests/test_v52_source_schedule.py
- Create: tests/test_v52_source_statcast.py
- Create: tests/test_v52_source_pitching.py
- Create: tests/test_v52_source_references.py
- Create: tests/test_v52_source_history.py
- Create fixture files under tests/fixtures/v5_2_qd/sources/

- [ ] Define a closed schema oneOf per grain. Generic row_count/gamePk validation is forbidden for reference grains.
- [ ] Implement these dispatch contracts:

    def recompute_schedule_anchor_sidecar(
        raw: bytes, query: LogicalQuery,
    ) -> Mapping[str, object]

    def recompute_game_evidence_snapshot(
        schedule_anchors: Sequence[Mapping[str, object]],
        boxscores: Mapping[int, tuple[bytes, str]],
    ) -> Mapping[str, object]

    def recompute_statcast_sidecar(
        raw: bytes, query: LogicalQuery,
    ) -> Mapping[str, object]

    def recompute_pitching_sidecar(
        raw: bytes, query: LogicalQuery,
        consuming_game_pks: Sequence[int],
    ) -> Mapping[str, object]

    def recompute_career_bf_sidecar(
        raw: bytes, query: LogicalQuery,
        consuming_game_pks: Sequence[int],
    ) -> Mapping[str, object]

    def recompute_reference_sidecar(
        team_payloads: Sequence[bytes], venue_payloads: Sequence[bytes],
        timezone_rows: Sequence[Mapping[str, object]],
        schedules: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object]

    def recompute_prior_history_sidecar(
        schedule_sidecar: Mapping[str, object],
    ) -> Mapping[str, object]

    def recompute_query_sidecar(
        query: LogicalQuery, raw: bytes,
    ) -> Mapping[str, object]

- [ ] Schedule tests prove the two layers: a query-level schedule anchor is sealable without boxscores and deterministically produces boxscore/venue queries; the later season game-evidence snapshot joins verified anchors with boxscores. Cover complete population, played/unplayed separation, duplicate/conflicting gamePk, official date/team/venue, doubleheader order, terminal status/scores/winner, actual starter, nine starting hitters per side, and disagreement.
- [ ] Label complete played-game identity RECONSTRUCTED_FINAL_PARTICIPANTS. Never label it T_MINUS_45. Label unplayed games without participants HISTORICAL_NO_PARTICIPANTS.
- [ ] Statcast tests cover every current REQUIRED_FIELDS item, unique (game_pk, at_bat_number, pitch_number), conflicting duplicates, game population, participant IDs, PA terminal evidence, per-game digest, 25,000-row cap, timeout/size split, ordered nonoverlapping child coverage, and terminal single-day cap.
- [ ] Reuse existing Statcast parsers instead of building a second pitch parser.
- [ ] Pitching tests cover totalSplits, paging/truncation, earned runs, outs, batters faced, unique as-of date, exact season open, and consuming-game mappings. Career-BF remains a distinct player/entity grain and can never satisfy annual pitching. Acquire a pre-2018 MLB baseline through inclusive_end=2017-12-31, then add verified in-scope Statcast BF through each consumer D-1. Permit the legacy 2021-12-31 BF baseline only for 2022+ consumers. Every sidecar binds pitcher_id, inclusive_end_date, consuming game IDs, and proves inclusive_end_date <= consumer D-1.
- [ ] Reference tests cover official team/venue identity, coordinates, alternate/neutral venues, valid IANA ZoneInfo names, inclusive effective dates, no overlap, exactly one mapping per observed (venue_id, official_date), and every team ID.
- [ ] Prior-history tests bind source game hashes and require every feature history cutoff to be D-1.
- [ ] Run focused tests and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_schedule tests.test_v52_source_statcast tests.test_v52_source_pitching tests.test_v52_source_references tests.test_v52_source_history -v
    git add mlb_backtest/v52_source_contracts.py mlb_backtest/v52_sources schemas/v5_2_season_source_snapshot.schema.json tests/test_v52_source_schedule.py tests/test_v52_source_statcast.py tests/test_v52_source_pitching.py tests/test_v52_source_references.py tests/test_v52_source_history.py tests/fixtures/v5_2_qd/sources
    git commit -m "feat: recompute v5.2 source semantics"

## Task 5: Implement the observed-venue timezone review gate

**Files:**

- Create: mlb_backtest/v52_timezone_review.py
- Create: schemas/v5_2_venue_timezones.schema.json
- Create: schemas/v5_2_timezone_review_receipt.schema.json
- Create: tests/test_v52_timezone_review.py
- Create fixture mapping: tests/fixtures/v5_2_qd/sources/venue_timezones_mini.json
- Do not create the real config/v5_2_qd_venue_timezones.json until Task 13 has fetched all 2018–2024 schedules.

- [ ] Write tests for candidate generation from observed regular-season venues, alternate/neutral venue inclusion, canonical ordering, valid ZoneInfo names, inclusive effective dates, no overlap, exactly one mapping per observed venue/date, two distinct reviewer approvals, candidate-hash binding, and approval refusal when rows remain unresolved.
- [ ] Implement:

    @dataclass(frozen=True)
    class TimezoneReview:
        reviewer_role: str
        candidate_sha256: str
        reviewed_venue_ids: tuple[int, ...]
        approved: bool
        occurred_at_utc: str

    def build_timezone_candidate(
        schedule_sidecars: Sequence[Mapping[str, object]],
        venue_sidecars: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object]

    def validate_timezone_mapping(
        mapping: Mapping[str, object],
        schedule_sidecars: Sequence[Mapping[str, object]],
    ) -> None

    def write_timezone_review_receipt(
        root: Path, *, candidate_path: Path,
        reviewer_identity: str, review_scope: Sequence[int],
        decision: str,
    ) -> Path

    def seal_timezone_mapping(
        candidate: Mapping[str, object],
        reviews: Sequence[TimezoneReview],
        *, output: Path, receipt_output: Path,
    ) -> tuple[Path, Path]

    def main(argv: Sequence[str] | None = None) -> int

- [ ] Candidate generation may suggest a timezone from coordinates but must mark every suggestion UNREVIEWED. It cannot write the production config.
- [ ] Sealing requires one deep-reasoner review of every official venue identity/location/effective range and one independent fast-worker completeness/overlap review. write_timezone_review_receipt uses O_EXCL and trusted UTC, binds reviewer identity, role, candidate hash, exact venue scope, and decision; duplicate identities do not count as independent reviews.
- [ ] Use the miniature fixture to prove the review gate without network access. Commit code, schemas, tests, and fixture only.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_timezone_review tests.test_v52_source_references -v
    git add mlb_backtest/v52_timezone_review.py schemas/v5_2_venue_timezones.schema.json schemas/v5_2_timezone_review_receipt.schema.json tests/test_v52_timezone_review.py tests/test_v52_source_references.py tests/fixtures/v5_2_qd/sources/venue_timezones_mini.json
    git commit -m "feat: gate reviewed v5.2 venue timezones"

## Task 6: Implement protected legacy promotion

**Files:**

- Create: mlb_backtest/v52_legacy.py
- Create: tests/test_v52_legacy.py
- Create: tests/fixtures/v5_2_qd/legacy/README.md

- [ ] Construct tiny legacy stores at test time with the current RawStore; do not commit duplicated raw data.
- [ ] Write promotion tests for protected inventory, manifest, seal, filename, canonical query, raw hash, row count, current-parser semantics, coverage, and exact planned-child matching.
- [ ] Add tamper cases for every field above plus symlink, hardlink, changed inode, ambiguous candidates, parser change, and career-BF aliasing.
- [ ] Implement:

    @dataclass(frozen=True)
    class LegacyCandidate:
        source: str
        manifest_path: Path
        manifest_sha256: str
        seal_path: Path
        payload_path: Path
        raw_sha256: str

    @dataclass(frozen=True)
    class PromotionDecision:
        query_id: str
        disposition: str
        reason_codes: tuple[str, ...]
        candidate: LegacyCandidate | None

    def load_protected_legacy_catalog(
        legacy_root: Path, protected_inventory_path: Path,
    ) -> tuple[LegacyCandidate, ...]

    def audit_legacy_candidate(
        query: LogicalQuery, catalog: Sequence[LegacyCandidate],
        semantic_parser: Callable[[bytes, LogicalQuery], Mapping[str, object]],
    ) -> PromotionDecision

    def promote_legacy_candidate(
        store: V52EvidenceStore, query: LogicalQuery,
        decision: PromotionDecision,
        semantic_parser: Callable[[bytes, LogicalQuery], Mapping[str, object]],
    ) -> PublishedEvidence

- [ ] PROMOTED requires every check. Otherwise return FETCH_REQUIRED with deterministic reason codes. The runner passes the contract-bound legacy root and protected-inventory path explicitly; no directory discovery or fallback path is permitted. Write a protected path reference with legacy manifest/seal/raw hashes, protected-inventory hash, parser version, and recomputed semantic hash. Create no copy, symlink, or hardlink.
- [ ] Run tests and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_legacy tests.test_manifest -v
    git add mlb_backtest/v52_legacy.py tests/test_v52_legacy.py tests/fixtures/v5_2_qd/legacy
    git commit -m "feat: promote verified legacy source evidence"

## Task 7: Build the resumable hybrid acquisition runner

**Files:**

- Create: mlb_backtest/v52_acquire.py
- Create: tests/test_v52_acquire.py
- Create: schemas/v5_2_schedule_anchor_inventory.schema.json
- Modify: mlb_backtest/v52_http.py
- Modify: mlb_backtest/v52_query_plan.py
- Modify: mlb_backtest/execution_registry.py

- [ ] Write tests for plan-only, hybrid promotion-before-fetch, schedule-before-dependent queries, one Savant/two MLB Stats concurrency, free-space reserve, Retry-After, deterministic retry, semantic quarantine, typed Statcast SplitRequired, --stop-after reference-inputs, stop/resume, and no rewrite/network call for verified successes. Plan-only and run modes both exit nonzero when --legacy-root/--protected-inventory is absent or differs from the contract-bound path/hash.
- [ ] Implement:

    @dataclass(frozen=True)
    class AcquisitionSummary:
        contract_sha256: str
        query_batch_sha256s: tuple[str, ...]
        promoted: int
        fetched: int
        resumed: int
        quarantined: int
        pending: int
        network_attempts: int

    def acquire_with_retries(
        store: V52EvidenceStore, query: LogicalQuery,
        query_parser: Callable[[bytes, LogicalQuery], Mapping[str, object]],
        *, policy: RetryPolicy, fetcher=fetch_attempt, sleeper=time.sleep,
    ) -> PublishedEvidence

    def acquire_sources(
        root: Path, run_spec: RunSpec, *, years: range,
        mode: str, resume: bool, stop_after: str | None,
        legacy_root: Path, protected_inventory_path: Path,
        reserve_bytes: int = 20 * 1024**3,
        fetcher=fetch_attempt,
    ) -> AcquisitionSummary

    def main(argv: Sequence[str] | None = None) -> int

- [ ] Permanent HTTP and semantic failures quarantine without retry. Transient HTTP/network failures persist every attempt and use Retry-After/backoff/jitter. Timeout retries exhaust the frozen policy before a multi-day Statcast query splits; a single-day query quarantines after exhaustion. Size-limit partial bodies are hash-quarantined. A SPLIT batch binding parent, reason, exhausted attempt receipt, and ordered children is published before child fetch; resume reuses it and never refetches the parent.
- [ ] CLI:

    python3 -m mlb_backtest.v52_acquire \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --years 2018:2024 \
      --mode hybrid \
      --legacy-root data/v5_1_bt/raw \
      --protected-inventory audit/v5_1_bt/protected_raw_inventory.json \
      --resume

- [ ] The runner initializes DESIGN_ACCEPTED when absent, transitions once to ACQUIRING, and never advances to SOURCES_READY. Stage names are exactly STATIC_SCHEDULES, REFERENCE_INPUTS, TIMEZONE_REVIEW, PARTICIPANT_DERIVED, and REMAINING. REFERENCE_INPUTS publishes audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json and validates it against schemas/v5_2_schedule_anchor_inventory.schema.json. Any pending or terminal quarantine makes the command exit nonzero.
- [ ] Run tests and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_acquire tests.test_v52_http tests.test_v52_query_plan -v
    git add mlb_backtest/v52_acquire.py mlb_backtest/v52_http.py mlb_backtest/v52_query_plan.py mlb_backtest/execution_registry.py schemas/v5_2_schedule_anchor_inventory.schema.json tests/test_v52_acquire.py
    git commit -m "feat: orchestrate hybrid v5.2 acquisition"

## Task 8: Build the offline grain-specific source verifier

**Files:**

- Create: mlb_backtest/v52_verify_sources.py
- Create: schemas/v5_2_source_inventory.schema.json
- Create: schemas/v5_2_source_review_receipt.schema.json
- Create: schemas/v5_2_normalized_snapshot.schema.json
- Create: tests/test_v52_verify_sources.py

- [ ] Write tests for owned/protected byte reread, semantic recomputation, sidecar forgery, split coverage, schedule played subset against Statcast, canceled games absent from Statcast, D-1 consumer mapping, preseason disposition, venue/date/team resolution, prior-history binding, inventory reproducibility, and zero network use.
- [ ] Implement:

    @dataclass(frozen=True)
    class SourceVerification:
        status: str
        inventory: Mapping[str, object]
        inventory_sha256: str
        critical_errors: tuple[Mapping[str, object], ...]
        network_attempts: int
        source_gate_ready: bool

    def verify_source_inventory(
        root: Path, run_spec: RunSpec,
        acquisition_contract: Mapping[str, object],
    ) -> SourceVerification

    def publish_normalized_snapshots(
        root: Path, verification: SourceVerification,
    ) -> tuple[Path, ...]

    def generate_source_review_selection(
        root: Path, *, inventory_path: Path,
    ) -> Path

    def write_source_review_receipt(
        root: Path, *, inventory_path: Path, selection_path: Path,
        reviewer_identity: str, decision: str,
    ) -> Path

    def write_source_inventory(
        root: Path, verification: SourceVerification,
    ) -> tuple[Path, Path]

    def main(argv: Sequence[str] | None = None) -> int

- [ ] The verifier safely opens every owned or protected raw object, recomputes its sidecar with the registered parser, validates attempt/query/split lineage, and then applies cross-source gates. It imports no fetch function.
- [ ] Compare Statcast to the schedule played-game subset, not the complete scheduled population.
- [ ] Set source_gate_ready=true only when every minimum source-season contract for 2018–2024 is READY and critical_errors is empty. Only preflight computes may_fit_H22 after the SOURCES_READY transition.
- [ ] Write normalized snapshots at data/v5_2_qd/historical/sources/{season}/{source}/{snapshot_sha256}.snapshot.json with a closed schema, source-child hashes, D-1 cutoff evidence, immutable manifest, and season inventory. Tests cover clean rebuild identity and path/symlink/hardlink attacks.
- [ ] Write the canonical inventory at data/v5_2_qd/historical/sources/source_inventory.json and a matching .sha256 file. Each --check-only run recomputes canonical inventory and snapshots in memory from evidence, exact-compares saved bytes and .sha256, asserts network_attempts=0, writes an immutable clean-replay receipt, and exits nonzero on any mismatch.
- [ ] The CLI is check-only by default. --advance-phase SOURCES_READY additionally requires --review-receipts PATH and --replay-receipts PATH, verifies that the manual audit and two clean-replay receipts bind the current inventory hash, snapshots, and reviewed query IDs, and then calls advance_execution_phase exactly once.
- [ ] Run tests and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_verify_sources -v
    git add mlb_backtest/v52_verify_sources.py schemas/v5_2_source_inventory.schema.json schemas/v5_2_source_review_receipt.schema.json schemas/v5_2_normalized_snapshot.schema.json tests/test_v52_verify_sources.py
    git commit -m "feat: verify grain-specific v5.2 sources"

## Task 9: Replace generic preflight readiness with verified inventory

**Files:**

- Modify: mlb_backtest/preflight.py
- Modify: tests/test_preflight.py
- Modify: schemas/v5_2_preflight.schema.json
- Modify generated preflight JSON/Markdown through production writers

- [ ] Add failing tests that reject generic reference-gamePk records, career-BF as annual pitching, full schedule/Statcast equality, forged sidecars, wrong raw root, READY without SOURCES_READY, and may_fit_H22 with any 2018–2024 source missing.
- [ ] Remove mlb_stats_pitcher_bf from the mlb_stats_pitching alias set. Treat career_pitcher_bf as a separate supporting entity grain.
- [ ] Replace legacy manifest scanning as readiness authority with the clean verified source inventory. Retain legacy scans only as diagnostic input to promotion.
- [ ] Remove the generic SIDECAR_KEYS and GAME_GRAIN_SOURCES/REFERENCE_GRAIN_SOURCES readiness path. Dispatch using explicit source contracts.
- [ ] Change preflight schema version because its serialized evidence contract changes incompatibly. preflight_schema_document() remains the schema authority; update parse_preflight required keys and render_preflight_markdown together, then generate schema/JSON/Markdown through write_preflight only.
- [ ] Correct exact_acquisition_steps to the real CLI and canonical raw root. Remove the stale --output data/v5_2_qd/raw command.
- [ ] Overall READY and may_fit_H22 both require: all 2018–2024 minimum rows READY, zero critical errors, inventory hash verified, and registry phase SOURCES_READY.
- [ ] Regenerate schema and reports only through production functions, run tests, and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_preflight tests.test_v52_verify_sources -v
    git add mlb_backtest/preflight.py tests/test_preflight.py schemas/v5_2_preflight.schema.json reports/v5_2_qd/walkforward_500_preflight.json reports/v5_2_qd/walkforward_500_preflight.md
    git commit -m "fix: gate preflight on verified v5.2 sources"

## Task 10: Make cross-artifact verification phase-aware

**Files:**

- Modify: mlb_backtest/verify_v52_design.py
- Modify: tests/test_verify_v52_design.py
- Modify: tests/test_execution_registry.py

- [ ] Add every new contract, schema, inventory, and canonical JSON artifact to EXPECTED_CURRENT_FILES, CANONICAL_JSON_FILES, CURRENT_SCOPE_TREE_ROOTS/CURRENT_SCOPE_SINGLE_FILES, and verify_contract_schemas.
- [ ] Add tests for exact namespace allowances in DESIGN_ACCEPTED, ACQUIRING, and SOURCES_READY; registry-phase mismatch; clean inventory recomputation; preflight mismatch; cards before CARDS_SEALED; outcomes before OUTCOMES_JOINED; and future phases failing closed until implemented.
- [ ] Add --expected-phase to the verifier CLI. When supplied, it must equal the registry head.
- [ ] Phase gates:

    DESIGN_ACCEPTED: preflight blocked; no source tree required.
    ACQUIRING: preflight blocked; raw/attempt/quarantine allowed; OOF/cards/locks/outcomes forbidden.
    SOURCES_READY: source inventory clean; preflight READY; may_fit_H22 true; cards still forbidden.

- [ ] Remove the hardcoded rule that any historical namespace is an error and the hardcoded demand for BLOCKED_REQUIRED_ACQUISITION.
- [ ] Run tests and commit.

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_verify_v52_design tests.test_execution_registry tests.test_preflight tests.test_v52_verify_sources -v
    git add mlb_backtest/verify_v52_design.py tests/test_verify_v52_design.py tests/test_execution_registry.py
    git commit -m "fix: verify v5.2 artifacts by execution phase"

## Task 11: Prove Plan A on a miniature clean replay

**Files:**

- Create: tests/test_v52_sources_e2e.py
- Create: tests/fixtures/v5_2_qd/e2e/fixture_contract.json
- Use runtime temporary legacy trees; do not commit raw duplicates

- [ ] Build a seven-season 2018–2024 fixture with one minimal source-season population per year; across the fixture include promoted and fetched children, played and canceled games, D-1 pitching records, a preseason disposition, career-BF records, an alternate venue, and prior-history projections.
- [ ] Test DESIGN_ACCEPTED → ACQUIRING → SOURCES_READY, byte-identical second verification, zero network on resume, unchanged protected fingerprints, canonical inventory, and fixture may_fit_H22=true.
- [ ] Add negative e2e cases for raw/sidecar mutation, population mismatch, timezone gap, interrupted publish, concurrent resume, registry fork, and hidden hardlink.
- [ ] Run focused suite, Ruff, and diff checks:

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
      tests.test_v52_acquisition_contract \
      tests.test_execution_registry \
      tests.test_v52_evidence \
      tests.test_v52_http \
      tests.test_v52_query_plan \
      tests.test_v52_source_schedule \
      tests.test_v52_source_statcast \
      tests.test_v52_source_pitching \
      tests.test_v52_source_references \
      tests.test_v52_source_history \
      tests.test_v52_timezone_review \
      tests.test_v52_legacy \
      tests.test_v52_acquire \
      tests.test_v52_verify_sources \
      tests.test_v52_sources_e2e \
      tests.test_preflight \
      tests.test_verify_v52_design -v
    python3 -m ruff check mlb_backtest tests
    git diff --check

- [ ] Commit the e2e proof.

    git add tests/test_v52_sources_e2e.py tests/fixtures/v5_2_qd/e2e
    git commit -m "test: prove v5.2 source readiness replay"

## Task 12: Full implementation verification before network use

- [ ] Run the full repository test suite:

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
    python3 -m ruff check .
    git diff --check

- [ ] Scan new production/contracts for forbidden placeholders:

    rg -n 'TODO|FIXME|TBD|NotImplementedError|^[[:space:]]*pass([[:space:]]|$)' mlb_backtest protocols/v5_2_qd schemas

Expected: no matches in new Plan A production code or contracts.

- [ ] Run an independent deep-reasoner review of query identity, protected promotion, raw semantic recomputation, phase ordering, preflight gate, and cross-source subset logic.
- [ ] Run a separate fast-worker review of schemas, fixture coverage, CLI help, test discovery, Ruff, generated report agreement, and accidental staging.
- [ ] Do not start network acquisition until both reviews approve.

## Task 13: Execute the real hybrid acquisition and seal SOURCES_READY

**Files produced by tools, never hand-edited:**

- audit/v5_2_qd/historical/acquisition/query_batches/
- audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json
- audit/v5_2_qd/historical/acquisition/timezone_reviews/
- audit/v5_2_qd/historical/acquisition/source_replays/
- audit/v5_2_qd/historical/acquisition/source_reviews/
- audit/v5_2_qd/historical/execution/phase_receipts/
- data/v5_2_qd/historical/raw/
- data/v5_2_qd/historical/sources/source_inventory.json
- data/v5_2_qd/historical/sources/source_inventory.sha256
- reports/v5_2_qd/walkforward_500_preflight.json
- reports/v5_2_qd/walkforward_500_preflight.md

- [ ] Confirm free space exceeds the 20 GiB reserve, hashes match, no H22/H23/H24 card exists, and the only pre-existing untracked paths are the two explicitly preserved user-owned duplicate files recorded at plan approval.
- [ ] Generate the offline plan:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --years 2018:2024 \
      --mode hybrid \
      --legacy-root data/v5_1_bt/raw \
      --protected-inventory audit/v5_1_bt/protected_raw_inventory.json \
      --plan-only

Expected: exit 0 and network_attempts=0. Each 2022–2024 child is classified PROMOTION_ELIGIBLE or FETCH_REQUIRED after exact protected-catalog matching; no source-level reuse assumption is allowed.

- [ ] Acquire and verify the complete 2018–2024 schedule/boxscore/venue stage, then stop before reference-dependent publication:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --years 2018:2024 \
      --mode hybrid \
      --legacy-root data/v5_1_bt/raw \
      --protected-inventory audit/v5_1_bt/protected_raw_inventory.json \
      --resume \
      --stop-after reference-inputs

Expected: schedule anchors, boxscores, and official venue payloads are verified; audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json enumerates all regular-season venue IDs; timezone-dependent aggregate snapshots are not yet published.

- [ ] Generate the observed-venue candidate under the audit namespace:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review \
      --root . \
      --schedule-inventory audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json \
      --write-candidate audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json

- [ ] Have a deep-reasoner review every venue identity, coordinates, IANA timezone, and effective range. Have a separate fast-worker verify complete venue/date coverage and absence of overlaps. Persist two review receipts with exact commands:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review --root . --candidate audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json --reviewer-identity deep-reasoner --review-scope all-observed-venues --decision APPROVED --write-review audit/v5_2_qd/historical/acquisition/timezone_reviews
    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review --root . --candidate audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json --reviewer-identity fast-worker --review-scope all-observed-venues --decision APPROVED --write-review audit/v5_2_qd/historical/acquisition/timezone_reviews
- [ ] Seal and commit the real mapping only after both reviews approve:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review \
      --root . \
      --candidate audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json \
      --review-receipts audit/v5_2_qd/historical/acquisition/timezone_reviews \
      --seal-output config/v5_2_qd_venue_timezones.json \
      --seal-receipt audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_timezone_review tests.test_v52_source_references -v
    git add config/v5_2_qd_venue_timezones.json
    git commit -m "data: seal observed 2018-2024 venue timezones"

- [ ] Resume the remaining hybrid acquisition:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --years 2018:2024 \
      --mode hybrid \
      --legacy-root data/v5_1_bt/raw \
      --protected-inventory audit/v5_1_bt/protected_raw_inventory.json \
      --resume

Expected: exit 0 only when every planned child is promoted or fetched; every failure has an attempt/quarantine receipt; successful children are immutable.

- [ ] Publish pass 1 inventory without advancing the phase:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_sources \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --write-inventory \
      --check-only \
      --write-replay-receipt audit/v5_2_qd/historical/acquisition/source_replays

Expected: source status=READY_CANDIDATE, critical_errors=[], network_attempts=0, and registry phase remains ACQUIRING.

- [ ] Run pass 2 from a clean process before any phase transition. It must exact-compare the saved inventory/snapshots, make zero network calls, and write a second clean-replay receipt:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_sources \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --check-only \
      --write-replay-receipt audit/v5_2_qd/historical/acquisition/source_replays

- [ ] Generate a deterministic review selection containing one promoted child and one fetched child for each storage mode/source combination that actually exists, plus three played games per year. For prior_schedule_history, select source-game projection bindings instead of web children:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_sources \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --inventory data/v5_2_qd/historical/sources/source_inventory.json \
      --generate-review-selection audit/v5_2_qd/historical/acquisition/source_review_selection.json

- [ ] An independent auditor recomputes raw hash, query-sidecar hash, snapshot hash, query lineage, schedule/Statcast subset, D-1 mapping, participants, and timezone for every selected item. Publish the canonical source review receipt with an identity distinct from the acquisition worker:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_sources \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --inventory data/v5_2_qd/historical/sources/source_inventory.json \
      --reviewer-identity independent-source-auditor \
      --review-selection audit/v5_2_qd/historical/acquisition/source_review_selection.json \
      --decision APPROVED \
      --write-review audit/v5_2_qd/historical/acquisition/source_reviews
- [ ] Advance exactly once, supplying the independent audit receipt:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_sources \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --check-only \
      --advance-phase SOURCES_READY \
      --review-receipts audit/v5_2_qd/historical/acquisition/source_reviews \
      --replay-receipts audit/v5_2_qd/historical/acquisition/source_replays

Expected: phase=SOURCES_READY, critical_errors=[], network_attempts=0, and the phase receipt binds pass-1 inventory, both clean-replay receipts, and the manual-review receipt hashes.

- [ ] Hash the inventory, run a clean-process check, and require identical bytes and zero network:

    shasum -a 256 data/v5_2_qd/historical/sources/source_inventory.json
    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_sources \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --check-only
    shasum -a 256 data/v5_2_qd/historical/sources/source_inventory.json

- [ ] Regenerate and verify preflight:

    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.preflight \
      --root . \
      --run-spec protocols/v5_2_qd/historical_walkforward_500.json \
      --json reports/v5_2_qd/walkforward_500_preflight.json \
      --markdown reports/v5_2_qd/walkforward_500_preflight.md \
      --schema schemas/v5_2_preflight.schema.json
    PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.verify_v52_design \
      --root . \
      --skip-html \
      --expected-phase SOURCES_READY

Expected: preflight READY, all 2018–2024 source-season rows READY, may_fit_H22=true, cards absent, protected v5.1 evidence unchanged, phase SOURCES_READY.


## Task 14: Plan A acceptance and Plan B handoff

- [ ] Run full tests, Ruff, placeholder scan, and git diff --check again.
- [ ] Record actual bytes by source/year, promoted/fetched/resumed/quarantined counts, quarantine reason codes, inventory SHA-256, preflight SHA-256, code commit, and elapsed acquisition time.
- [ ] Confirm:

    execution phase = SOURCES_READY
    required years = 2018..2024
    every minimum source-season contract = READY
    critical source errors = 0
    offline replay network attempts = 0
    may_fit_H22 = true
    H22/H23/H24 fitted cards = absent
    protected v5.1 fingerprint = unchanged

- [ ] Write the separate Plan B implementation plan for result-free features, O19–O23 sealed OOF ledgers, and H22/H23/H24 rolling cards. Do not fit inside this plan.

## Final verification checklist

- [ ] Every JSON artifact is canonical, has one final newline, rejects unknown keys, and validates against its committed schema.
- [ ] Every source verifier recomputes semantics from owned/protected raw bytes.
- [ ] Every protected reference is path-, inode-, manifest-, seal-, raw-hash-, parser-, inventory-, and semantic-hash-bound.
- [ ] Schedule/Statcast comparison uses only the schedule played-game subset.
- [ ] Pitching and career-BF remain distinct grains.
- [ ] Every observed venue/date resolves to one reviewed IANA timezone row.
- [ ] The second verifier pass performs zero network calls and reproduces inventory bytes.
- [ ] Preflight cannot become READY from forged manifests or sidecars.
- [ ] No fit, target lock, outcome join, or 500-sample artifact exists at Plan A completion.
