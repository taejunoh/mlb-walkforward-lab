# MLB v5.2 Duplicate gamePk 565534 Ephemeral parse_schedule Equivalence Addendum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every production change and superpowers:verification-before-completion before every completion claim. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind the approved ephemeral parse_schedule addendum as the exact fourth superseding authority, rehash every dependent disposition identity, and prove direct ordered equivalence through the unchanged public parser without persisting temporary bytes.

**Architecture:** Task 1 changes only the superseding contract/schema and the two existing authenticated Git-rebind owners. Task 2 preserves the current five-file Task 4 work, makes the disposition requested range JSON-compatible, and adds one in-memory parser call whose helper returns only normalized games. Task 3 verifies the committed authority slice, parser equivalence, protected evidence, network prohibition, and staged whitelist. The governing plan’s Task 5 still owns authority= dispatch and derived-sidecar publication; this addendum does not implement those or any downstream service.

**Tech Stack:** Python 3.11, frozen dataclasses, canonical JSON, SHA-256, JSON Schema Draft 2020-12, unittest, unittest.mock, Ruff, and Git HEAD/index/worktree identity checks.

---

## Fixed scope and authority

Run from /Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest.

Authenticate the committed addendum before Task 1:

~~~bash
git diff --exit-code -- docs/superpowers/specs/2026-07-22-v5-2-duplicate-gamepk-565534-ephemeral-parse-schedule-equivalence-addendum.md
test "$(shasum -a 256 docs/superpowers/specs/2026-07-22-v5-2-duplicate-gamepk-565534-ephemeral-parse-schedule-equivalence-addendum.md | awk '{print $1}')" = "b6dbc409dc51e03ed1ff7d95a850fb6be87d4ada7b6a37cc34f38efb51281825"
test "$(git rev-parse HEAD:docs/superpowers/specs/2026-07-22-v5-2-duplicate-gamepk-565534-ephemeral-parse-schedule-equivalence-addendum.md)" = "1eb934c37aaf88a9b2f6732e432bb3b9bfb7f06a"
~~~

Expected: no diff, SHA-256 b6dbc409dc51e03ed1ff7d95a850fb6be87d4ada7b6a37cc34f38efb51281825, and blob 1eb934c37aaf88a9b2f6732e432bb3b9bfb7f06a.

Use these complete literals:

~~~python
ADDENDUM_BINDING = {
    "path": "docs/superpowers/specs/2026-07-22-v5-2-duplicate-gamepk-565534-ephemeral-parse-schedule-equivalence-addendum.md",
    "sha256": "b6dbc409dc51e03ed1ff7d95a850fb6be87d4ada7b6a37cc34f38efb51281825",
    "git_blob_id": "1eb934c37aaf88a9b2f6732e432bb3b9bfb7f06a",
}
STALE_CONTRACT_SHA256 = "1f2facf18390ec703fbd68b05327465d0c12d0a3a472bfdb654ca103c44958ac"
NEW_CONTRACT_SHA256 = "3ddf83fdec9a328f5005fe8f62f4f680f4ef81516acd1aef2f90845b11c11d58"
STALE_DISPOSITION_QUERY_ID = "a132f270531aa38ca38c7b053fcce2474c3692eca0fbcee2989a734646900fe7"
NEW_DISPOSITION_QUERY_ID = "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095"
~~~

Never edit or stage the protected duplicate files, default authorities, source parser, or historical evidence:

- mlb_backtest/v52_query_plan 2.py
- tests/test_v52_query_plan 2.py
- schemas/v5_2_preflight.schema 2.json
- reports/v5_2_qd/walkforward_500_preflight 2.md
- protocols/v5_2_qd/historical_walkforward_500.json
- protocols/v5_2_qd/historical_source_acquisition.json
- mlb_backtest/source_parsers/schedule.py
- every existing file under data/v5_2_qd/historical/
- every existing file under audit/v5_2_qd/historical/

### Task 1: Bind and Git-rebind the fourth superseding authority

Files:
- Modify: protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json
- Modify: schemas/v5_2_acquisition_contract.schema.json
- Modify: mlb_backtest/v52_query_plan.py
- Modify: mlb_backtest/execution_registry.py
- Test: tests/test_v52_acquisition_contract.py
- Test: tests/test_v52_query_plan.py
- Test: tests/test_execution_registry.py
- Test: tests/test_v52_schedule_inventory.py

No receipt field/schema changes are authorized. Do not edit v52_schedule_inventory.py, protocols.py, verify_v52_design.py, v52_evidence.py, v52_acquire.py, or downstream services.

- [ ] Step 1: Write red contract/query-plan tests using committed temporary repositories.

Add ADDENDUM_BINDING, STALE_CONTRACT_SHA256, and NEW_CONTRACT_SHA256 as exact test constants. Add the addendum path to the existing _STATCAST_AUTHORITY_FILES fixture tuple so every temporary repository contains the committed source authority.

In tests/test_v52_acquisition_contract.py:

~~~python
def test_superseding_contract_has_exact_fourth_binding(self):
    self.assertEqual(
        set(self.contract["bindings"]),
        {
            "approved_design",
            "implementation_plan",
            "run_spec",
            "ephemeral_parse_schedule_equivalence_addendum",
        },
    )
    self.assertEqual(
        self.contract["bindings"][
            "ephemeral_parse_schedule_equivalence_addendum"
        ],
        ADDENDUM_BINDING,
    )
~~~

In tests/test_v52_query_plan.py, add temp-repository tests named:

~~~python
import mlb_backtest.v52_query_plan as query_plan


def test_source_context_authenticates_equivalence_addendum_sha_and_blob(self):
    with tempfile.TemporaryDirectory() as directory:
        root = _statcast_authority_fixture(Path(directory))
        context = query_plan._load_static_contracts(
            root / SOURCE_RUN_SPEC.relative_to(ROOT),
            root / SOURCE_CONTRACT.relative_to(ROOT),
        )
    self.assertEqual(context.acquisition_contract_sha256, NEW_CONTRACT_SHA256)

def test_source_context_rejects_dirty_wrong_blob_and_racing_addendum(self):
    import mlb_backtest.v52_query_plan as query_plan

    relative = Path(ADDENDUM_BINDING["path"])
    for attack in ("dirty", "wrong_blob", "race"):
        with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
            root = _statcast_authority_fixture(Path(directory))
            run_path = root / SOURCE_RUN_SPEC.relative_to(ROOT)
            contract_path = root / SOURCE_CONTRACT.relative_to(ROOT)
            addendum_path = root / relative
            if attack == "dirty":
                addendum_path.write_bytes(addendum_path.read_bytes() + b"\n")
                with self.assertRaisesRegex(ValueError, "addendum|binding"):
                    query_plan._load_static_contracts(run_path, contract_path)
            elif attack == "wrong_blob":
                contract = json.loads(contract_path.read_bytes())
                contract["bindings"]["ephemeral_parse_schedule_equivalence_addendum"]["git_blob_id"] = "0" * 40
                contract_path.write_bytes(query_plan._canonical_json_bytes(contract) + b"\n")
                schema_path = root / "schemas/v5_2_acquisition_contract.schema.json"
                schema = json.loads(schema_path.read_bytes())
                schema["oneOf"][1]["const"] = contract
                schema_path.write_bytes(query_plan._canonical_json_bytes(schema) + b"\n")
                subprocess.run(["git", "add", contract_path, schema_path], cwd=root, check=True)
                subprocess.run(["git", "commit", "-qm", "wrong addendum blob"], cwd=root, check=True)
                with self.assertRaisesRegex(ValueError, "addendum|Git blob|binding"):
                    query_plan._load_static_contracts(run_path, contract_path)
            else:
                original = query_plan._verify_head_blob
                def mutate(*args, **kwargs):
                    relative_arg = args[1]
                    if relative_arg == relative:
                        addendum_path.write_bytes(
                            addendum_path.read_bytes() + b"\n"
                        )
                    return original(*args, **kwargs)
                with mock.patch.object(query_plan, "_verify_head_blob", side_effect=mutate):
                    with self.assertRaisesRegex(ValueError, "addendum|binding"):
                        query_plan._load_static_contracts(run_path, contract_path)
~~~

Use the three concrete branches above as written. Each branch must assert ValueError matching "addendum|Git blob|binding", and the temporary-directory boundary must leave the real repository untouched.

- [ ] Step 2: Run only the red tests that operate on committed temporary fixtures.

~~~bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_acquisition_contract.V52SupersedingAcquisitionContractTest.test_superseding_contract_has_exact_fourth_binding \
  tests.test_v52_query_plan.V52QueryPlanTest.test_source_context_authenticates_equivalence_addendum_sha_and_blob \
  tests.test_v52_query_plan.V52QueryPlanTest.test_source_context_rejects_dirty_wrong_blob_and_racing_addendum -v
~~~

Expected: failures identify the missing fourth binding/path/blob authentication. Do not run real-root Git-clean suites while the authority slice is uncommitted.

- [ ] Step 3: Implement the exact query-plan binding.

Add these module constants in mlb_backtest/v52_query_plan.py:

~~~python
_SOURCE_EQUIVALENCE_ADDENDUM_RELATIVE = Path(
    "docs/superpowers/specs/"
    "2026-07-22-v5-2-duplicate-gamepk-565534-"
    "ephemeral-parse-schedule-equivalence-addendum.md"
)
ADDENDUM_BINDING = {
    "path": _SOURCE_EQUIVALENCE_ADDENDUM_RELATIVE.as_posix(),
    "sha256": "b6dbc409dc51e03ed1ff7d95a850fb6be87d4ada7b6a37cc34f38efb51281825",
    "git_blob_id": "1eb934c37aaf88a9b2f6732e432bb3b9bfb7f06a",
}
~~~

Extend _AuthorityBranch with an optional source-equivalence field while retaining amendment_relative:

~~~python
@dataclass(frozen=True)
class _AuthorityBranch:
    authority_relative: Path
    contract_relative: Path
    design_relative: Path
    plan_relative: Path
    amendment_relative: Path | None
    equivalence_addendum_relative: Path | None
    schema_index: int
~~~

Set equivalence_addendum_relative=None for the old branch and _SOURCE_EQUIVALENCE_ADDENDUM_RELATIVE for the source branch. Replace the current `if branch.amendment_relative is None` block completely with mutually exclusive source/default handling. The source branch must preserve `contract.get("supersedes") == _SOURCE_SUPERSEDES`, require the exact four source binding names, capture binding bytes, and authenticate the bound blob with the existing helper:

~~~python
if branch.equivalence_addendum_relative is not None:
    if set(bindings) != {
        "approved_design",
        "implementation_plan",
        "run_spec",
        "ephemeral_parse_schedule_equivalence_addendum",
    } or contract.get("supersedes") != _SOURCE_SUPERSEDES:
        raise ValueError("superseding acquisition authority differs")
    raw_addendum = bindings.get(
        "ephemeral_parse_schedule_equivalence_addendum"
    )
    if raw_addendum != ADDENDUM_BINDING:
        raise ValueError("superseding addendum binding differs")
    addendum_contents = _verify_repository_binding(
        root,
        raw_addendum,
        expected_path=branch.equivalence_addendum_relative,
        label="ephemeral parse_schedule equivalence addendum",
        digest_key="sha256",
        extra_keys=frozenset({"git_blob_id"}),
    )
    _verify_head_blob(
        root,
        branch.equivalence_addendum_relative,
        addendum_contents,
        "ephemeral parse_schedule equivalence addendum",
        expected_blob=raw_addendum.get("git_blob_id"),
    )
    if _verify_repository_binding(
        root,
        raw_addendum,
        expected_path=branch.equivalence_addendum_relative,
        label="ephemeral parse_schedule equivalence addendum",
        digest_key="sha256",
        extra_keys=frozenset({"git_blob_id"}),
    ) != addendum_contents:
        raise ValueError("superseding addendum changed during authentication")
elif branch.amendment_relative is not None:
    _verify_repository_binding(
        root,
        bindings.get("plan_a_amendment"),
        expected_path=branch.amendment_relative,
        label="Plan A amendment",
    )
else:
    raise AssertionError("authority branch supplemental binding is undefined")
~~~

Keep the old Plan A branch unchanged. Update only the source schema const/closed properties and canonical superseding contract. Assert its canonical SHA-256 is NEW_CONTRACT_SHA256 and differs from STALE_CONTRACT_SHA256.

- [ ] Step 4: Generalize the registry’s supplemental binding unconditionally.

Rename _plan_a_amendment_binding to _supplemental_authority_binding. Select exactly one supplemental binding by authenticated contract run_id and validate the addendum as a three-key SHA/blob binding:

~~~python
def _supplemental_authority_binding(
    root: Path,
    evidence: Mapping[str, object],
) -> Mapping[str, object]:
    contract_binding = _validate_binding_value(
        evidence["acquisition_contract"], "acquisition contract"
    )
    _, contents = _read_path(
        root,
        root / str(contract_binding["path"]),
        "acquisition contract",
    )
    contract = _canonical_object(contents, "acquisition contract")
    bindings = contract.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("acquisition contract bindings differ")
    if contract.get("run_id") == "historical_walkforward_500":
        return _artifact_binding(
            root, bindings.get("plan_a_amendment"), "Plan A amendment"
        )
    if contract.get("run_id") == "historical_walkforward_500_superseding_565534":
        return _artifact_binding(
            root,
            bindings.get("ephemeral_parse_schedule_equivalence_addendum"),
            "ephemeral parse_schedule equivalence addendum",
            plan=True,
        )
    raise ValueError("acquisition contract run_id differs")
~~~

Call _supplemental_authority_binding unconditionally from _git_bound_paths, _capture_git_snapshot, and _validate_common_bindings. Include its path in bound paths/expected bindings, so existing _capture_git_file validates HEAD, index, worktree SHA, and git_blob_id. Do not add a receipt field: the supplemental binding is derived from the already bound acquisition contract.

Add exact tests in tests/test_execution_registry.py:

- test_source_addendum_is_in_git_bound_paths_and_common_bindings
- test_source_addendum_rejects_dirty_worktree_and_committed_blob_drift
- test_source_addendum_replacement_race_fails_final_git_snapshot

Add the addendum file to every source temporary-repository fixture. Update tests/test_v52_schedule_inventory.py fixture construction so its committed source fixture includes the addendum; production schedule-inventory code remains unchanged.

- [ ] Step 5: Run precommit green only in committed temporary fixtures, then atomically commit Task 1.

~~~bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_query_plan.V52QueryPlanTest.test_source_context_authenticates_equivalence_addendum_sha_and_blob \
  tests.test_v52_query_plan.V52QueryPlanTest.test_source_context_rejects_dirty_wrong_blob_and_racing_addendum \
  tests.test_execution_registry.ExecutionRegistryTest.test_source_addendum_is_in_git_bound_paths_and_common_bindings \
  tests.test_execution_registry.ExecutionRegistryTest.test_source_addendum_rejects_dirty_worktree_and_committed_blob_drift \
  tests.test_execution_registry.ExecutionRegistryTest.test_source_addendum_replacement_race_fails_final_git_snapshot -v
ruff check mlb_backtest/v52_query_plan.py mlb_backtest/execution_registry.py tests/test_v52_acquisition_contract.py tests/test_v52_query_plan.py tests/test_execution_registry.py tests/test_v52_schedule_inventory.py
git diff --check
~~~

Expected: committed temporary fixtures pass; no real-root test requires the uncommitted schema to match HEAD.

~~~bash
git add protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json schemas/v5_2_acquisition_contract.schema.json mlb_backtest/v52_query_plan.py mlb_backtest/execution_registry.py tests/test_v52_acquisition_contract.py tests/test_v52_query_plan.py tests/test_execution_registry.py tests/test_v52_schedule_inventory.py
git commit -m "feat: bind ephemeral equivalence authority"
~~~

After this atomic commit, run the real-root suites:

~~~bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_acquisition_contract tests.test_v52_query_plan tests.test_execution_registry tests.test_v52_schedule_inventory -v
~~~

Expected: all pass, default branch behavior remains unchanged, and the source context reports contract SHA 3ddf83fdec9a328f5005fe8f62f4f680f4ef81516acd1aef2f90845b11c11d58.

### Task 2: Prove direct ordered equivalence through one public parser call

Files (exactly five):
- Modify: mlb_backtest/v52_duplicate_gamepk_disposition.py
- Modify: tests/test_v52_source_schedule.py
- Preserve: tests/fixtures/v52_duplicate_gamepk_565534/exact_pair.json
- Preserve: tests/fixtures/v52_duplicate_gamepk_565534/ordinary_games.json
- Preserve: tests/fixtures/v52_duplicate_gamepk_565534/negative_cases.json

Preserve current exact-pair authentication, fixed predicates, raw/date/duplicate counts, ordinary reconciliation, both digests, selected provenance, deep-freeze, and tamper failures.

- [ ] Step 1: Write red authority-ID and single-parser-boundary tests.

In tests/test_v52_source_schedule.py, build the real authenticated authority and make its identity the primary query test:

~~~python
authority = build_duplicate_disposition_authority(
    ROOT,
    source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
    acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
)
self.assertEqual(
    logical_query_id(authority.query),
    "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095",
)
self.assertNotEqual(
    logical_query_id(authority.query),
    "a132f270531aa38ca38c7b053fcce2474c3692eca0fbcee2989a734646900fe7",
)
~~~

Replace the old no-serialization test with one fully defined test. It wraps the module’s parse_schedule once, captures that invocation’s bytes and returned list, reconstructs the expected view from authenticated raw with only the exact suppression, and never parses twice:

~~~python
def test_2019_projection_uses_one_canonical_ephemeral_parser_call(self):
    from mlb_backtest.source_parsers.schedule import parse_schedule as public_parse_schedule

    authority = build_duplicate_disposition_authority(
        ROOT,
        source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
        acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
    )
    authenticated_raw = (ROOT / authority.old_raw_relative).read_bytes()
    expected_view = json.loads(authenticated_raw)
    expected_view["dates"][54]["games"].pop(5)
    expected_view["dates"][54]["totalGames"] -= 1
    expected_view["totalGames"] -= 1
    observed: dict[str, object] = {}

    def capture(raw: bytes | Path) -> list[dict]:
        self.assertIsInstance(raw, bytes)
        decoded = json.loads(raw.decode("utf-8"))
        parsed = public_parse_schedule(raw)
        observed["bytes"] = raw
        observed["decoded"] = decoded
        observed["parsed"] = parsed
        return parsed

    superseding_namespaces = (
        ROOT / "data/v5_2_qd/historical_superseding_565534",
        ROOT / "audit/v5_2_qd/historical_superseding_565534",
        ROOT / "reports/v5_2_qd/historical_superseding_565534",
    )
    self.assertTrue(all(not path.exists() for path in superseding_namespaces))
    with patch.object(disposition_module, "parse_schedule", side_effect=capture) as parser:
        projection = project_schedule_occurrences(authority)
    self.assertEqual(parser.call_count, 1)
    self.assertEqual(
        observed["bytes"],
        disposition_module.canonical_json_bytes(observed["decoded"]),
    )
    self.assertEqual(observed["decoded"], expected_view)
    self.assertEqual(
        _plain_json(projection.normalized_games),
        observed["parsed"],
    )
    self.assertFalse(_contains_bytes(authority))
    self.assertFalse(_contains_bytes(projection))
    self.assertTrue(all(not path.exists() for path in superseding_namespaces))
~~~

Define the recursive test helper in the same file:

~~~python
def _contains_bytes(value: object) -> bool:
    if isinstance(value, bytes):
        return True
    if isinstance(value, Mapping):
        return any(
            _contains_bytes(key) or _contains_bytes(child)
            for key, child in value.items()
        )
    if isinstance(value, (tuple, list)):
        return any(_contains_bytes(child) for child in value)
    if hasattr(value, "__dict__"):
        return _contains_bytes(vars(value))
    return False
~~~

Add a second test that patches parse_schedule to return a copy with one reordered or changed game and asserts ValueError before any namespace exists.

- [ ] Step 2: Run the red Task 2 tests after the authority commit.

~~~bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_source_schedule.V52ScheduleSourceTest.test_565534_exact_pair_retains_only_resumed_record_and_binds_disposition \
  tests.test_v52_source_schedule.V52ScheduleSourceTest.test_2019_projection_uses_one_canonical_ephemeral_parser_call -v
~~~

Expected: logical_query_id currently fails because _requested_range contains mappingproxy values, and the parser boundary fails because public parse_schedule is not called.

- [ ] Step 3: Make requested_range plain JSON and add a normalized-games-only helper.

Change _requested_range to return nested plain dictionaries, not deep_freeze or MappingProxyType:

~~~python
def _requested_range() -> dict[str, object]:
    return {
        "year": 2019,
        "start_date": "2019-03-20",
        "end_date": "2019-09-29",
        "disposition": "SCHEDULE_DUPLICATE_GAMEPK_565534",
        "game_pk": 565534,
        "old_authority": {
            "query_id": OLD_QUERY_ID,
            "query_registration": {
                "path": str(OLD_REGISTRATION_RELATIVE),
                "sha256": OLD_REGISTRATION_SHA256,
            },
            "quarantine_receipt": {
                "path": str(OLD_QUARANTINE_RELATIVE),
                "sha256": OLD_QUARANTINE_SHA256,
            },
            "raw": {
                "path": str(OLD_RAW_RELATIVE),
                "sha256": OLD_RAW_SHA256,
                "byte_count": OLD_RAW_BYTE_COUNT,
            },
        },
        "suppressed_occurrence": {
            "date_index": 54,
            "game_index": 5,
            "sha256": SUPPRESSED_OBJECT_SHA256,
        },
        "retained_occurrence": {
            "date_index": 161,
            "game_index": 12,
            "sha256": RETAINED_OBJECT_SHA256,
        },
    }
~~~

Add a helper that constructs bytes locally, immediately calls unchanged parse_schedule(bytes), deletes the local bytes, and returns only normalized games:

~~~python
def _parse_ephemeral_filtered_view(
    payload: Mapping[str, object],
) -> list[dict]:
    filtered_dates = []
    for date_index, date_entry in enumerate(payload["dates"]):
        games = list(date_entry["games"])
        if date_index == SUPPRESSED_POSITION[0]:
            games.pop(SUPPRESSED_POSITION[1])
        filtered_dates.append(
            {**date_entry, "games": games, "totalGames": len(games)}
        )
    filtered_view = {
        **payload,
        "dates": filtered_dates,
        "totalGames": payload["totalGames"] - 1,
    }
    ephemeral_bytes = canonical_json_bytes(filtered_view)
    try:
        return parse_schedule(ephemeral_bytes)
    finally:
        del ephemeral_bytes
~~~

In _project_payload, compare the complete ordered outputs before the existing digest checks:

~~~python
mirrored_normalized = [game for game, _ in selected]
parser_normalized = _parse_ephemeral_filtered_view(payload)
if parser_normalized != mirrored_normalized:
    raise ValueError(
        "565534 projection differs from unchanged parse_schedule output"
    )
if canonical_json_bytes(parser_normalized) != canonical_json_bytes(
    mirrored_normalized
):
    raise ValueError("565534 normalized output canonical equality differs")
normalized = mirrored_normalized
~~~

No helper may return bytes. Do not add bytes to DuplicateGamePkDispositionAuthority or ScheduleOccurrenceProjection, and do not edit source_parsers/schedule.py.

- [ ] Step 4: Verify and commit Task 2.

~~~bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_schedule tests.test_sources -v
ruff check mlb_backtest/v52_duplicate_gamepk_disposition.py tests/test_v52_source_schedule.py
git diff --check
git diff --exit-code -- mlb_backtest/source_parsers/schedule.py
~~~

Expected: tests pass; the real authority query ID is 8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095; parser invocation count is one; the argument is canonical and equals the raw copy with only (54,5) removed and totals copied; projection equals captured parser output; no bytes or superseding namespace exists; every output/digest/pair/totals tamper fails closed.

~~~bash
git add mlb_backtest/v52_duplicate_gamepk_disposition.py tests/test_v52_source_schedule.py tests/fixtures/v52_duplicate_gamepk_565534/exact_pair.json tests/fixtures/v52_duplicate_gamepk_565534/ordinary_games.json tests/fixtures/v52_duplicate_gamepk_565534/negative_cases.json
git commit -m "feat: prove ephemeral parse schedule equivalence"
~~~

### Task 3: Final offline, protected-evidence, and whitelist verification

Files:
- Test only; no production or evidence writes

- [ ] Step 1: Capture old evidence before verification.

~~~bash
V52_565534_AUDIT_DIR="$(mktemp -d)"
find data/v5_2_qd/historical audit/v5_2_qd/historical -type f -print0 \
  | xargs -0 shasum -a 256 | LC_ALL=C sort > "$V52_565534_AUDIT_DIR/old-evidence.sha256"
find data/v5_2_qd/historical audit/v5_2_qd/historical -type f | wc -l > "$V52_565534_AUDIT_DIR/old-evidence.count"
~~~

Expected: only out-of-repository manifest files are created.

- [ ] Step 2: Run committed real-root suites with explicit network prohibition.

Run the suite inside one process that rejects every socket connection and `urllib.request.urlopen` call. Clear inherited proxy bypass variables and retain dead proxy endpoints as a second boundary:

~~~bash
PYTHONDONTWRITEBYTECODE=1 NO_PROXY='' no_proxy='' HTTP_PROXY='http://127.0.0.1:9' HTTPS_PROXY='http://127.0.0.1:9' ALL_PROXY='http://127.0.0.1:9' python3 - <<'PY'
import socket
import unittest
from unittest.mock import patch


def network_forbidden(*args, **kwargs):
    raise AssertionError("network forbidden")


names = (
    "tests.test_v52_acquisition_contract",
    "tests.test_v52_query_plan",
    "tests.test_execution_registry",
    "tests.test_v52_schedule_inventory",
    "tests.test_v52_source_schedule",
    "tests.test_sources",
)
suite = unittest.defaultTestLoader.loadTestsFromNames(names)
with (
    patch("socket.create_connection", side_effect=network_forbidden),
    patch.object(socket.socket, "connect", network_forbidden),
    patch("urllib.request.urlopen", side_effect=network_forbidden),
):
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
PY
~~~

Expected: all suites pass from committed HEAD; any attempted network connection fails the suite immediately with `AssertionError("network forbidden")`.

- [ ] Step 3: Recheck protected bytes, parser, lint, and whitespace.

~~~bash
shasum -a 256 -c "$V52_565534_AUDIT_DIR/old-evidence.sha256"
test "$(find data/v5_2_qd/historical audit/v5_2_qd/historical -type f | wc -l | tr -d ' ')" = "$(tr -d ' ' < "$V52_565534_AUDIT_DIR/old-evidence.count")"
git diff --exit-code -- protocols/v5_2_qd/historical_walkforward_500.json protocols/v5_2_qd/historical_source_acquisition.json data/v5_2_qd/historical audit/v5_2_qd/historical
git diff --exit-code -- mlb_backtest/source_parsers/schedule.py
git status --short -- "mlb_backtest/v52_query_plan 2.py" "tests/test_v52_query_plan 2.py" "schemas/v5_2_preflight.schema 2.json" "reports/v5_2_qd/walkforward_500_preflight 2.md"
ruff check mlb_backtest/v52_query_plan.py mlb_backtest/execution_registry.py mlb_backtest/v52_duplicate_gamepk_disposition.py tests/test_v52_acquisition_contract.py tests/test_v52_query_plan.py tests/test_execution_registry.py tests/test_v52_schedule_inventory.py tests/test_v52_source_schedule.py
git diff --check
~~~

Expected: manifest and count match, old/default files and parser are unchanged, protected duplicate status is pre-existing only, Ruff passes, and whitespace is clean.

- [ ] Step 4: Enforce the exact staged whitelist.

~~~bash
git diff --cached --name-only
~~~

Expected: every staged path is an explicit subset of this list; v52_schedule_inventory.py, old evidence, protected duplicates, governing authorities, and this addendum plan are absent:

~~~text
protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json
schemas/v5_2_acquisition_contract.schema.json
mlb_backtest/v52_query_plan.py
mlb_backtest/execution_registry.py
mlb_backtest/v52_duplicate_gamepk_disposition.py
tests/test_v52_acquisition_contract.py
tests/test_v52_query_plan.py
tests/test_execution_registry.py
tests/test_v52_schedule_inventory.py
tests/test_v52_source_schedule.py
tests/fixtures/v52_duplicate_gamepk_565534/exact_pair.json
tests/fixtures/v52_duplicate_gamepk_565534/ordinary_games.json
tests/fixtures/v52_duplicate_gamepk_565534/negative_cases.json
~~~

- [ ] Step 5: Stop at the governing Task 5 handoff.

Do not implement authority= dispatch, derived-sidecar publication, checkpoint advancement, acquisition, or downstream services. Report only committed test/lint/diff results, old-evidence verification, contract SHA 3ddf83fdec9a328f5005fe8f62f4f680f4ef81516acd1aef2f90845b11c11d58, disposition query ID 8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095, and commit IDs.

## Self-review checklist and results

- [x] Exact three-task scope; Task 1 production files are fixed to contract, schema, query plan, and execution registry.
- [x] Query-plan binding defines the addendum constant/path, branch field, repository-byte capture, and existing _verify_head_blob call.
- [x] Registry supplemental binding is unconditional for old/source, adds no receipt field, and participates in bound paths, snapshots, common validation, dirty/blob/race tests.
- [x] Primary query identity is logical_query_id(authority.query), enabled by plain JSON requested_range.
- [x] Parser test performs one wrapped invocation, proves canonical full filtered view, compares projection to captured parser result, and checks no bytes/namespaces.
- [x] Precommit green uses committed temporary fixtures; real-root Git-clean suites run only after atomic authority commit.
- [x] Network prohibition uses dead proxies plus explicit monkeypatch and no NO_PROXY override.
- [x] Protected evidence, parser immutability, staged whitelist, exact hashes/IDs, and governing Task 5 handoff are explicit.
