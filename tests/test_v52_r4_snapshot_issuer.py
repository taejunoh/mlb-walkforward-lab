import hashlib
import io
import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

from mlb_backtest.v52_r4_apfs import CUSTODY_UNAVAILABLE
from mlb_backtest.v52_r4_snapshot_issuer import (
    IssuanceOwner,
    IssuanceState,
    SnapshotSourceClosure,
    acquire_issuance_lock,
    observe_issuance_state,
    populate_snapshot,
    _populate_snapshot_for_test,
    _populate_snapshot_from_authority,
    _SourceAuthorityBinding,
    _AuthenticatedSnapshot,
    _AuthenticatedAnchor,
    _AuthenticatedInventory,
    _AuthenticatedReceipt,
    _AuthenticatedContract,
    _AuthenticatedCheckpointChain,
    _AuthenticatedTerminal,
    _observe_issuance_state,
    _r4_authority_layout,
    _bind_r4_authority_for_test,
    _read_source_closure,
    _read_source_closure_from_root,
    publish_canonical_json,
    publish_owned_snapshot,
    validate_source_closure,
)
from mlb_backtest.secure_fs import open_trusted_root


class LiveOwnerPublicApiRedTests(unittest.TestCase):
    """The public transaction boundary must not be an ad-hoc owner protocol."""

    def test_public_live_owner_types_are_frozen(self) -> None:
        from dataclasses import is_dataclass
        from mlb_backtest.v52_r4_snapshot_issuer import IssuanceInputs, IssuanceResult

        self.assertTrue(is_dataclass(IssuanceInputs))
        self.assertTrue(is_dataclass(IssuanceResult))
        self.assertEqual(IssuanceInputs.__dataclass_params__.frozen, True)
        self.assertEqual(IssuanceResult.__dataclass_params__.frozen, True)

    def test_public_live_owner_operations_exist(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        for name in ("provision_custody_parent", "preflight", "issue_and_bind", "verify"):
            self.assertTrue(callable(getattr(issuer, name, None)), name)


class IssuedAuthorityFixtureRedTests(unittest.TestCase):
    def test_r3_shaped_inventory_is_normalized_before_profile_comparison(self) -> None:
        """R3 inode evidence is accepted, but R4 compares normalized records."""
        from tests import r4_issued_authority_fixture as fixture

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            repository = Path(temporary)
            fixture._write_source(repository)
            source_root = repository / Path(*fixture._RAW.parts)
            inventory = repository / Path(*fixture._INVENTORY.parts)
            document = json.loads(inventory.read_bytes())
            for record in document["records"]:
                for artifact in (record["manifest"], record["raw"], record["seal"]):
                    artifact["inode_fingerprint"] = {"device": 1, "inode": 1}
            inventory.write_bytes(fixture._canonical_json_bytes(document))

            def project(record: dict[str, object]) -> dict[str, object]:
                return {
                    "source": record["source"],
                    "canonical_query": dict(record["canonical_query"]),
                    "rawstore_verified": True,
                    **{
                        role: {
                            key: record[role][key]
                            for key in ("path", "sha256", "byte_size")
                        }
                        for role in ("manifest", "raw", "seal")
                    },
                }

            expected = tuple(project(record) for record in document["records"])

            profile = fixture._fixture_source_authority_profile(source_root, inventory)
            with open_trusted_root(repository, nonblocking=True) as root:
                closure = _read_source_closure_from_root(
                    root,
                    inventory_relative=fixture._INVENTORY,
                    expected_sha256=profile.inventory_sha256,
                    profile=profile,
                )

        self.assertEqual(tuple(profile.closure.records), expected)
        self.assertEqual(tuple(closure.records), expected)
        for records in (profile.closure.records, closure.records):
            for record in records:
                for role in ("manifest", "raw", "seal"):
                    self.assertNotIn("inode_fingerprint", record[role])

    def test_logical_record_mutations_are_rejected_after_inventory_authentication(self) -> None:
        from tests import r4_issued_authority_fixture as fixture

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            repository = Path(temporary)
            fixture._write_source(repository)
            source_root = repository / Path(*fixture._RAW.parts)
            inventory = repository / Path(*fixture._INVENTORY.parts)
            document = json.loads(inventory.read_bytes())
            for record in document["records"]:
                for artifact in (record["manifest"], record["raw"], record["seal"]):
                    artifact["inode_fingerprint"] = {"device": 1, "inode": 1}
            inventory.write_bytes(fixture._canonical_json_bytes(document))
            profile = fixture._fixture_source_authority_profile(source_root, inventory)
            original = json.loads(inventory.read_bytes())

            mutations = {
                "path": lambda artifact: artifact.update(
                    path="data/v5_1_bt/raw/baseball_savant_statcast/mutated.bin"
                ),
                "sha256": lambda artifact: artifact.update(sha256="0" * 64),
                "byte_size": lambda artifact: artifact.update(byte_size=artifact["byte_size"] + 1),
            }
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    mutated = json.loads(json.dumps(original))
                    mutate(mutated["records"][0]["manifest"])
                    payload = fixture._canonical_json_bytes(mutated)
                    inventory.write_bytes(payload)
                    with open_trusted_root(repository, nonblocking=True) as root:
                        with self.assertRaisesRegex(
                            CUSTODY_UNAVAILABLE, "approved source inventory records differ"
                        ):
                            _read_source_closure_from_root(
                                root,
                                inventory_relative=fixture._INVENTORY,
                                expected_sha256=hashlib.sha256(payload).hexdigest(),
                                profile=profile,
                            )

    def test_inventory_digest_mismatch_rejects_before_record_normalization(self) -> None:
        from tests import r4_issued_authority_fixture as fixture

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            repository = Path(temporary)
            fixture._write_source(repository)
            inventory = repository / Path(*fixture._INVENTORY.parts)
            expected_sha256 = hashlib.sha256(inventory.read_bytes()).hexdigest()
            inventory.write_bytes(b"not-json")
            with open_trusted_root(repository, nonblocking=True) as root:
                with self.assertRaisesRegex(
                    CUSTODY_UNAVAILABLE, "approved source inventory digest differs"
                ):
                    _read_source_closure_from_root(
                        root,
                        inventory_relative=fixture._INVENTORY,
                        expected_sha256=expected_sha256,
                    )

    def test_fixture_and_source_contracts_do_not_load_query_plan_or_scipy(self) -> None:
        """R4 issuance validation must remain import-light and SciPy-free."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import mlb_backtest.v52_source_contracts; "
                    "import tests.r4_issued_authority_fixture; "
                    "blocked=('mlb_backtest.v52_query_plan','mlb_backtest.diagnostics','scipy'); "
                    "raise SystemExit(any(name in sys.modules for name in blocked))"
                ),
            ],
            cwd=Path.cwd(),
            check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_profile_builder_binds_the_full_fixture_inventory(self) -> None:
        """A synthetic closure is usable only as an explicit validated profile."""
        from tests import r4_issued_authority_fixture as fixture

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            repository = Path(temporary)
            fixture._write_source(repository)
            inventory = repository / Path(*fixture._INVENTORY.parts)
            expected_sha256 = hashlib.sha256(inventory.read_bytes()).hexdigest()
            profile = fixture._fixture_source_authority_profile(
                repository / Path(*fixture._RAW.parts),
                inventory,
            )
        self.assertEqual(profile.source_approval["protected_inventory_sha256"], expected_sha256)
        self.assertEqual((profile.record_count, profile.physical_member_count, profile.logical_binding_count), (1675, 4711, 5025))

    def test_shared_fixture_has_no_external_authority_or_copy_dependency(self) -> None:
        import inspect
        import typing
        import tests.r4_issued_authority_fixture as fixture

        source = inspect.getsource(fixture)
        self.assertNotIn("MLB_V52_SOURCE_AUTHORITY_ROOT", source)
        self.assertNotIn("shutil.copy", source)
        self.assertEqual(
            typing.get_type_hints(fixture.IssuedR4Fixture)["sample_logical_path"],
            PurePosixPath,
        )

    def test_issuer_and_fixture_use_only_the_canonical_authority_resolver(self) -> None:
        import inspect
        from mlb_backtest import v52_r4_authority as authority
        from mlb_backtest import v52_r4_snapshot_issuer as issuer
        from tests import r4_issued_authority_fixture as fixture

        issuer_source = inspect.getsource(issuer)
        fixture_source = inspect.getsource(fixture)
        authority_source = inspect.getsource(authority)
        self.assertEqual(authority_source.count("def _resolve_source_authority"), 1)
        self.assertNotIn("def _resolve_source_authority", issuer_source)
        self.assertIn("_r4_authority._resolve_source_authority", issuer_source)
        self.assertNotIn("v52_r4_snapshot_issuer._resolve_source_authority", fixture_source)
        self.assertIn("v52_r4_authority._resolve_source_authority", fixture_source)

    def test_shared_fixture_issues_a_full_executable_authority(self) -> None:
        from tests.r4_issued_authority_fixture import issue_test_r4_authority

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            issued = issue_test_r4_authority(root)
            self.assertIsInstance(issued.sample_logical_path, PurePosixPath)
            self.assertEqual(
                (issued.custody_root / Path(*issued.sample_logical_path.parts[3:])).read_bytes(),
                issued.sample_payload,
            )
            self.assertFalse((issued.custody_root.parent / ".v5_1_bt_r4.issuance.lock").exists())
            for candidate in (issued.custody_root, *issued.custody_root.rglob("*")):
                try:
                    os.chflags(candidate, 0, follow_symlinks=False)
                    os.chmod(candidate, 0o700 if candidate.is_dir() else 0o600, follow_symlinks=False)
                except OSError:
                    pass


class SnapshotIssuerCliTests(unittest.TestCase):
    """The executable issuer boundary accepts only the closed R4 argv forms."""

    @classmethod
    def setUpClass(cls) -> None:
        from tests import r4_issued_authority_fixture as fixture

        cls._temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        cls.repository = Path(cls._temporary.name) / "repo"
        cls.repository.mkdir()
        cls.source_repository = Path(cls._temporary.name) / "external-source"
        cls.source_repository.mkdir()
        cls.records, cls.sample_logical_path, cls.sample_payload = fixture._write_source(
            cls.source_repository
        )
        cls.source_root = cls.source_repository / Path(*fixture._RAW.parts)
        cls.source_inventory = cls.source_repository / Path(*fixture._INVENTORY.parts)
        cls.custody_parent = Path(cls._temporary.name) / "MLB Custody"
        cls.run_spec = cls.repository / Path(*fixture._RUN_SPEC.parts)
        cls.run_spec.parent.mkdir(parents=True, exist_ok=True)
        from mlb_backtest.protocols import expected_r4_recovery_source_run
        from mlb_backtest.v52_source_contracts import canonical_json_bytes

        cls.run_spec.write_bytes(canonical_json_bytes(expected_r4_recovery_source_run()) + b"\n")
        fixture._write_r3_contract_template(cls.repository)
        cls.contract = cls.repository / Path(*fixture._CONTRACT.parts)
        cls.audit_root = cls.repository / Path(*fixture._AUDIT.parts)
        cls._fixture = fixture
        cls._issued = False

    @classmethod
    def tearDownClass(cls) -> None:
        # APFS immutable flags, when available, must not prevent fixture cleanup.
        for candidate in (cls.custody_parent / "v5_1_bt_r4",):
            if candidate.exists():
                for path in (candidate, *candidate.rglob("*")):
                    try:
                        os.chflags(path, 0, follow_symlinks=False)
                        os.chmod(
                            path,
                            0o700 if path.is_dir() else 0o600,
                            follow_symlinks=False,
                        )
                    except OSError:
                        pass
        cls._temporary.cleanup()

    @classmethod
    def _local_authority_provider_patch(cls):
        profile = cls._fixture._fixture_source_authority_profile(
            cls.source_root, cls.source_inventory,
        )

        def test_local_provider(*args: object, **kwargs: object):
            if not args and not kwargs:
                return profile
            if args == (cls.source_root, cls.source_inventory):
                return cls._fixture._IssuanceAuthority(
                    profile, cls._fixture._FIXTURE_ANCHOR_PRIVATE_KEY.sign
                )
            raise AssertionError("fixture source authority provider mode differs")

        return patch(
            "mlb_backtest.v52_r4_authority._resolve_source_authority",
            side_effect=test_local_provider,
        )

    @classmethod
    def _issue_argv(cls) -> list[str]:
        return [
            "issue-and-bind",
            "--source-root", str(cls.source_root),
            "--source-inventory", str(cls.source_inventory),
            "--source-approval-logical-root", "data/v5_1_bt/raw",
            "--source-approval-inventory", "audit/v5_1_bt/protected_raw_inventory.json",
            "--run-spec", str(cls.run_spec),
            "--contract", str(cls.contract),
            "--custody-parent", str(cls.custody_parent),
            "--final-name", "v5_1_bt_r4",
            "--audit-root", str(cls.audit_root),
        ]

    @classmethod
    def _ensure_issued(cls) -> None:
        if cls._issued:
            return
        from mlb_backtest.v52_r4_snapshot_issuer import main

        assert main(["provision-custody-parent", "--custody-parent", str(cls.custody_parent)]) == 0
        with cls._local_authority_provider_patch():
            assert main(cls._issue_argv()) == 0
        cls._issued = True

    def test_provision_custody_parent_argv(self) -> None:
        from mlb_backtest.v52_r4_snapshot_issuer import main

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            custody_parent = Path(temporary) / "MLB Custody"
            self.assertEqual(
                main(["provision-custody-parent", "--custody-parent", str(custody_parent)]),
                0,
            )
            self.assertTrue(custody_parent.is_dir())

    def test_preflight_argv_requires_provision_then_accepts_absent_authority(self) -> None:
        from mlb_backtest.v52_r4_snapshot_issuer import main

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            source_repository = Path(temporary) / "source"
            source_repository.mkdir()
            self._fixture._write_source(source_repository)
            source_root = source_repository / Path(*self._fixture._RAW.parts)
            source_inventory = source_repository / Path(*self._fixture._INVENTORY.parts)
            profile = self._fixture._fixture_source_authority_profile(
                source_root, source_inventory,
            )
            custody_parent = Path(temporary) / "MLB Custody"
            argv = [
                "preflight", "--source-root", str(source_root),
                "--source-inventory", str(source_inventory),
                "--custody-parent", str(custody_parent),
                "--final-name", "v5_1_bt_r4",
            ]
            self.assertFalse(custody_parent.exists())
            self.assertNotEqual(main(argv), 0)
            self.assertFalse(custody_parent.exists())
            self.assertEqual(
                main(["provision-custody-parent", "--custody-parent", str(custody_parent)]),
                0,
            )
            wrong_final = [*argv]
            wrong_final[-1] = "another-r4-root"
            self.assertNotEqual(main(wrong_final), 0)
            with patch(
                "mlb_backtest.v52_r4_authority._resolve_source_authority",
                side_effect=lambda *args, **kwargs: (
                    profile
                    if not args and not kwargs
                    else self._fixture._IssuanceAuthority(
                        profile, self._fixture._FIXTURE_ANCHOR_PRIVATE_KEY.sign
                    )
                ),
            ):
                self.assertEqual(main(argv), 0)
            self.assertIs(
                observe_issuance_state(source_repository, custody_parent),
                IssuanceState.ABSENT,
            )

    def test_issue_and_bind_argv(self) -> None:
        self._ensure_issued()
        self.assertTrue((self.custody_parent / "v5_1_bt_r4").is_dir())
        self.assertFalse((self.repository / "data/v5_1_bt/raw").exists())

    def test_issue_and_bind_cli_rejects_noncanonical_relative_approval_before_mutation(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer
        from mlb_backtest.protocols import expected_r4_recovery_source_run
        from tests import r4_issued_authority_fixture as fixture

        malformed = (
            ("dot prefix", "./audit/v5_1_bt/protected_raw_inventory.json"),
            ("duplicate slash", "audit//v5_1_bt/protected_raw_inventory.json"),
            ("parent traversal", "audit/v5_1_bt/../v5_1_bt/protected_raw_inventory.json"),
            ("backslash", "audit\\v5_1_bt\\protected_raw_inventory.json"),
        )
        for label, raw_approval_inventory in malformed:
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                repository, source_repository = root / "repository", root / "source"
                repository.mkdir()
                source_repository.mkdir()
                fixture._write_source(source_repository)
                fixture._write_r3_contract_template(repository)
                run_spec = repository / Path(*fixture._RUN_SPEC.parts)
                run_spec.parent.mkdir(parents=True, exist_ok=True)
                run_spec.write_bytes(
                    fixture._canonical_json_bytes(expected_r4_recovery_source_run()) + b"\n"
                )
                source_root = source_repository / Path(*fixture._RAW.parts)
                source_inventory = source_repository / Path(*fixture._INVENTORY.parts)
                custody_parent = root / "MLB Custody"
                audit_root = repository / Path(*fixture._AUDIT.parts)
                argv = [
                    "issue-and-bind",
                    "--source-root", str(source_root),
                    "--source-inventory", str(source_inventory),
                    "--source-approval-logical-root", "data/v5_1_bt/raw",
                    "--source-approval-inventory", raw_approval_inventory,
                    "--run-spec", str(run_spec),
                    "--contract", str(repository / Path(*fixture._CONTRACT.parts)),
                    "--custody-parent", str(custody_parent),
                    "--final-name", "v5_1_bt_r4",
                    "--audit-root", str(audit_root),
                ]
                with patch.object(
                    issuer,
                    "issue_and_bind",
                    side_effect=AssertionError("issuer mutation must not start"),
                ):
                    self.assertEqual(issuer.main(argv), 2)
                self.assertFalse(custody_parent.exists())
                self.assertFalse((repository / Path(*fixture._AUDIT.parts)).exists())
                self.assertFalse(
                    (custody_parent / ".v5_1_bt_r4.issuance.lock").exists()
                )

    def test_verify_argv_is_read_only_when_an_incomplete_lock_exists(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        self._ensure_issued()
        layout = issuer._r4_authority_layout(self.repository, self.custody_parent)
        lock = self.custody_parent / ".v5_1_bt_r4.issuance.lock"
        argv = [
            "verify", "--custody-root", str(self.custody_parent / "v5_1_bt_r4"),
            "--inventory", str(self.repository / Path(*layout.inventory_relative.parts)),
            "--receipt", str(self.repository / Path(*layout.receipt_relative.parts)),
            "--run-spec", str(self.run_spec),
            "--contract", str(self.contract),
        ]
        with self._local_authority_provider_patch():
            self.assertEqual(issuer.main(argv), 0)
            self.assertIs(
                observe_issuance_state(self.repository, self.custody_parent),
                IssuanceState.EXECUTABLE,
            )
        lock.write_bytes(b"not-an-owner-lock")
        with patch.object(issuer.os, "unlink", side_effect=AssertionError("verify unlinked")), patch.object(
            issuer.os, "chmod", side_effect=AssertionError("verify chmodded")
        ), patch.object(
            issuer.os, "chflags", side_effect=AssertionError("verify changed flags"), create=True
        ), patch.object(issuer, "_unlink_tree_at", side_effect=AssertionError("verify repaired")):
            self.assertNotEqual(issuer.main(argv), 0)
        self.assertTrue(lock.exists())
        self.assertIs(
            observe_issuance_state(self.repository, self.custody_parent),
            IssuanceState.OBSERVER_INCOMPLETE_LOCKED,
        )

    def test_parser_rejects_alternate_missing_and_unexpected_arguments(self) -> None:
        from mlb_backtest.v52_r4_snapshot_issuer import _parser

        parser = _parser()
        subparsers = parser._subparsers._group_actions[0].choices
        self.assertEqual(
            set(subparsers),
            {"provision-custody-parent", "preflight", "issue-and-bind", "verify"},
        )
        expected_options = {
            "provision-custody-parent": {"custody_parent"},
            "preflight": {"source_root", "source_inventory", "custody_parent", "final_name"},
            "issue-and-bind": {
                "source_root", "source_inventory", "source_approval_logical_root",
                "source_approval_inventory", "run_spec", "contract", "custody_parent",
                "final_name", "audit_root",
            },
            "verify": {"custody_root", "inventory", "receipt", "run_spec", "contract"},
        }
        for command, options in expected_options.items():
            self.assertEqual(
                {action.dest for action in subparsers[command]._actions if action.dest != "help"},
                options,
            )
        invalid = (
            ["provision-custody-parent"],
            ["preflight", "--source-root", str(self.source_root)],
            ["verify", "--custody-root", str(self.custody_parent), "--extra", "no"],
            ["issue-and-bind", "--source-root", str(self.source_root), "--custody", str(self.custody_parent)],
        )
        for argv in invalid:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(argv)


def _shared_raw_paths() -> tuple[tuple[str, int], tuple[str, int]]:
    return (
        (
            "data/v5_1_bt/raw/mlb_stats_pitcher_bf/"
            "729bad325d9eafb22883c3318896f2baf877c99b5600fa77aba2bb9f6da6a6e7.raw",
            85,
        ),
        (
            "data/v5_1_bt/raw/mlb_stats_pitcher_bf/"
            "f8615992094a949312f8a27b4e129e9c14abd006b994818642110733e260d86f.raw",
            231,
        ),
    )


class SnapshotIssuerLockTests(unittest.TestCase):
    def test_owner_binds_exact_repository_authority_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            custody, repository_a, repository_b = (
                root / "custody", root / "repository-a", root / "repository-b"
            )
            custody.mkdir()
            repository_a.mkdir()
            repository_b.mkdir()
            with acquire_issuance_lock(custody, "v5_1_bt_r4") as owner:
                owner.configure_r4_authority(repository_a, custody)
                owner.require_r4_repository_authority(repository_a)
                with self.assertRaises(CUSTODY_UNAVAILABLE):
                    owner.configure_r4_authority(repository_b, custody)
                with self.assertRaises(CUSTODY_UNAVAILABLE):
                    owner.require_r4_repository_authority(repository_b)

    @unittest.skipUnless(hasattr(os, "fork"), "fork requires POSIX")
    def test_fork_child_cannot_start_fresh_issuance(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            custody = Path(temporary) / "custody"
            custody.mkdir()
            child = os.fork()
            if child == 0:
                try:
                    try:
                        acquire_issuance_lock(custody, "snapshot", nonce=b"\x37" * 16)
                    except CUSTODY_UNAVAILABLE:
                        os._exit(0)
                    os._exit(1)
                except BaseException:
                    os._exit(2)
            _pid, status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0)
            self.assertEqual(list(custody.iterdir()), [])

    def test_enumeration_preflight_failure_leaves_no_lock(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            custody = Path(temporary) / "custody"
            custody.mkdir()
            from mlb_backtest import v52_r4_snapshot_issuer as issuer

            with patch.object(issuer, "_snapshot_open_fd_numbers", side_effect=CUSTODY_UNAVAILABLE("no fd snapshot")):
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "no fd snapshot"):
                    acquire_issuance_lock(custody, "snapshot", nonce=b"\x38" * 16)
            self.assertEqual(list(custody.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "fork"), "fork requires POSIX")
    def test_child_exits_fail_closed_when_alias_enumeration_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            custody = Path(temporary) / "custody"
            custody.mkdir()
            owner = acquire_issuance_lock(custody, "snapshot", nonce=b"\x39" * 16)
            from mlb_backtest import v52_r4_snapshot_issuer as issuer

            with patch.object(issuer, "_snapshot_open_fd_numbers", side_effect=CUSTODY_UNAVAILABLE("injected")):
                child = os.fork()
            if child == 0:
                os._exit(1)
            _pid, status = os.waitpid(child, 0)
            self.assertTrue(
                os.WIFEXITED(status)
                and os.WEXITSTATUS(status) == issuer._FORK_ENUMERATION_FAILURE_STATUS
            )
            self.assertTrue(owner.is_live_owner())
            self.assertTrue((custody / owner.lock_name).exists())
            owner.abort_before_rename()

    @unittest.skipUnless(
        hasattr(os, "fork") and hasattr(os, "register_at_fork"),
        "fork invalidation requires POSIX at-fork support",
    )
    def test_fork_child_invalidates_inherited_owner_without_releasing_parent_lock(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            custody = Path(temporary) / "custody"
            custody.mkdir()
            owner = acquire_issuance_lock(custody, "snapshot", nonce=b"\x35" * 16)
            duplicate_lock_fd = os.dup(owner._lock_fd)
            duplicate_parent_fd = os.dup(owner.parent_fd)
            read_fd, write_fd = os.pipe()
            before = len(os.listdir("/dev/fd")) if Path("/dev/fd").is_dir() else None
            child = os.fork()
            if child == 0:
                try:
                    os.close(read_fd)
                    invalid = not owner.is_live_owner()
                    try:
                        os.fstat(duplicate_lock_fd)
                    except OSError:
                        pass
                    else:
                        invalid = False
                    try:
                        os.fstat(duplicate_parent_fd)
                    except OSError:
                        pass
                    else:
                        invalid = False
                    try:
                        owner.abort_before_rename()
                    except CUSTODY_UNAVAILABLE:
                        invalid = invalid and owner.parent_fd == -1 and owner._lock_fd == -1
                    else:
                        invalid = False
                    os.write(write_fd, b"1" if invalid else b"0")
                finally:
                    os.close(write_fd)
                os._exit(0)
            os.close(write_fd)
            self.assertEqual(os.read(read_fd, 1), b"1")
            os.close(read_fd)
            _pid, status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0)
            self.assertTrue(owner.is_live_owner())
            self.assertTrue((custody / owner.lock_name).exists())
            os.close(duplicate_lock_fd)
            os.close(duplicate_parent_fd)
            owner.abort_before_rename()
            if before is not None:
                self.assertEqual(len(os.listdir("/dev/fd")), before - 6)

    def test_owner_registry_is_removed_when_lock_closes(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            custody = Path(temporary) / "custody"
            custody.mkdir()
            owner = acquire_issuance_lock(custody, "snapshot", nonce=b"\x36" * 16)
            self.assertIn(owner, issuer._ACTIVE_ISSUANCE_OWNERS)
            owner.abort_before_rename()
            self.assertNotIn(owner, issuer._ACTIVE_ISSUANCE_OWNERS)

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "descriptor count requires /dev/fd")
    def test_context_freeze_failure_preserves_original_error_lock_final_and_fds(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            custody = root / "custody"
            custody.mkdir()
            source = root / "source"
            source.mkdir()
            (source / "payload").write_bytes(b"payload")
            before = len(os.listdir("/dev/fd"))
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "injected freeze failure"):
                with acquire_issuance_lock(custody, "snapshot", nonce=b"\x31" * 16) as owner:
                    _populate_snapshot_for_test(owner, source)
                    from mlb_backtest import v52_r4_snapshot_issuer as issuer

                    with patch.object(
                        issuer,
                        "_freeze_owned_snapshot",
                        side_effect=CUSTODY_UNAVAILABLE("injected freeze failure"),
                    ):
                        publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
            self.assertTrue((custody / "snapshot").is_dir())
            self.assertTrue((custody / ".snapshot.issuance.lock").is_file())
            self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_owner_factory_nonce_pid_and_kernel_lock_are_required(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            custody = Path(temporary) / "custody"
            custody.mkdir()
            owner = acquire_issuance_lock(custody, "snapshot", nonce=b"\x32" * 16)
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "lock factory"):
                IssuanceOwner(
                    owner.parent_fd,
                    owner._lock_fd,
                    owner.lock_name,
                    owner.final_name,
                    b"\x32" * 16,
                    os.fstat(owner._lock_fd),
                    os.fstat(owner.parent_fd),
                    factory_token=object(),
                )
            (custody / owner.lock_name).write_bytes(b"tampered nonce!!!")
            self.assertFalse(owner.is_live_owner())
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "no longer live"):
                owner.abort_before_rename()
            self.assertTrue((custody / owner.lock_name).exists())
            owner._close_descriptors()

    def test_owner_rejects_creator_pid_change_without_removing_lock(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            custody = Path(temporary) / "custody"
            custody.mkdir()
            owner = acquire_issuance_lock(custody, "snapshot", nonce=b"\x33" * 16)
            with patch("mlb_backtest.v52_r4_snapshot_issuer.os.getpid", return_value=owner._creator_pid + 1):
                self.assertFalse(owner.is_live_owner())
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "no longer live"):
                    owner.abort_before_rename()
            self.assertTrue((custody / owner.lock_name).exists())
            owner._close_descriptors()

    def test_separately_opened_lock_fd_cannot_fabricate_owner_or_remove_lock(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            custody = Path(temporary) / "custody"
            custody.mkdir()
            owner = acquire_issuance_lock(custody, "snapshot", nonce=b"\x34" * 16)
            separate_fd = os.open(
                custody / owner.lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            try:
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "lock factory"):
                    IssuanceOwner(
                        owner.parent_fd,
                        separate_fd,
                        owner.lock_name,
                        owner.final_name,
                        b"\x34" * 16,
                        os.fstat(separate_fd),
                        os.fstat(owner.parent_fd),
                        factory_token=object(),
                    )
                self.assertTrue((custody / owner.lock_name).exists())
            finally:
                os.close(separate_fd)
                owner.abort_before_rename()

    def test_live_owner_requires_original_lock_identity_and_matching_nonce(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            parent = Path(temporary) / "custody"
            parent.mkdir()
            repository = Path(temporary) / "repository"
            repository.mkdir()
            owner = acquire_issuance_lock(parent, "v5_1_bt_r4", nonce=b"\x01" * 16)
            self.assertEqual(
                owner.temporary_name,
                ".v5_1_bt_r4.issuance." + "01" * 16 + ".tmp",
            )
            self.assertIs(
                observe_issuance_state(repository, parent),
                IssuanceState.OBSERVER_INCOMPLETE_LOCKED,
            )
            self.assertTrue(owner.is_live_owner())
            owner.abort_before_rename()

    def test_nonowner_cannot_acquire_or_remove_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            parent = Path(temporary) / "custody"
            parent.mkdir()
            owner = acquire_issuance_lock(parent, "v5_1_bt_r4", nonce=b"\x02" * 16)
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "issuance lock exists"):
                acquire_issuance_lock(parent, "v5_1_bt_r4", nonce=b"\x03" * 16)
            self.assertTrue((parent / ".v5_1_bt_r4.issuance.lock").exists())
            owner.abort_before_rename()
            self.assertFalse((parent / ".v5_1_bt_r4.issuance.lock").exists())

    def test_nonce_must_be_exactly_sixteen_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            parent = Path(temporary) / "custody"
            parent.mkdir()
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "16 bytes"):
                acquire_issuance_lock(parent, "snapshot", nonce=b"short")

    def test_lock_acquisition_rejects_an_existing_final_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            parent = Path(temporary) / "custody"
            parent.mkdir()
            (parent / "snapshot").mkdir()
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "final root exists"):
                acquire_issuance_lock(parent, "snapshot", nonce=b"\x06" * 16)
            self.assertFalse((parent / ".snapshot.issuance.lock").exists())

    def test_lock_acquisition_rejects_any_matching_temp_and_leaves_no_new_lock(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            parent = Path(temporary) / "custody"
            parent.mkdir()
            (parent / (".snapshot.issuance." + "aa" * 16 + ".tmp")).mkdir()
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "temporary root exists"):
                acquire_issuance_lock(parent, "snapshot", nonce=b"\x07" * 16)
            self.assertFalse((parent / ".snapshot.issuance.lock").exists())

    def test_owner_state_precedence(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            parent = Path(temporary) / "custody"
            parent.mkdir()
            owner = acquire_issuance_lock(parent, "snapshot", nonce=b"\x04" * 16)
            self.assertIs(owner.state(), IssuanceState.OWNER_LOCK_ACQUIRED)
            _populate_snapshot_for_test(owner, self._source(Path(temporary)))
            self.assertIs(owner.state(), IssuanceState.OWNER_PREPARING)
            publish_owned_snapshot(owner, owner.temporary_name, "snapshot")
            self.assertIs(owner.state(), IssuanceState.OWNER_SNAPSHOT_PUBLISHED)

    def test_private_evidence_capabilities_classify_every_owner_state(self) -> None:
        class FakeAuthenticator:
            def authenticate_snapshot(self, layout, parent_fd):
                del parent_fd
                return _AuthenticatedSnapshot(layout) if (layout.custody_parent / layout.final_name).is_dir() else None

            def authenticate_anchor(self, layout, snapshot):
                return _AuthenticatedAnchor(snapshot, {}) if (layout.repository_root / layout.anchor_relative).is_file() else None

            def authenticate_inventory(self, layout, snapshot, anchor):
                del snapshot
                return _AuthenticatedInventory(anchor) if (layout.repository_root / layout.inventory_relative).is_file() else None

            def authenticate_receipt(self, layout, snapshot, anchor, inventory):
                del snapshot, anchor
                return _AuthenticatedReceipt(inventory) if (layout.repository_root / layout.receipt_relative).is_file() else None

            def authenticate_contract(self, layout, snapshot, anchor, inventory, receipt):
                del snapshot, anchor, inventory
                return _AuthenticatedContract(receipt) if (layout.repository_root / layout.contract_relative).is_file() else None

            def authenticate_checkpoint_chain(self, layout, contract):
                del layout, contract
                return None

            def authenticate_terminal(self, layout, contract):
                del layout, contract
                return None

        cases = (
            ("lock", False, False, (), IssuanceState.OWNER_LOCK_ACQUIRED),
            ("preparing", True, False, (), IssuanceState.OWNER_PREPARING),
            ("snapshot", False, True, (), IssuanceState.OWNER_SNAPSHOT_PUBLISHED),
            ("inventory", False, True, ("anchor_relative", "inventory_relative"), IssuanceState.OWNER_INVENTORY_PUBLISHED),
            ("receipt", False, True, ("anchor_relative", "inventory_relative", "receipt_relative"), IssuanceState.OWNER_RECEIPT_PUBLISHED),
            ("contract", False, True, ("anchor_relative", "inventory_relative", "receipt_relative", "contract_relative"), IssuanceState.OWNER_CONTRACT_AUTHENTICATED),
        )
        for _label, temporary, final, artifacts, expected in cases:
            with self.subTest(state=expected):
                with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary_directory:
                    root = Path(temporary_directory)
                    repository, custody = root / "repository", root / "custody"
                    repository.mkdir()
                    custody.mkdir()
                    owner = acquire_issuance_lock(custody, "v5_1_bt_r4", nonce=b"\x20" * 16)
                    layout = _r4_authority_layout(repository, custody)
                    if temporary:
                        (custody / owner.temporary_name).mkdir()
                    if final:
                        (custody / owner.final_name).mkdir()
                    for attribute in artifacts:
                        path = repository / getattr(layout, attribute)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"bound")
                    _bind_r4_authority_for_test(owner, layout, FakeAuthenticator())
                    self.assertIs(owner.state(), expected)
                    owner._close_descriptors()

    def test_release_reauthenticates_bound_receipt_and_keeps_lock_on_swap(self) -> None:
        class FreshAuthenticator:
            expected = b"bound"

            def authenticate_snapshot(self, layout, parent_fd):
                del parent_fd
                return _AuthenticatedSnapshot(layout) if (layout.custody_parent / layout.final_name).is_dir() else None

            def authenticate_anchor(self, layout, snapshot):
                return _AuthenticatedAnchor(snapshot, {}) if (layout.repository_root / layout.anchor_relative).read_bytes() == self.expected else None

            def authenticate_inventory(self, layout, snapshot, anchor):
                del snapshot
                return _AuthenticatedInventory(anchor) if (layout.repository_root / layout.inventory_relative).read_bytes() == self.expected else None

            def authenticate_receipt(self, layout, snapshot, anchor, inventory):
                del snapshot, anchor
                return _AuthenticatedReceipt(inventory) if (layout.repository_root / layout.receipt_relative).read_bytes() == self.expected else None

            def authenticate_contract(self, layout, snapshot, anchor, inventory, receipt):
                del snapshot, anchor, inventory
                return _AuthenticatedContract(receipt) if (layout.repository_root / layout.contract_relative).read_bytes() == self.expected else None

            def authenticate_checkpoint_chain(self, layout, contract):
                del layout, contract
                return None

            def authenticate_terminal(self, layout, contract):
                del layout, contract
                return None

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            repository, custody = root / "repository", root / "custody"
            repository.mkdir()
            custody.mkdir()
            owner = acquire_issuance_lock(custody, "v5_1_bt_r4", nonce=b"\x18" * 16)
            source = root / "source"
            source.mkdir()
            (source / "payload").write_bytes(b"payload")
            _populate_snapshot_for_test(owner, source)
            publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
            layout = _r4_authority_layout(repository, custody)
            for relative in (layout.anchor_relative, layout.inventory_relative, layout.receipt_relative, layout.contract_relative):
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"bound")
            _bind_r4_authority_for_test(owner, layout, FreshAuthenticator())
            self.assertIs(owner.state(), IssuanceState.OWNER_CONTRACT_AUTHENTICATED)
            (repository / layout.receipt_relative).write_bytes(b"substitute")
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "contract is not authenticated"):
                owner.release_after_contract_authentication()
            self.assertTrue((custody / ".v5_1_bt_r4.issuance.lock").exists())
            owner._close_descriptors()  # post-rename cleanup is deliberately forbidden.

    @staticmethod
    def _source(root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        (source / "payload").write_bytes(b"payload")
        return source


class SnapshotIssuerPublicationTests(unittest.TestCase):
    def _owner(self, root: Path):
        parent = root / "custody"
        parent.mkdir()
        return acquire_issuance_lock(parent, "snapshot", nonce=b"\x05" * 16)

    def test_copy_streams_safe_regular_files_and_applies_readonly_modes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "nested").mkdir(parents=True)
            payload = b"safe bytes" * 1000
            (source / "nested" / "payload").write_bytes(payload)
            owner = self._owner(root)
            _populate_snapshot_for_test(owner, source)
            copied = root / "custody" / owner.temporary_name / "nested" / "payload"
            self.assertEqual(copied.read_bytes(), payload)
            self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), hashlib.sha256(payload).hexdigest())
            self.assertEqual(copied.stat().st_mode & 0o777, 0o444)
            self.assertEqual(copied.parent.stat().st_mode & 0o777, 0o555)
            owner.abort_before_rename()

    def test_public_copy_rejects_unbound_source_authority_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            source_authority = root / "authority"
            source_authority.mkdir()
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "approved source inventory"):
                populate_snapshot(owner, source_authority)
            owner.abort_before_rename()

    def test_held_authority_root_rejects_lexical_swap_after_inventory_read(self) -> None:
        """A swapped lexical root must never direct the raw open to replacement B."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            custody = root / "custody"
            custody.mkdir()
            owner = acquire_issuance_lock(custody, "snapshot", nonce=b"\x15" * 16)
            authority = root / "authority"
            authority.mkdir()
            (authority / "audit").mkdir()
            (authority / "data" / "raw").mkdir(parents=True)
            inventory = authority / "audit" / "small.json"
            inventory.write_bytes(b"small inventory")
            (authority / "data" / "raw" / "payload").write_bytes(b"A")
            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "audit").mkdir()
            (replacement / "data" / "raw").mkdir(parents=True)
            (replacement / "audit" / "small.json").write_bytes(b"replacement")
            (replacement / "data" / "raw" / "payload").write_bytes(b"B")
            binding = _SourceAuthorityBinding(
                PurePosixPath("audit/small.json"),
                hashlib.sha256(b"small inventory").hexdigest(),
                PurePosixPath("data/raw"),
            )
            from mlb_backtest import v52_r4_snapshot_issuer as issuer

            def read_then_swap(*_args, **_kwargs):
                authority.rename(root / "authority-a")
                replacement.rename(authority)
                return SnapshotSourceClosure(())

            with patch.object(issuer, "_read_source_closure_from_root", side_effect=read_then_swap):
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "authority root changed"):
                    _populate_snapshot_from_authority(owner, authority, binding)
            copied = custody / owner.temporary_name / "payload"
            self.assertFalse(copied.exists())
            owner.abort_before_rename()

    def test_copy_rejects_symlink_and_multilink_sources(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "symlink").symlink_to("missing")
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "symlink"):
                _populate_snapshot_for_test(self._owner(root), source)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "source-file").write_bytes(b"x")
            os.link(source / "source-file", source / "second-name")
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "link count"):
                _populate_snapshot_for_test(self._owner(root), source)

    def test_source_root_is_opened_nonblocking_before_descriptor_traversal(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "payload").write_bytes(b"payload")
            owner = self._owner(root)
            from mlb_backtest import v52_r4_snapshot_issuer as issuer

            with patch.object(issuer, "_open_directory_no_follow", wraps=issuer._open_directory_no_follow) as opener:
                _populate_snapshot_for_test(owner, source)
            self.assertEqual(opener.call_args_list[-1].kwargs, {"nonblocking": True})
            owner.abort_before_rename()

    def test_source_inventory_parent_chain_and_leaf_open_nonblocking(self) -> None:
        """Every source-inventory traversal open keeps FIFO-safe O_NONBLOCK."""
        from mlb_backtest import secure_fs

        opened: list[tuple[str, int]] = []
        original_open = secure_fs.os.open

        def capture(name, flags, *args, **kwargs):
            if isinstance(name, str) and name in {"audit", "v5_1_bt", "protected_raw_inventory.json"}:
                opened.append((name, flags))
            return original_open(name, flags, *args, **kwargs)

        with patch.object(secure_fs.os, "open", side_effect=capture):
            _read_source_closure(
                Path.cwd(),
                inventory_relative=Path("audit/v5_1_bt/protected_raw_inventory.json"),
                expected_sha256="2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6",
            )
        self.assertEqual([name for name, _flags in opened[-3:]], ["audit", "v5_1_bt", "protected_raw_inventory.json"])
        self.assertTrue(all(flags & os.O_NONBLOCK for _name, flags in opened[-3:]))

    def test_publish_is_exclusive_and_conflict_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            temporary_root = root / "custody" / owner.temporary_name
            temporary_root.mkdir()
            (temporary_root / "payload").write_bytes(b"new")
            (root / "custody" / "snapshot").mkdir()
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "final root exists"):
                publish_owned_snapshot(owner, owner.temporary_name, "snapshot")
            self.assertEqual((root / "custody" / "snapshot").stat().st_mode & 0o777, 0o755)

    def test_publish_rejects_snapshot_without_bound_copy_verification(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            temporary_root = root / "custody" / owner.temporary_name
            temporary_root.mkdir()
            (temporary_root / "payload").write_bytes(b"new")
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "bound source closure"):
                publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
            self.assertTrue((root / "custody" / owner.final_name).exists())
            self.assertTrue((root / "custody" / ".snapshot.issuance.lock").exists())

    def test_post_rename_abort_preserves_final_and_lock(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            source = root / "source"
            source.mkdir()
            (source / "payload").write_bytes(b"new")
            _populate_snapshot_for_test(owner, source)
            publish_owned_snapshot(owner, owner.temporary_name, "snapshot")
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "post-rename"):
                owner.abort_before_rename()
            self.assertTrue((root / "custody" / "snapshot" / "payload").exists())
            self.assertTrue((root / "custody" / ".snapshot.issuance.lock").exists())

    def test_canonical_json_publication_writes_newline_and_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                digest = publish_canonical_json(fd, ".draft", "final.json", {"b": 2, "a": 1})
            finally:
                os.close(fd)
            payload = (root / "final.json").read_bytes()
            self.assertEqual(payload, b'{"a":1,"b":2}\n')
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())

    def test_publish_uses_descriptor_exclusive_rename_not_replace(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            with patch("mlb_backtest.v52_r4_snapshot_issuer.rename_exclusive_at") as rename, patch.object(os, "replace") as replace, patch("mlb_backtest.v52_r4_snapshot_issuer._freeze_owned_snapshot") as freeze:
                publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
            rename.assert_called_once_with(owner.temporary_name, owner.final_name, src_dir_fd=owner.parent_fd, dst_dir_fd=owner.parent_fd)
            replace.assert_not_called()
            freeze.assert_called_once_with(owner)

    def test_owner_cannot_publish_an_arbitrary_sibling(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "names differ"):
                publish_owned_snapshot(owner, ".other.tmp", owner.final_name)
            owner.abort_before_rename()

    def test_temporary_name_is_derived_and_cannot_be_mutated(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            owner = self._owner(Path(temporary))
            with self.assertRaises(AttributeError):
                owner.temporary_name = ".attacker.tmp"
            self.assertEqual(owner.temporary_name, ".snapshot.issuance." + "05" * 16 + ".tmp")
            owner.abort_before_rename()

    @unittest.skipUnless(sys.platform == "darwin", "Darwin custody protocol")
    def test_publish_freezes_bound_snapshot_before_acceptance(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            source = root / "source"
            source.mkdir()
            (source / "payload").write_bytes(b"payload")
            _populate_snapshot_for_test(owner, source)
            publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
            self.assertTrue(owner._snapshot_published)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin custody protocol")
    def test_publish_sets_final_root_immutable_last(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            source = root / "source"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "payload").write_bytes(b"payload")
            _populate_snapshot_for_test(owner, source)
            from mlb_backtest import v52_r4_snapshot_issuer as issuer

            frozen_inodes: list[int] = []

            def capture(descriptor):
                frozen_inodes.append(os.fstat(descriptor).st_ino)

            with patch.object(issuer, "_set_immutable_flag", side_effect=capture), patch.object(issuer, "_verify_immutable_flag"):
                publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
            self.assertEqual(
                frozen_inodes[-1], (root / "custody" / owner.final_name).stat().st_ino
            )

    @unittest.skipUnless(sys.platform == "darwin", "Darwin custody protocol")
    def test_full_fsync_is_called_after_each_immutable_metadata_update(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            source = root / "source"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "payload").write_bytes(b"payload")
            _populate_snapshot_for_test(owner, source)
            from mlb_backtest import v52_r4_snapshot_issuer as issuer

            events: list[str] = []
            original_set = issuer._set_immutable_flag

            def set_flag(descriptor):
                events.append("flag")
                return original_set(descriptor)

            def full_sync(descriptor, label):
                del descriptor, label
                events.append("full")

            def volume_sync(descriptor, label):
                del descriptor
                self.assertEqual(label, "custody parent")
                events.append("volume")

            with patch.object(issuer, "_set_immutable_flag", side_effect=set_flag), patch.object(
                issuer, "_full_fsync", side_effect=full_sync
            ), patch.object(
                issuer, "_volume_full_sync", side_effect=volume_sync
            ):
                publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
            self.assertGreaterEqual(events.count("full"), events.count("flag"))
            self.assertTrue(
                all(events[index + 1] == "full" for index, event in enumerate(events[:-1]) if event == "flag")
            )
            self.assertEqual(events[-1], "volume")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin custody protocol")
    def test_full_fsync_failure_fails_closed(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        with patch.object(issuer.fcntl, "fcntl", side_effect=OSError("injected")):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "full fsync failed"):
                issuer._full_fsync(42, "fixture")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin custody protocol")
    def test_volume_full_sync_uses_libsystem_waiting_fullsync(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        class Function:
            argtypes = None
            restype = None

            def __init__(self) -> None:
                self.calls: list[tuple[int, int]] = []

            def __call__(self, descriptor: int, flags: int) -> int:
                self.calls.append((descriptor, flags))
                return 0

        function = Function()
        library = type("Library", (), {"fsync_volume_np": function})()
        with patch.object(issuer.ctypes, "CDLL", return_value=library) as load:
            issuer._volume_full_sync(42, "fixture")
        load.assert_called_once_with("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.assertEqual(function.calls, [(42, 0x01 | 0x02)])
        self.assertEqual(function.argtypes, (issuer.ctypes.c_int, issuer.ctypes.c_int))
        self.assertIs(function.restype, issuer.ctypes.c_int)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin custody protocol")
    def test_volume_full_sync_failure_fails_closed(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        function = type("Function", (), {"argtypes": None, "restype": None, "__call__": lambda self, *_args: 1})()
        library = type("Library", (), {"fsync_volume_np": function})()
        with patch.object(issuer.ctypes, "CDLL", return_value=library), patch.object(
            issuer.ctypes, "get_errno", return_value=5
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "volume full sync failed"):
                issuer._volume_full_sync(42, "fixture")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin custody protocol")
    def test_volume_full_sync_real_private_tmp_probe(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        descriptor = os.open("/private/tmp", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            issuer._volume_full_sync(descriptor, "private tmp probe")
        finally:
            os.close(descriptor)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin custody protocol")
    def test_volume_barrier_precedes_delayed_ctime_snapshot_captures(self) -> None:
        """Three equal captures may start only after the volume barrier commits ctime."""
        from mlb_backtest import v52_r4_custody_artifacts as artifacts
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            source = root / "source"
            source.mkdir()
            (source / "payload").write_bytes(b"payload")
            _populate_snapshot_for_test(owner, source)
            events: list[str] = []
            delayed_ctime = {"value": 1}

            def volume_sync(descriptor: int, label: str) -> None:
                del descriptor
                self.assertEqual(label, "custody parent")
                delayed_ctime["value"] = 2
                events.append("volume")

            def capture(*_args: object, **_kwargs: object) -> SimpleNamespace:
                events.append(f"capture:{delayed_ctime['value']}")
                return SimpleNamespace(
                    snapshot={"ctime": delayed_ctime["value"]},
                    members=(), root_locks=(), ancestor_identities=(),
                )

            try:
                with patch.object(issuer, "_full_fsync"), patch.object(
                    issuer, "_volume_full_sync", side_effect=volume_sync
                ):
                    publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
                with patch.object(
                    artifacts, "_capture_frozen_snapshot_once", side_effect=capture
                ) as captures, patch.object(artifacts.time, "sleep"):
                    result = artifacts.capture_frozen_snapshot(
                        owner, repository_root=root, custody_parent=root / "custody"
                    )
            finally:
                owner._close_descriptors()
            self.assertEqual(result.snapshot, {"ctime": 2})
            self.assertEqual(captures.call_count, 3)
            self.assertEqual(events, ["volume", "capture:2", "capture:2", "capture:2"])

    def test_publish_freeze_failure_preserves_lock_and_incomplete_final(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            owner = self._owner(root)
            source = root / "source"
            source.mkdir()
            (source / "payload").write_bytes(b"payload")
            _populate_snapshot_for_test(owner, source)
            from mlb_backtest import v52_r4_snapshot_issuer as issuer

            with patch.object(
                issuer,
                "_freeze_owned_snapshot",
                side_effect=CUSTODY_UNAVAILABLE("injected immutable failure"),
            ):
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "injected immutable failure"):
                    publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
            self.assertFalse(owner._snapshot_published)
            self.assertTrue((root / "custody" / owner.final_name).is_dir())
            self.assertTrue((root / "custody" / ".snapshot.issuance.lock").exists())


class SnapshotIssuerObserverTests(unittest.TestCase):
    def test_public_state_interfaces_accept_only_fixed_layout_anchors(self) -> None:
        observer_parameters = inspect.signature(observe_issuance_state).parameters
        self.assertEqual(tuple(observer_parameters), ("repository_root", "custody_parent"))
        self.assertNotIn("artifacts", inspect.signature(acquire_issuance_lock).parameters)

    def test_observer_state_precedence(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            repository = Path(temporary) / "repository"
            parent = Path(temporary) / "custody"
            repository.mkdir()
            parent.mkdir()
            self.assertIs(observe_issuance_state(repository, parent), IssuanceState.ABSENT)
            (parent / "v5_1_bt_r4").mkdir()
            self.assertIs(observe_issuance_state(repository, parent), IssuanceState.OBSERVER_INCOMPLETE_UNLOCKED)
            (parent / ".v5_1_bt_r4.issuance.lock").write_bytes(b"")
            self.assertIs(
                observe_issuance_state(repository, parent),
                IssuanceState.OBSERVER_INCOMPLETE_LOCKED,
            )

    def test_private_evidence_capabilities_classify_all_advanced_observer_states(self) -> None:
        class FakeAuthenticator:
            def __init__(self, stage: str) -> None:
                self.stage = stage

            def authenticate_snapshot(self, layout, parent_fd):
                del parent_fd
                return _AuthenticatedSnapshot(layout) if self.stage != "none" else None

            def authenticate_anchor(self, layout, snapshot):
                del layout
                return _AuthenticatedAnchor(snapshot, {}) if self.stage in {"inventory", "receipt", "contract", "checkpoint", "terminal"} else None

            def authenticate_inventory(self, layout, snapshot, anchor):
                del layout, snapshot
                return _AuthenticatedInventory(anchor) if self.stage in {"inventory", "receipt", "contract", "checkpoint", "terminal"} else None

            def authenticate_receipt(self, layout, snapshot, anchor, inventory):
                del layout, snapshot, anchor
                return _AuthenticatedReceipt(inventory) if self.stage in {"receipt", "contract", "checkpoint", "terminal"} else None

            def authenticate_contract(self, layout, snapshot, anchor, inventory, receipt):
                del layout, snapshot, anchor, inventory
                return _AuthenticatedContract(receipt) if self.stage in {"contract", "checkpoint", "terminal"} else None

            def authenticate_checkpoint_chain(self, layout, contract):
                del layout
                return _AuthenticatedCheckpointChain(contract) if self.stage == "checkpoint" else None

            def authenticate_terminal(self, layout, contract):
                del layout
                return _AuthenticatedTerminal(contract) if self.stage == "terminal" else None

        cases = (
            ("absent", False, False, "none", IssuanceState.ABSENT),
            ("locked", True, True, "terminal", IssuanceState.OBSERVER_INCOMPLETE_LOCKED),
            ("incomplete", False, True, "none", IssuanceState.OBSERVER_INCOMPLETE_UNLOCKED),
            ("executable", False, True, "contract", IssuanceState.EXECUTABLE),
            ("running", False, True, "checkpoint", IssuanceState.RUNNING),
            ("final", False, True, "terminal", IssuanceState.FINAL),
        )
        for _label, lock, final_root, stage, expected in cases:
            with self.subTest(state=expected):
                with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                    root = Path(temporary)
                    repository, custody = root / "repository", root / "custody"
                    repository.mkdir()
                    custody.mkdir()
                    if final_root:
                        (custody / "v5_1_bt_r4").mkdir()
                    if lock:
                        (custody / ".v5_1_bt_r4.issuance.lock").write_bytes(b"")
                    layout = _r4_authority_layout(repository, custody)
                    self.assertIs(
                        _observe_issuance_state(layout, FakeAuthenticator(stage)), expected
                    )


class SignedAnchorIssuanceRedTests(unittest.TestCase):
    """The signed anchor is part of the one-shot R4 release boundary."""

    @staticmethod
    def _fresh_issue_inputs(root: Path):
        """Build a distinct source/output pair entirely below one temp root."""
        from mlb_backtest import v52_r4_snapshot_issuer as issuer
        from tests import r4_issued_authority_fixture as fixture
        from mlb_backtest.protocols import expected_r4_recovery_source_run

        root = Path(root).resolve(strict=True)
        repository, source_repository = root / "repository", root / "source"
        repository.mkdir()
        source_repository.mkdir()
        fixture._write_source(source_repository)
        fixture._write_r3_contract_template(repository)
        run_spec = repository / Path(*fixture._RUN_SPEC.parts)
        run_spec.parent.mkdir(parents=True, exist_ok=True)
        run_spec.write_bytes(fixture._canonical_json_bytes(expected_r4_recovery_source_run()) + b"\n")
        source_root = source_repository / Path(*fixture._RAW.parts)
        source_inventory = source_repository / Path(*fixture._INVENTORY.parts)
        profile = fixture._fixture_source_authority_profile(source_root, source_inventory)
        custody_parent = root / "custody"
        issuer.provision_custody_parent(custody_parent)
        inputs = issuer.IssuanceInputs(
            source_root=source_root, source_inventory=source_inventory,
            source_approval_logical_root=fixture._RAW.as_posix(),
            source_approval_inventory=source_inventory, run_spec=run_spec,
            contract=repository / Path(*fixture._CONTRACT.parts),
            custody_parent=custody_parent, final_name="v5_1_bt_r4",
            audit_root=repository / Path(*fixture._AUDIT.parts),
        )

        def provider(*args: object, **kwargs: object):
            if args == (source_root, source_inventory):
                return fixture._IssuanceAuthority(profile, fixture._FIXTURE_ANCHOR_PRIVATE_KEY.sign)
            if not args and not kwargs:
                return profile
            raise AssertionError("unexpected authority resolution")

        return issuer, inputs, repository, custody_parent, provider

    def test_fixed_layout_exposes_anchor_and_detached_signature(self) -> None:
        layout = _r4_authority_layout(Path("repository"), Path("custody"))
        self.assertEqual(
            layout.anchor_relative.as_posix(),
            "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
            "acquisition/physical_custody_authority_anchor_r4.json",
        )
        self.assertEqual(
            layout.signature_relative.as_posix(),
            "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
            "acquisition/physical_custody_authority_anchor_r4.ed25519",
        )

    def test_each_preexisting_output_fails_before_snapshot_or_owner_lock(self) -> None:
        """No output authority entry may be adopted or overwritten."""
        from mlb_backtest import v52_r4_snapshot_issuer as issuer
        from tests import r4_issued_authority_fixture as fixture

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            source_repository = root / "source"
            source_repository.mkdir()
            fixture._write_source(source_repository)
            fixture._write_r3_contract_template(repository)
            run_spec = repository / Path(*fixture._RUN_SPEC.parts)
            run_spec.parent.mkdir(parents=True, exist_ok=True)
            from mlb_backtest.protocols import expected_r4_recovery_source_run

            run_spec.write_bytes(
                fixture._canonical_json_bytes(expected_r4_recovery_source_run()) + b"\n"
            )
            source_root = source_repository / Path(*fixture._RAW.parts)
            source_inventory = source_repository / Path(*fixture._INVENTORY.parts)
            profile = fixture._fixture_source_authority_profile(source_root, source_inventory)
            custody_parent = root / "custody"
            issuer.provision_custody_parent(custody_parent)
            layout = _r4_authority_layout(repository, custody_parent, profile)

            def provider(*args: object, **kwargs: object):
                if args == (source_root, source_inventory):
                    return fixture._IssuanceAuthority(
                        profile, fixture._FIXTURE_ANCHOR_PRIVATE_KEY.sign
                    )
                if not args and not kwargs:
                    return profile
                raise AssertionError("unexpected authority resolution")

            inputs = issuer.IssuanceInputs(
                source_root=source_root,
                source_inventory=source_inventory,
                source_approval_logical_root=fixture._RAW.as_posix(),
                source_approval_inventory=source_inventory,
                run_spec=run_spec,
                contract=repository / Path(*fixture._CONTRACT.parts),
                custody_parent=custody_parent,
                final_name="v5_1_bt_r4",
                audit_root=repository / Path(*fixture._AUDIT.parts),
            )
            for relative in (
                layout.inventory_relative, layout.receipt_relative, layout.contract_relative,
                layout.anchor_relative, layout.signature_relative,
            ):
                with self.subTest(path=relative):
                    existing = repository / Path(*relative.parts)
                    existing.parent.mkdir(parents=True, exist_ok=True)
                    existing.write_bytes(b"already present")
                    with patch(
                        "mlb_backtest.v52_r4_authority._resolve_source_authority",
                        side_effect=provider,
                    ), patch.object(
                        issuer, "_populate_snapshot_from_profile",
                        side_effect=AssertionError("snapshot mutation must not start"),
                    ):
                        with self.assertRaises(CUSTODY_UNAVAILABLE):
                            issuer.issue_and_bind(inputs)
                    self.assertFalse((custody_parent / "v5_1_bt_r4").exists())
                    self.assertFalse((custody_parent / ".v5_1_bt_r4.issuance.lock").exists())
                    self.assertEqual(existing.read_bytes(), b"already present")
                    existing.unlink()

    def test_preflight_ignores_source_side_r4_lookalike_and_observes_output(self) -> None:
        """Distinct source evidence must not block a clean output transaction."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            issuer, inputs, repository, custody, provider = self._fresh_issue_inputs(root)
            layout = _r4_authority_layout(repository, custody)
            # The old source-root state check rejected this harmless lookalike.
            # Inputs carry ``source/.../data/v5_1_bt/raw``; use the known
            # fixture authority parent without deriving any production path.
            source_repository = inputs.source_root.parents[2]
            lookalike = source_repository / Path(*layout.inventory_relative.parts)
            lookalike.parent.mkdir(parents=True, exist_ok=True)
            lookalike.write_bytes(b"source-side-only\n")
            self.assertFalse((repository / Path(*layout.inventory_relative.parts)).exists())
            with patch(
                "mlb_backtest.v52_r4_authority._resolve_source_authority", side_effect=provider
            ):
                result = issuer.issue_and_bind(inputs)
                self.assertIs(issuer.observe_issuance_state(repository, custody), IssuanceState.EXECUTABLE)
            self.assertEqual(result.custody_root, custody / layout.final_name)

    def test_repository_binding_resolves_canonical_relative_source_approval(self) -> None:
        """A canonical relative approval inventory is rooted at source authority."""
        from dataclasses import replace
        from tests import r4_issued_authority_fixture as fixture

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            issuer, inputs, repository, _custody, _provider = self._fresh_issue_inputs(root)
            profile = fixture._fixture_source_authority_profile(
                inputs.source_root, inputs.source_inventory
            )
            relative_inputs = replace(
                inputs,
                source_approval_inventory=Path(
                    "audit/v5_1_bt/protected_raw_inventory.json"
                ),
            )
            self.assertEqual(
                issuer._repository_for_issuance(relative_inputs, profile), repository
            )

    def test_repository_binding_rejects_noncanonical_or_foreign_source_approval(self) -> None:
        """Approval inventory must be the exact canonical path in one authority."""
        from dataclasses import replace
        from tests import r4_issued_authority_fixture as fixture

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            issuer, inputs, _repository, _custody, _provider = self._fresh_issue_inputs(root)
            profile = fixture._fixture_source_authority_profile(
                inputs.source_root, inputs.source_inventory
            )
            another_repository = root / "another-source"
            cases = (
                ("parent traversal", Path("../audit/v5_1_bt/protected_raw_inventory.json")),
                (
                    "noncanonical relative",
                    Path("audit/v5_1_bt/../v5_1_bt/protected_raw_inventory.json"),
                ),
                ("wrong absolute inventory", inputs.source_inventory.parent / "wrong.json"),
                (
                    "another repository",
                    another_repository / Path(*fixture._INVENTORY.parts),
                ),
            )
            for label, approval_inventory in cases:
                with self.subTest(label=label):
                    candidate = replace(
                        inputs, source_approval_inventory=approval_inventory
                    )
                    with self.assertRaises(CUSTODY_UNAVAILABLE):
                        issuer._repository_for_issuance(candidate, profile)

    def test_fixture_release_contains_a_valid_frozen_signed_anchor(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import verify_authority_anchor
        from tests import r4_issued_authority_fixture as fixture

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            issued = fixture.issue_test_r4_authority(Path(temporary))
            layout = _r4_authority_layout(issued.root, issued.custody_root.parent)
            anchor = issued.root / Path(*layout.anchor_relative.parts)
            signature = issued.root / Path(*layout.signature_relative.parts)
            self.assertTrue(anchor.is_file())
            self.assertEqual(len(signature.read_bytes()), 64)
            verified = verify_authority_anchor(
                anchor.read_bytes(), signature.read_bytes(),
                public_key=fixture._FIXTURE_ANCHOR_PUBLIC_KEY,
                expected_key_id=fixture._FIXTURE_ANCHOR_KEY_ID,
            )
            self.assertEqual(
                verified["artifacts"]["inventory"]["sha256"],
                hashlib.sha256(issued.inventory_path.read_bytes()).hexdigest(),
            )
            for path in (
                issued.inventory_path, issued.receipt_path, issued.contract_path,
                anchor, signature,
            ):
                self.assertEqual(path.stat().st_mode & 0o777, 0o444)
                self.assertTrue(path.stat().st_flags & getattr(stat, "UF_IMMUTABLE", 2))

    def test_anchor_publish_failure_keeps_owner_lock_and_incomplete_state(self) -> None:
        """A signer/publish failure must never release or clean up its lock."""
        from mlb_backtest import v52_r4_snapshot_issuer as issuer
        from tests import r4_issued_authority_fixture as fixture
        from mlb_backtest.protocols import expected_r4_recovery_source_run

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            repository, source_repository = root / "repository", root / "source"
            repository.mkdir()
            source_repository.mkdir()
            fixture._write_source(source_repository)
            fixture._write_r3_contract_template(repository)
            run_spec = repository / Path(*fixture._RUN_SPEC.parts)
            run_spec.parent.mkdir(parents=True, exist_ok=True)
            run_spec.write_bytes(fixture._canonical_json_bytes(expected_r4_recovery_source_run()) + b"\n")
            source_root = source_repository / Path(*fixture._RAW.parts)
            source_inventory = source_repository / Path(*fixture._INVENTORY.parts)
            profile = fixture._fixture_source_authority_profile(source_root, source_inventory)
            custody_parent = root / "custody"
            issuer.provision_custody_parent(custody_parent)
            inputs = issuer.IssuanceInputs(
                source_root=source_root, source_inventory=source_inventory,
                source_approval_logical_root=fixture._RAW.as_posix(),
                source_approval_inventory=source_inventory, run_spec=run_spec,
                contract=repository / Path(*fixture._CONTRACT.parts),
                custody_parent=custody_parent, final_name="v5_1_bt_r4",
                audit_root=repository / Path(*fixture._AUDIT.parts),
            )

            def provider(*args: object, **kwargs: object):
                if args == (source_root, source_inventory):
                    return fixture._IssuanceAuthority(profile, fixture._FIXTURE_ANCHOR_PRIVATE_KEY.sign)
                if not args and not kwargs:
                    return profile
                raise AssertionError("unexpected authority resolution")

            with patch(
                "mlb_backtest.v52_r4_authority._resolve_source_authority", side_effect=provider
            ), patch.object(
                issuer, "_publish_signed_anchor",
                side_effect=CUSTODY_UNAVAILABLE("injected signer failure"),
            ):
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "injected signer failure"):
                    issuer.issue_and_bind(inputs)
            self.assertTrue((custody_parent / ".v5_1_bt_r4.issuance.lock").is_file())
            self.assertIs(
                issuer.observe_issuance_state(repository, custody_parent),
                IssuanceState.OBSERVER_INCOMPLETE_LOCKED,
            )

    def test_unsettled_snapshot_identity_capture_keeps_owner_lock(self) -> None:
        """A bounded APFS stabilization failure is never releasable evidence."""
        from mlb_backtest import v52_r4_snapshot_issuer as issuer
        from tests import r4_issued_authority_fixture as fixture
        from mlb_backtest.protocols import expected_r4_recovery_source_run

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            repository, source_repository = root / "repository", root / "source"
            repository.mkdir()
            source_repository.mkdir()
            fixture._write_source(source_repository)
            fixture._write_r3_contract_template(repository)
            run_spec = repository / Path(*fixture._RUN_SPEC.parts)
            run_spec.parent.mkdir(parents=True, exist_ok=True)
            run_spec.write_bytes(fixture._canonical_json_bytes(expected_r4_recovery_source_run()) + b"\n")
            source_root = source_repository / Path(*fixture._RAW.parts)
            source_inventory = source_repository / Path(*fixture._INVENTORY.parts)
            profile = fixture._fixture_source_authority_profile(source_root, source_inventory)
            custody_parent = root / "custody"
            issuer.provision_custody_parent(custody_parent)
            inputs = issuer.IssuanceInputs(
                source_root=source_root, source_inventory=source_inventory,
                source_approval_logical_root=fixture._RAW.as_posix(),
                source_approval_inventory=source_inventory, run_spec=run_spec,
                contract=repository / Path(*fixture._CONTRACT.parts),
                custody_parent=custody_parent, final_name="v5_1_bt_r4",
                audit_root=repository / Path(*fixture._AUDIT.parts),
            )

            def provider(*args: object, **kwargs: object):
                if args == (source_root, source_inventory):
                    return fixture._IssuanceAuthority(profile, fixture._FIXTURE_ANCHOR_PRIVATE_KEY.sign)
                if not args and not kwargs:
                    return profile
                raise AssertionError("unexpected authority resolution")

            with patch(
                "mlb_backtest.v52_r4_authority._resolve_source_authority", side_effect=provider
            ), patch(
                "mlb_backtest.v52_r4_custody_artifacts.capture_frozen_snapshot",
                side_effect=CUSTODY_UNAVAILABLE("frozen snapshot APFS identity did not settle"),
            ):
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "did not settle"):
                    issuer.issue_and_bind(inputs)
            self.assertTrue((custody_parent / ".v5_1_bt_r4.issuance.lock").is_file())
            self.assertIs(
                issuer.observe_issuance_state(repository, custody_parent),
                IssuanceState.OBSERVER_INCOMPLETE_LOCKED,
            )

    def test_release_race_seam_covers_every_resource_and_timing_class(self) -> None:
        """Every destructive race test has a deterministic, private injection point."""
        from mlb_backtest import v52_r4_snapshot_issuer as issuer
        from tests import r4_issued_authority_fixture as fixture

        seen: set[tuple[str, str]] = set()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary, patch.object(
            issuer, "_r4_release_test_hook", side_effect=lambda stage, resource: seen.add((stage, resource))
        ):
            issued = fixture.issue_test_r4_authority(Path(temporary))
            layout = _r4_authority_layout(issued.root, issued.custody_root.parent)
        stages = {stage for stage, _resource in seen}
        self.assertEqual(stages, {"before_bundle", "during_bundle_auth", "final_rebind"})
        resources = {resource for _stage, resource in seen}
        self.assertIn("owner_lock", resources)
        self.assertIn("snapshot_root", resources)
        for relative in (
            layout.run_spec_relative, layout.inventory_relative, layout.receipt_relative,
            layout.contract_relative, layout.anchor_relative, layout.signature_relative,
        ):
            self.assertIn(relative.as_posix(), resources)
        self.assertTrue(any(resource.startswith("parent:") for resource in resources))

    def test_clean_issuance_reaches_every_release_stage_repeatedly(self) -> None:
        """A valid frozen fixture must reach the swap seams before any mutation."""
        from mlb_backtest import v52_r4_snapshot_issuer as issuer
        from tests import r4_issued_authority_fixture as fixture

        for attempt in range(3):
            with self.subTest(attempt=attempt), tempfile.TemporaryDirectory() as temporary:
                seen: set[str] = set()
                with patch.object(
                    issuer, "_r4_release_test_hook",
                    side_effect=lambda stage, resource: seen.add(stage),
                ):
                    fixture.issue_test_r4_authority(Path(temporary))
                self.assertEqual(seen, {"before_bundle", "during_bundle_auth", "final_rebind"})

    def test_every_release_swap_class_keeps_the_original_owner_lock(self) -> None:
        """All destructive swaps are real disposable-directory replacements."""
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        # Five frozen static leaves, their two distinct parent directories,
        # the frozen snapshot root, and the owner lock; each is replaced at
        # the three release boundaries named in the approved design.
        static_names = ("inventory", "receipt", "contract", "anchor", "signature")
        stages = ("before_bundle", "during_bundle_auth", "final_rebind")
        for stage in stages:
            for resource_kind in (
                *static_names,
                "audit_parent",
                "protocol_parent",
                "snapshot_root",
                "owner_lock",
                "temporary_root",
            ):
                with self.subTest(stage=stage, resource=resource_kind), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    candidate, inputs, repository, custody, provider = self._fresh_issue_inputs(root)
                    layout = _r4_authority_layout(repository, custody)
                    if resource_kind in static_names:
                        target = repository / Path(*getattr(layout, f"{resource_kind}_relative").parts)
                        hook_resource = getattr(layout, f"{resource_kind}_relative").as_posix()
                    elif resource_kind == "audit_parent":
                        target = repository / Path(*layout.inventory_relative.parent.parts)
                        hook_resource = f"parent:{target}"
                    elif resource_kind == "protocol_parent":
                        target = repository / Path(*layout.contract_relative.parent.parts)
                        hook_resource = f"parent:{target}"
                    elif resource_kind == "snapshot_root":
                        target = custody / layout.final_name
                        hook_resource = "snapshot_root"
                    elif resource_kind == "temporary_root":
                        target = custody / f".{layout.final_name}.issuance.matrix-race.tmp"
                        hook_resource = "owner_lock"
                    else:
                        target = custody / f".{layout.final_name}.issuance.lock"
                        hook_resource = "owner_lock"
                    fired = False

                    def clear_immutable_tree(path: Path) -> None:
                        for current, directories, files in os.walk(path, topdown=False):
                            for name in (*files, *directories):
                                os.chflags(Path(current) / name, 0, follow_symlinks=False)
                        os.chflags(path, 0, follow_symlinks=False)

                    def replace(stage_value: str, resource: str) -> None:
                        nonlocal fired
                        if fired or (stage_value, resource) != (stage, hook_resource):
                            return
                        fired = True
                        # Every replacement is constrained to this test's
                        # TemporaryDirectory; no source or Task 12 path is used.
                        self.assertTrue(target.resolve().is_relative_to(root.resolve()))
                        if resource_kind in static_names:
                            os.chflags(target, 0, follow_symlinks=False)
                            os.chmod(target, 0o600, follow_symlinks=False)
                            target.write_bytes(b"swapped static authority artifact\n")
                        elif resource_kind in {"audit_parent", "protocol_parent"}:
                            target.rename(target.with_name(target.name + ".swapped"))
                            target.mkdir()
                        elif resource_kind == "snapshot_root":
                            clear_immutable_tree(target)
                            target.rename(target.with_name(target.name + ".swapped"))
                            target.mkdir()
                        elif resource_kind == "temporary_root":
                            target.mkdir()
                        else:
                            target.rename(target.with_name(target.name + ".swapped"))
                            target.write_bytes(b"replacement lock\n")

                    with patch(
                        "mlb_backtest.v52_r4_authority._resolve_source_authority", side_effect=provider
                    ), patch.object(issuer, "_r4_release_test_hook", side_effect=replace):
                        with self.assertRaises(CUSTODY_UNAVAILABLE) as failure:
                            candidate.issue_and_bind(inputs)
                    self.assertTrue(fired, str(failure.exception))
                    self.assertTrue((custody / f".{layout.final_name}.issuance.lock").is_file())
                    self.assertIs(
                        candidate.observe_issuance_state(repository, custody),
                        IssuanceState.OBSERVER_INCOMPLETE_LOCKED,
                    )

    def test_final_rebind_audit_parent_swap_keeps_lock(self) -> None:
        """Targeted regression for the final owner-unlink race boundary."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            issuer, inputs, repository, custody, provider = self._fresh_issue_inputs(root)
            layout = _r4_authority_layout(repository, custody)
            target = repository / Path(*layout.inventory_relative.parent.parts)
            hook_resource = f"parent:{target.absolute()}"
            fired = False

            def replace(stage: str, resource: str) -> None:
                nonlocal fired
                if fired or (stage, resource) != ("final_rebind", hook_resource):
                    return
                fired = True
                self.assertTrue(target.resolve().is_relative_to(root.resolve()))
                target.rename(target.with_name(target.name + ".swapped"))
                target.mkdir()

            with patch(
                "mlb_backtest.v52_r4_authority._resolve_source_authority", side_effect=provider
            ), patch.object(issuer, "_r4_release_test_hook", side_effect=replace):
                with self.assertRaises(CUSTODY_UNAVAILABLE):
                    issuer.issue_and_bind(inputs)
            self.assertTrue(fired)
            self.assertTrue((custody / f".{layout.final_name}.issuance.lock").is_file())
            self.assertIs(
                issuer.observe_issuance_state(repository, custody),
                IssuanceState.OBSERVER_INCOMPLETE_LOCKED,
            )

    def test_final_owner_unlock_rejects_a_matching_temporary_root(self) -> None:
        """A temporary root created after final rebind must retain the lock."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            issuer, inputs, repository, custody, provider = self._fresh_issue_inputs(root)
            layout = _r4_authority_layout(repository, custody)
            temporary_root = custody / (
                f".{layout.final_name}.issuance.final-unlink-race.tmp"
            )
            fired = False

            def create_temporary_root(stage: str, resource: str) -> None:
                nonlocal fired
                if fired or (stage, resource) != ("final_rebind", "owner_lock"):
                    return
                fired = True
                self.assertTrue(temporary_root.is_relative_to(root))
                temporary_root.mkdir()

            with patch(
                "mlb_backtest.v52_r4_authority._resolve_source_authority", side_effect=provider
            ), patch.object(issuer, "_r4_release_test_hook", side_effect=create_temporary_root):
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "temporary root remains"):
                    issuer.issue_and_bind(inputs)
            self.assertTrue(fired)
            self.assertTrue((custody / f".{layout.final_name}.issuance.lock").is_file())
            self.assertTrue(temporary_root.is_dir())
            self.assertIs(
                issuer.observe_issuance_state(repository, custody),
                IssuanceState.OBSERVER_INCOMPLETE_LOCKED,
            )

    def test_fresh_process_raw_free_verify_accepts_only_original_signed_state(self) -> None:
        """Verification gets its fixture authority solely through the private seam."""
        from tests import r4_issued_authority_fixture as fixture

        program = """
import sys
from pathlib import Path
from unittest.mock import patch
from tests import r4_issued_authority_fixture as fixture
from mlb_backtest.v52_r4_snapshot_issuer import verify
root, source, custody = map(Path, sys.argv[1:])
profile = fixture._fixture_source_authority_profile(
    source / Path(*fixture._RAW.parts), source / Path(*fixture._INVENTORY.parts)
)
def provider(*args, **kwargs):
    if not args and not kwargs:
        return profile
    raise AssertionError('unexpected authority resolution')
with patch('mlb_backtest.v52_r4_authority._resolve_source_authority', side_effect=provider):
    verify(
        custody_root=custody,
        inventory=root / Path(*fixture._AUDIT.parts) / 'protected_raw_inventory_r4.json',
        receipt=root / Path(*fixture._AUDIT.parts) / 'physical_custody_transition_r4.json',
        run_spec=root / Path(*fixture._RUN_SPEC.parts),
        contract=root / Path(*fixture._CONTRACT.parts),
    )
raise SystemExit((root / Path(*fixture._RAW.parts)).exists())
"""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            issued = fixture.issue_test_r4_authority(Path(temporary))
            source = issued.root / "external-source-authority"
            command = [sys.executable, "-c", program, str(issued.root), str(source), str(issued.custody_root)]
            self.assertEqual(subprocess.run(command, cwd=Path.cwd(), check=False).returncode, 0)
            # All test operands stay inside the disposable directory before
            # unfreezing a copy of the signed inventory for this rejection case.
            self.assertTrue(issued.inventory_path.is_relative_to(Path(temporary)))
            os.chflags(issued.inventory_path, 0, follow_symlinks=False)
            os.chmod(issued.inventory_path, 0o600, follow_symlinks=False)
            issued.inventory_path.write_bytes(b"{}\n")
            self.assertNotEqual(subprocess.run(command, cwd=Path.cwd(), check=False).returncode, 0)


class SnapshotIssuerCorpusContractTests(unittest.TestCase):
    def test_exact_approved_inventory_is_descriptor_bound_before_copy(self) -> None:
        closure = _read_source_closure(
            Path.cwd(),
            inventory_relative=Path("audit/v5_1_bt/protected_raw_inventory.json"),
            expected_sha256="2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6",
        )
        self.assertEqual(len(closure.records), 1675)

    def test_approved_source_inventory_has_exact_unique_and_shared_closure(self) -> None:
        inventory_path = Path("audit/v5_1_bt/protected_raw_inventory.json")
        with inventory_path.open("rb") as handle:
            inventory = json.load(handle)
        records = inventory["records"]
        bindings = [
            (index, role, record[role]["path"])
            for index, record in enumerate(records)
            for role in ("manifest", "raw", "seal")
        ]
        unique_paths = {path for _, _, path in bindings}
        self.assertEqual(len(records), 1675)
        self.assertEqual(len(unique_paths), 4711)
        self.assertEqual(len(bindings), 5025)
        result = validate_source_closure(SnapshotSourceClosure(records))
        self.assertEqual(
            (result.record_count, result.physical_member_count, result.logical_binding_count),
            (1675, 4711, 5025),
        )
        for path, expected_count in _shared_raw_paths():
            references = [binding for binding in bindings if binding[2] == path]
            self.assertEqual(len(references), expected_count)
            self.assertEqual({role for _, role, _ in references}, {"raw"})
