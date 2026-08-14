"""One tiny-payload, full-closure R4 authority fixture."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mlb_backtest.v52_r4_snapshot_issuer import (
    IssuanceInputs,
    issue_and_bind,
    observe_issuance_state,
    provision_custody_parent,
    verify,
)
from mlb_backtest.protocols import expected_r4_recovery_source_run
from mlb_backtest.v52_r4_authority import (
    INVENTORY_RELATIVE,
    LOGICAL_RAW_ROOT,
    R4SourceAuthorityProfile,
    SnapshotSourceClosure,
    _IssuanceAuthority,
    _PROFILE_TOKEN,
    _canonical_json_bytes as _authority_canonical_json_bytes,
    _verify_records,
)


_RAW = PurePosixPath("data/v5_1_bt/raw")
_INVENTORY = PurePosixPath("audit/v5_1_bt/protected_raw_inventory.json")
_RUN_SPEC = PurePosixPath(
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json"
)
_CONTRACT = PurePosixPath(
    "protocols/v5_2_qd/"
    "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json"
)
_AUDIT = PurePosixPath(
    "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition"
)
_SOURCES = (
    ("baseball_savant_statcast", 213),
    ("mlb_schedule", 5),
    ("mlb_stats_pitcher_bf", 731),
    ("mlb_stats_pitching", 726),
)
_FIXTURE_ANCHOR_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_FIXTURE_ANCHOR_PUBLIC_KEY = _FIXTURE_ANCHOR_PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
)
_FIXTURE_ANCHOR_KEY_ID = "ed25519-sha256:" + hashlib.sha256(_FIXTURE_ANCHOR_PUBLIC_KEY).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


@dataclass(frozen=True)
class IssuedR4Fixture:
    root: Path
    custody_root: Path
    inventory_path: Path
    receipt_path: Path
    contract_path: Path
    run_spec_path: Path
    sample_logical_path: PurePosixPath
    sample_payload: bytes


def _query(source: str, index: int) -> dict[str, object]:
    common = {"url": f"https://fixture.invalid/{source}/{index}"}
    if source == "baseball_savant_statcast":
        return {**common, "start": "2020-01-01", "end": "2020-01-02", "game_type": "R"}
    if source == "mlb_schedule":
        return {**common, "start": "2020-01-01", "end": "2020-01-02", "gameType": "R", "role": "schedule"}
    if source == "mlb_stats_pitcher_bf":
        return {**common, "startDate": "2020-01-01", "endDate": "2020-01-02", "gameType": "R", "group": "pitching", "stats": "career", "pitcher_id": index + 1, "sportIds": 1}
    return {**common, "startDate": "2020-01-01", "endDate": "2020-01-02", "gameType": "R", "group": "pitching", "playerPool": "QUALIFIED", "stats": "season", "limit": 1, "sportIds": 1}


def _records() -> tuple[list[dict[str, object]], dict[PurePosixPath, bytes], PurePosixPath]:
    records: list[dict[str, object]] = []
    payloads: dict[PurePosixPath, bytes] = {}
    shared_a = _RAW / "mlb_stats_pitcher_bf" / "729bad325d9eafb22883c3318896f2baf877c99b5600fa77aba2bb9f6da6a6e7.raw"
    shared_b = _RAW / "mlb_stats_pitcher_bf" / "f8615992094a949312f8a27b4e129e9c14abd006b994818642110733e260d86f.raw"
    sample = shared_a
    raw_unique = 0
    index = 0
    for source, count in _SOURCES:
        for local in range(count):
            def artifact(role: str, token: str) -> dict[str, object]:
                path = _RAW / source / f"{role}-{token}.bin"
                payload = f"{role}:{token}".encode()
                payloads.setdefault(path, payload)
                return {"path": path.as_posix(), "sha256": hashlib.sha256(payload).hexdigest(), "byte_size": len(payload)}

            if index < 85:
                raw_path = shared_a
            elif index < 316:
                raw_path = shared_b
            else:
                raw_path = _RAW / source / f"raw-{raw_unique:04d}.bin"
                raw_unique += 1
            raw_payload = f"raw:{raw_path.name}".encode()
            payloads.setdefault(raw_path, raw_payload)
            records.append({
                "source": source,
                "canonical_query": _query(source, local),
                "rawstore_verified": True,
                "manifest": artifact("manifest", f"{index:04d}"),
                "raw": {"path": raw_path.as_posix(), "sha256": hashlib.sha256(raw_payload).hexdigest(), "byte_size": len(raw_payload)},
                "seal": artifact("seal", f"{index:04d}"),
            })
            index += 1
    assert len(records) == 1675 and len(payloads) == 4711 and raw_unique == 1359
    return records, payloads, sample


def _write_source(root: Path) -> tuple[list[dict[str, object]], PurePosixPath, bytes]:
    records, payloads, sample = _records()
    for logical, payload in payloads.items():
        path = root / Path(*logical.parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    raw_root = root / Path(*_RAW.parts)
    for lock in (
        ".baseball_savant_statcast.lock", ".mlb_schedule.lock",
        ".mlb_stats_pitcher_bf.lock", ".mlb_stats_pitching.lock",
    ):
        (raw_root / lock).write_bytes(b"")
    source_inventory = {"legacy_root": _RAW.as_posix(), "manifest_count": 1675, "records": records}
    target = root / Path(*_INVENTORY.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_canonical_json_bytes(source_inventory))
    return records, sample, payloads[sample]


def _write_r3_contract_template(repository: Path) -> None:
    """Supply the immutable semantic template consumed by the real builder."""
    source = Path(__file__).resolve().parent.parent / (
        "protocols/v5_2_qd/"
        "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"
    )
    target = repository / source.relative_to(source.parents[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())


def _fixture_source_authority_profile(
    source_root: Path, source_inventory: Path,
) -> R4SourceAuthorityProfile:
    """Construct the synthetic capability solely for the private issuer seam."""
    payload = source_inventory.read_bytes()
    document = json.loads(payload)
    records, evidence = _verify_records(document["records"])
    digest = hashlib.sha256(payload).hexdigest()
    approval = MappingProxyType({
        "logical_raw_root": LOGICAL_RAW_ROOT.as_posix(),
        "protected_inventory_path": INVENTORY_RELATIVE.as_posix(),
        "protected_inventory_sha256": digest,
    })
    evidence_rows = [dict(item) for item in evidence]
    return R4SourceAuthorityProfile(
        authority_root=source_root.parents[len(LOGICAL_RAW_ROOT.parts) - 1],
        logical_raw_root=LOGICAL_RAW_ROOT,
        inventory_relative=INVENTORY_RELATIVE,
        inventory_sha256=digest,
        source_approval=approval,
        closure=SnapshotSourceClosure(records),
        physical_evidence=evidence,
        physical_closure_sha256=hashlib.sha256(_authority_canonical_json_bytes(evidence_rows)).hexdigest(),
        record_count=1675,
        physical_member_count=4711,
        logical_binding_count=5025,
        anchor_public_key=_FIXTURE_ANCHOR_PUBLIC_KEY,
        anchor_key_id=_FIXTURE_ANCHOR_KEY_ID,
        _token=_PROFILE_TOKEN,
    )


def issue_test_r4_authority(root: Path) -> IssuedR4Fixture:
    """Build and issue a complete 1,675/4,711/5,025 authority under ``root``."""
    # macOS commonly returns ``/var/...`` from TemporaryDirectory while
    # custody's no-follow resolver intentionally refuses that symlink.  This
    # is a fixture-only lexical canonicalization before any authority path is
    # constructed; production keeps its strict no-symlink root rule.
    repository = Path(root).resolve(strict=True)
    source_repository = repository / "external-source-authority"
    source_repository.mkdir()
    records, sample, sample_payload = _write_source(source_repository)
    _write_r3_contract_template(repository)
    run_spec = repository / Path(*_RUN_SPEC.parts)
    run_spec.parent.mkdir(parents=True, exist_ok=True)
    run_spec.write_bytes(_canonical_json_bytes(expected_r4_recovery_source_run()) + b"\n")
    source_inventory = source_repository / Path(*_INVENTORY.parts)
    profile = _fixture_source_authority_profile(
        source_repository / Path(*_RAW.parts), source_inventory,
    )

    def test_local_provider(*args: object, **kwargs: object):
        if not args and not kwargs:
            return profile
        if args == (source_repository / Path(*_RAW.parts), source_inventory):
            return _IssuanceAuthority(profile, _FIXTURE_ANCHOR_PRIVATE_KEY.sign)
        raise AssertionError("fixture source authority provider mode differs")
    custody_parent = repository / "custody"
    provision_custody_parent(custody_parent)
    inputs = IssuanceInputs(
        source_root=source_repository / Path(*_RAW.parts), source_inventory=source_inventory,
        source_approval_logical_root=_RAW.as_posix(), source_approval_inventory=source_inventory,
        run_spec=run_spec, contract=repository / Path(*_CONTRACT.parts),
        custody_parent=custody_parent, final_name="v5_1_bt_r4",
        audit_root=repository / Path(*_AUDIT.parts),
    )
    with patch(
        "mlb_backtest.v52_r4_authority._resolve_source_authority",
        side_effect=test_local_provider,
    ) as resolver:
        result = issue_and_bind(inputs)
        verify(custody_root=result.custody_root, inventory=result.inventory_path, receipt=result.receipt_path, run_spec=result.run_spec_path, contract=result.contract_path)
        if observe_issuance_state(repository, custody_parent).value != "EXECUTABLE":
            raise AssertionError("fixture authority is not executable")
        if (custody_parent / ".v5_1_bt_r4.issuance.lock").exists() or any(
            child.name.startswith(".v5_1_bt_r4.issuance.") and child.name.endswith(".tmp")
            for child in custody_parent.iterdir()
        ):
            raise AssertionError("fixture authority retained incomplete issuance state")
        expected_paths = (
            custody_parent / "v5_1_bt_r4",
            repository / Path(*_AUDIT.parts) / "protected_raw_inventory_r4.json",
            repository / Path(*_AUDIT.parts) / "physical_custody_transition_r4.json",
            repository / Path(*_CONTRACT.parts),
            repository / Path(*_RUN_SPEC.parts),
        )
        observed_paths = (
            result.custody_root,
            result.inventory_path,
            result.receipt_path,
            result.contract_path,
            result.run_spec_path,
        )
        if observed_paths != expected_paths:
            raise AssertionError("fixture authority paths differ from fixed R4 layout")
        if (repository / Path(*_RAW.parts)).exists():
            raise AssertionError("output repository must not retain source raw authority")
        if (
            resolver.call_count < 2
            or resolver.call_args_list[0].args
            != (source_repository / Path(*_RAW.parts), source_inventory)
            or not any(not call.args and not call.kwargs for call in resolver.call_args_list)
        ):
            raise AssertionError("fixture source authority provider calls differ")
    return IssuedR4Fixture(repository, result.custody_root, result.inventory_path, result.receipt_path, result.contract_path, result.run_spec_path, sample, sample_payload)
