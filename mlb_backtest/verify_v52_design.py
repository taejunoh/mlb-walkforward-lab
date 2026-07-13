from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from mlb_backtest.diagnostic_report import render_markdown
from mlb_backtest.diagnostics import (
    build_diagnostic,
    canonical_json_bytes,
    deep_freeze,
    load_v51_evidence,
    sha256_file,
)
from mlb_backtest.preflight import (
    PREFLIGHT_REQUIRED_KEYS,
    parse_preflight,
    protected_inventory,
    render_preflight_markdown,
)
from mlb_backtest.protocols import (
    ProspectiveSpec,
    RunSpec,
    V52Config,
    expected_historical_run,
    expected_prospective_spec,
    expected_v52_config,
    load_prospective_spec,
    load_run_spec,
    load_v52_config,
    parse_change_contract,
)
from mlb_backtest.report_artifact import build_chart_map, build_report_artifact


APPROVED_DESIGN_COMMIT = "a4aa8d2"
PROTECTED_TREES = ("data/v5_1_bt", "audit/v5_1_bt")
PROTECTED_FILES = ("config/v5_1_bt.json", "data/v5_1_bt/reports/summary.json")
HISTORICAL_NAMESPACE_ROOTS = (
    "data/v5_2_qd/historical",
    "reports/v5_2_qd/historical",
    "audit/v5_2_qd/historical",
)
PROSPECTIVE_OBSERVATION_ROOTS = (
    "data/v5_2_qd/prospective_2026",
    "reports/v5_2_qd/prospective_2026",
    "audit/v5_2_qd/prospective_2026",
    "models/v5_2_qd/prospective_2026",
)
FITTED_NAMESPACE_ROOT = "models/v5_2_qd/folds"
EXPECTED_CURRENT_FILES = (
    "reports/v5_1_bt/diagnostic_100.json",
    "reports/v5_1_bt/diagnostic_100.md",
    "analysis/v5_1_bt/diagnostic_100.ipynb",
    "reports/v5_1_bt/diagnostic_100_artifact.json",
    "reports/v5_1_bt/diagnostic_100_chart_map.json",
    "config/v5_2_qd.json",
    "schemas/v5_2_qd.schema.json",
    "schemas/v5_2_run_spec.schema.json",
    "schemas/v5_2_prospective.schema.json",
    "schemas/v5_2_preflight.schema.json",
    "protocols/v5_2_qd/change_contract.json",
    "protocols/v5_2_qd/historical_walkforward_500.json",
    "protocols/v5_2_qd/prospective_2026.json",
    "reports/v5_2_qd/walkforward_500_preflight.json",
    "reports/v5_2_qd/walkforward_500_preflight.md",
)
FORBIDDEN_CLAIMS = (
    "시장 대비 우위가 입증",
    "ROI가 양수",
    "CLV가 양수",
    "수익성이 입증",
    "actionable betting",
    "predictive improvement established",
)


def git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True
    )
    if completed.returncode:
        raise ValueError(completed.stderr.decode(errors="replace"))
    return completed.stdout


def build_git_inventory(
    root: Path, commit: str = APPROVED_DESIGN_COMMIT
) -> dict[str, object]:
    paths = (*PROTECTED_TREES, *PROTECTED_FILES)
    raw = git(root, "ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths)
    entries: dict[str, str] = {}
    for item in raw.rstrip(b"\0").split(b"\0") if raw else ():
        metadata, path = item.split(b"\t", 1)
        _mode, kind, oid = metadata.decode().split()
        if kind == "blob":
            entries[path.decode()] = oid
    return {
        "schema_version": 1,
        "approved_commit": commit,
        "algorithm": "git-blob-sha1",
        "protected_trees": list(PROTECTED_TREES),
        "protected_files": list(PROTECTED_FILES),
        "blobs": dict(sorted(entries.items())),
    }


def write_git_inventory(root: Path, commit: str) -> Path:
    path = root / "inventory.json"
    path.write_bytes(canonical_json_bytes(build_git_inventory(root, commit)) + b"\n")
    return path


def make_tiny_git_fixture(parent: Path) -> Path:
    repo = parent / "repo"
    (repo / "data/v5_1_bt/locked").mkdir(parents=True)
    (repo / "data/v5_1_bt/reports").mkdir(parents=True)
    (repo / "audit/v5_1_bt").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "data/v5_1_bt/locked/predictions.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    (repo / "data/v5_1_bt/reports/summary.json").write_text("{}\n", encoding="utf-8")
    (repo / "audit/v5_1_bt/model_card.json").write_text("{}\n", encoding="utf-8")
    (repo / "config/v5_1_bt.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "fixture"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _decode_nul_paths(raw: bytes) -> set[str]:
    return {item.decode() for item in raw.rstrip(b"\0").split(b"\0") if item}


def verify_protected_inventory(root: Path, inventory_path: Path) -> None:
    try:
        expected = json.loads(inventory_path.read_text(encoding="utf-8"))
        commit = expected["approved_commit"]
        if not isinstance(commit, str) or expected != build_git_inventory(root, commit):
            raise ValueError
        baseline = expected["blobs"]
        if not isinstance(baseline, dict):
            raise ValueError
        tracked = _decode_nul_paths(
            git(root, "ls-files", "-z", "--", *PROTECTED_TREES, *PROTECTED_FILES)
        )
        if tracked != set(baseline):
            raise ValueError
        for relative, oid in baseline.items():
            path = root / relative
            if (
                not isinstance(relative, str)
                or not isinstance(oid, str)
                or not path.is_file()
                or git(root, "hash-object", "--", relative).decode().strip() != oid
            ):
                raise ValueError
        changes = (
            git(
                root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *PROTECTED_TREES,
                *PROTECTED_FILES,
            )
            .decode()
            .splitlines()
        )
        if changes:
            raise ValueError
    except (KeyError, OSError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            "protected v5.1 inventory differs by mutation, addition, or deletion"
        ) from exc


def verify_protected_snapshot(root: Path, expected: Mapping[str, object]) -> None:
    """Compare the bounded preflight guard without hashing multi-GB raw trees."""
    if protected_inventory(root) != expected:
        raise ValueError("protected evidence metadata or small sealed digest differs")


def verify_diagnostic(root: Path) -> dict[str, object]:
    saved_path = root / "reports/v5_1_bt/diagnostic_100.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    recomputed = build_diagnostic(load_v51_evidence(root))
    if (
        saved_path.read_bytes() != canonical_json_bytes(saved) + b"\n"
        or canonical_json_bytes(saved) != canonical_json_bytes(recomputed)
        or saved["primary"]["record"] != {"W": 67, "L": 33, "N": 100}
    ):
        raise ValueError("diagnostic does not match sealed inputs")
    verify_leakage_evidence(saved)
    return saved


def verify_leakage_evidence(diagnostic: Mapping[str, object]) -> None:
    leakage = diagnostic.get("leakage")
    if not isinstance(leakage, Mapping):
        raise ValueError("diagnostic leakage evidence missing")
    statistical = leakage.get("statistical_as_of_safety")
    participant = leakage.get("participant_time_limitation")
    revision = leakage.get("source_revision_limitation")
    if not all(
        isinstance(value, Mapping) for value in (statistical, participant, revision)
    ):
        raise ValueError("diagnostic leakage evidence malformed")
    assert isinstance(statistical, Mapping)
    assert isinstance(participant, Mapping)
    assert isinstance(revision, Mapping)
    expected = {
        "status": "LIMITATION",
        "d1_feature_isolation_status": "PASS",
        "locked_row_outcome_field_isolation_status": "PASS",
        "lock_to_outcome_join_sequence_status": "NOT_TIMESTAMP_PROVEN",
        "calibration_status": "IN_SAMPLE_NOT_OOF",
    }
    if any(statistical.get(key) != value for key, value in expected.items()):
        raise ValueError("diagnostic leakage evidence differs")
    if (
        participant.get("status") != "LIMITATION"
        or participant.get("lineup") != "RECONSTRUCTED_FINAL_LINEUP"
        or participant.get("starter") != "RECONSTRUCTED_ACTUAL_STARTER"
        or revision.get("status") != "LIMITATION"
    ):
        raise ValueError("diagnostic leakage limitation differs")


def verify_markdown_agreement(root: Path, diagnostic: Mapping[str, object]) -> None:
    path = root / "reports/v5_1_bt/diagnostic_100.md"
    normalized = json.loads(canonical_json_bytes(diagnostic))
    top_six = normalized["team_concentration"]["top_six"]
    normalized["team_concentration"]["top_six"] = dict(
        sorted(top_six.items(), key=lambda item: (-item[1], item[0]))
    )
    if path.read_text(encoding="utf-8") != render_markdown(normalized):
        raise ValueError("Markdown differs from canonical diagnostic")


def verify_notebook_agreement(root: Path, diagnostic: Mapping[str, object]) -> None:
    notebook = json.loads(
        (root / "analysis/v5_1_bt/diagnostic_100.ipynb").read_text(encoding="utf-8")
    )
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        raise ValueError("notebook cells missing")
    code = [cell for cell in cells if cell.get("cell_type") == "code"]
    try:
        output = code[-1]["outputs"][0]
        text_value = output["text"]
        text = "".join(text_value) if isinstance(text_value, list) else text_value
        receipt = json.loads(text)
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("notebook receipt missing") from exc
    expected_digest = hashlib.sha256(
        canonical_json_bytes(diagnostic) + b"\n"
    ).hexdigest()
    has_error = any(
        output.get("output_type") == "error"
        for cell in code
        for output in cell.get("outputs", [])
    )
    execution_counts = [cell.get("execution_count") for cell in code]
    if (
        receipt
        != {
            "diagnostic_sha256": expected_digest,
            "primary": {"L": 33, "N": 100, "W": 67},
            "recomputed_from_sealed_inputs": True,
        }
        or has_error
        or execution_counts != list(range(1, len(code) + 1))
    ):
        raise ValueError("notebook does not agree with diagnostic")


def verify_artifact_agreement(
    root: Path, diagnostic: Mapping[str, object]
) -> Mapping[str, object]:
    artifact_path = root / "reports/v5_1_bt/diagnostic_100_artifact.json"
    chart_map_path = root / "reports/v5_1_bt/diagnostic_100_chart_map.json"
    saved = json.loads(artifact_path.read_text(encoding="utf-8"))
    expected = build_report_artifact(diagnostic)
    if (
        saved != expected
        or artifact_path.read_bytes() != canonical_json_bytes(saved) + b"\n"
    ):
        raise ValueError("artifact differs from diagnostic")
    chart_map = json.loads(chart_map_path.read_text(encoding="utf-8"))
    if (
        chart_map != build_chart_map()
        or chart_map_path.read_bytes() != canonical_json_bytes(chart_map) + b"\n"
    ):
        raise ValueError("chart map differs from deterministic renderer")
    return saved


def verify_contract_schemas(
    root: Path,
    config: V52Config,
    historical: RunSpec,
    prospective: ProspectiveSpec,
    preflight: Mapping[str, object],
) -> None:
    pairs = (
        ("schemas/v5_2_qd.schema.json", config.raw, expected_v52_config()),
        (
            "schemas/v5_2_run_spec.schema.json",
            historical.raw,
            expected_historical_run(),
        ),
        (
            "schemas/v5_2_prospective.schema.json",
            prospective.raw,
            expected_prospective_spec(),
        ),
    )
    for relative, parsed, expected in pairs:
        schema = json.loads((root / relative).read_text(encoding="utf-8"))
        if deep_freeze(schema.get("const")) != parsed or parsed != deep_freeze(
            expected
        ):
            raise ValueError(f"schema/parser disagreement: {relative}")
    schema = json.loads(
        (root / "schemas/v5_2_preflight.schema.json").read_text(encoding="utf-8")
    )
    if (
        set(schema.get("required", ())) != PREFLIGHT_REQUIRED_KEYS
        or set(schema.get("properties", ())) != PREFLIGHT_REQUIRED_KEYS
        or schema.get("additionalProperties") is not False
        or set(preflight) != PREFLIGHT_REQUIRED_KEYS
    ):
        raise ValueError("preflight schema/parser disagreement")


def verify_change_contract(root: Path) -> Mapping[str, object]:
    contract = parse_change_contract(
        json.loads(
            (root / "protocols/v5_2_qd/change_contract.json").read_text(
                encoding="utf-8"
            )
        )
    )
    if contract["config_sha256"] != sha256_file(root / "config/v5_2_qd.json"):
        raise ValueError("change contract config digest differs")
    design_path = (
        "docs/superpowers/specs/2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md"
    )
    design_blob = (
        git(root, "rev-parse", f"{APPROVED_DESIGN_COMMIT}:{design_path}")
        .decode()
        .strip()
    )
    design_digest = hashlib.sha256(
        git(root, "cat-file", "blob", design_blob)
    ).hexdigest()
    if contract["approved_design_blob_sha256"] != design_digest:
        raise ValueError("change contract design digest differs")
    return contract


def verify_prospective_pin(
    root: Path, prospective: ProspectiveSpec, contract: Mapping[str, object]
) -> None:
    freeze_pin = prospective.raw.get("freeze_pin")
    if not isinstance(freeze_pin, Mapping):
        raise ValueError("prospective freeze pin missing")
    if (
        freeze_pin.get("approved_design_commit") != APPROVED_DESIGN_COMMIT
        or freeze_pin.get("approved_design_blob_sha256")
        != contract["approved_design_blob_sha256"]
    ):
        raise ValueError("prospective design digest differs")


def verify_forbidden_claims(root: Path) -> None:
    paths = (
        root / "reports/v5_1_bt/diagnostic_100.md",
        root / "reports/v5_1_bt/diagnostic_100_artifact.json",
        root / "analysis/v5_1_bt/diagnostic_100.ipynb",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "독립 전향 검증, 시장 우위, ROI, CLV, 수익성, actionable betting 또는 "
            "v5.2 예측력 개선을 주장하지 않는다.",
            "",
        )
        if any(claim in text for claim in FORBIDDEN_CLAIMS):
            raise ValueError(f"forbidden claim in {path}")


def verify_preflight(root: Path) -> dict[str, object]:
    json_path = root / "reports/v5_2_qd/walkforward_500_preflight.json"
    payload = parse_preflight(json.loads(json_path.read_text(encoding="utf-8")))
    if json_path.read_bytes() != canonical_json_bytes(payload) + b"\n":
        raise ValueError("preflight JSON is not canonical")
    if (
        payload["status"] != "BLOCKED_REQUIRED_ACQUISITION"
        or payload["may_fit_H22"]
        or payload["fitted_model_cards_present"]
    ):
        raise ValueError("current preflight status differs")
    if payload["protected_tree_before"] != payload["protected_tree_after"]:
        raise ValueError("preflight protected evidence changed during scan")
    verify_protected_snapshot(root, payload["protected_tree_after"])
    markdown_path = root / "reports/v5_2_qd/walkforward_500_preflight.md"
    if markdown_path.read_text(encoding="utf-8") != render_preflight_markdown(payload):
        raise ValueError("preflight Markdown differs from canonical JSON")
    return payload


def fitted_model_card_count(root: Path) -> int:
    base = root / FITTED_NAMESPACE_ROOT
    return sum(path.is_file() for path in base.rglob("*")) if base.exists() else 0


def _namespace_status(root: Path, relatives: Sequence[str], absent: str) -> str:
    files = [
        path
        for relative in relatives
        if (root / relative).exists()
        for path in (root / relative).rglob("*")
        if path.is_file()
    ]
    return "PARTIAL_OUTPUT_PRESENT" if files else absent


def historical_execution_status(root: Path) -> str:
    return _namespace_status(root, HISTORICAL_NAMESPACE_ROOTS, "NOT_EXECUTED")


def observation_namespace_status(root: Path) -> str:
    return _namespace_status(root, PROSPECTIVE_OBSERVATION_ROOTS, "NOT_STARTED")


def ensure_expected_files(root: Path) -> None:
    missing = [
        relative
        for relative in EXPECTED_CURRENT_FILES
        if not (root / relative).is_file()
    ]
    if missing:
        raise ValueError(f"missing current-scope files: {missing}")


def verify_namespaces_differ(historical: RunSpec, prospective: ProspectiveSpec) -> None:
    historical_namespaces = set(historical.raw["namespaces"].values())
    if prospective.namespace in historical_namespaces:
        raise ValueError("historical and prospective namespaces overlap")


def verify_html_payload(
    html_path: Path, artifact_path: Path, receipt_path: Path
) -> dict[str, object]:
    plugin = Path(
        "/Users/taejunoh/.codex/plugins/cache/openai-curated-remote/"
        "data-analytics/0.2.8-13ceeea1f599"
    )
    completed = subprocess.run(
        [
            "npm",
            "run",
            "report:verify",
            "--",
            "--html",
            str(html_path),
            "--artifact",
            str(artifact_path),
        ],
        cwd=plugin,
        check=False,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    stages = receipt.get("stages", {})
    if (
        completed.returncode != 0
        or '"ok":true' not in completed.stdout.replace(" ", "")
        or any(
            stages.get(stage) != "passed"
            for stage in ("validation", "package", "verification")
        )
    ):
        raise ValueError(f"portable HTML verification failed: {completed.stderr}")
    return {"sha256": sha256_file(html_path), "receipt": receipt}


def build_pass_receipt(
    diagnostic: Mapping[str, object],
    config: V52Config,
    prospective: ProspectiveSpec,
    preflight: Mapping[str, object],
    fitted_count: int,
    execution: str,
    observation: str,
    inventory: Mapping[str, object],
    html: object,
) -> dict[str, object]:
    return {
        "status": "PASS",
        "diagnostic": {"primary": diagnostic["primary"]["record"]},
        "v52": {
            "model_version": config.model_version,
            "fitted_model_card_count": fitted_count,
        },
        "preflight": {"status": preflight["status"]},
        "historical": {"execution_status": execution},
        "prospective": {
            "first_permitted_date": prospective.first_permitted_date.isoformat(),
            "observation_status": observation,
        },
        "immutability": {
            "approved_commit": inventory["approved_commit"],
            "tracked_blob_count": len(inventory["blobs"]),
            "full_tree_guard": "metadata_path_size_mtime_inode_mode",
            "small_sealed_guard": "sha256",
        },
        "html": html,
    }


def verify_design_deliverables(root: Path, outputs: Path | None) -> dict[str, object]:
    root = root.resolve()
    inventory_path = root / "protocols/v5_2_qd/v51_protected_inventory.json"
    verify_protected_inventory(root, inventory_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if inventory["approved_commit"] != APPROVED_DESIGN_COMMIT:
        raise ValueError("protected v5.1 inventory is not pinned to approved commit")
    ensure_expected_files(root)
    diagnostic = verify_diagnostic(root)
    verify_markdown_agreement(root, diagnostic)
    verify_notebook_agreement(root, diagnostic)
    verify_artifact_agreement(root, diagnostic)
    config = load_v52_config(root / "config/v5_2_qd.json")
    historical = load_run_spec(
        root / "protocols/v5_2_qd/historical_walkforward_500.json"
    )
    prospective = load_prospective_spec(
        root / "protocols/v5_2_qd/prospective_2026.json"
    )
    preflight = verify_preflight(root)
    verify_contract_schemas(root, config, historical, prospective, preflight)
    contract = verify_change_contract(root)
    verify_prospective_pin(root, prospective, contract)
    verify_forbidden_claims(root)
    verify_namespaces_differ(historical, prospective)
    fitted_count = fitted_model_card_count(root)
    if fitted_count:
        raise ValueError("fitted fold card exists before source acquisition")
    execution = historical_execution_status(root)
    if execution != "NOT_EXECUTED":
        raise ValueError("historical outputs exist in design-only phase")
    observation = observation_namespace_status(root)
    if observation != "NOT_STARTED":
        raise ValueError(
            "prospective observations exist before collector implementation"
        )
    html = None
    if outputs is not None:
        html = verify_html_payload(
            outputs / "mlb_v5_1_100_pick_diagnostic.html",
            root / "reports/v5_1_bt/diagnostic_100_artifact.json",
            root / "reports/v5_1_bt/report_delivery_receipt.json",
        )
    return build_pass_receipt(
        diagnostic,
        config,
        prospective,
        preflight,
        fitted_count,
        execution,
        observation,
        inventory,
        html,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--outputs", type=Path)
    parser.add_argument("--skip-html", action="store_true")
    args = parser.parse_args(argv)
    if not args.skip_html and args.outputs is None:
        parser.error("--outputs is required unless --skip-html is set")
    receipt = verify_design_deliverables(
        args.root.resolve(), None if args.skip_html else args.outputs
    )
    print(json.dumps(receipt, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
