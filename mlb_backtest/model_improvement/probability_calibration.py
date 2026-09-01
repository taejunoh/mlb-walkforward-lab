"""Read-only admission of source-derived, pre-target calibration authority."""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping

from mlb_backtest.normalize import fit_logistic
from mlb_backtest.v52_snapshot_store import parse_inventory
from mlb_backtest.v52_game_evidence_inventory import (
    load_verified_game_evidence_inventory,
)

from .canonical import canonical_json_bytes, read_regular_bytes
from .calibration_feature_receipts import (
    CalibrationFeatureReceiptV1,
    load_calibration_feature_receipt,
)
from .contracts import (
    COMPONENTS,
    ProbabilityCalibrationCard,
    ProbabilityCalibrationCardV2,
    _require_sha256,
)
from .run_spec import ModelImprovementRunSpec, _repository_root, run_spec_sha256
from .scoring_cards import resolve_prediction_weight_authority
from .source_gate import SourceGateReceipt, load_bound_source_gate_receipt


_SCHEMA = "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V1"
_CARD_FIELDS = frozenset({"schema"}) | (
    frozenset(ProbabilityCalibrationCard.__dataclass_fields__)
    - {"artifact_sha256", "path"}
)
_ROW_FIELDS = frozenset(
    {
        "game_pk",
        "official_date",
        "target_year",
        "edge",
        "home_win",
        "feature_provenance_sha256",
        "game_evidence_sha256",
    }
)


class CalibrationAuthorityBlockedError(RuntimeError):
    """The immutable prior-target authority has not been published yet."""

    code = "BLOCKED_CALIBRATION_AUTHORITY_MISSING"

    def __init__(self) -> None:
        super().__init__(self.code)


class WeightSelectionAuthorityBlockedError(RuntimeError):
    code = "BLOCKED_WEIGHT_SELECTION_AUTHORITY_MISSING"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CalibrationPaths:
    rows: Path
    rows_sidecar: Path
    card: Path
    card_sidecar: Path


def _identity(
    spec: ModelImprovementRunSpec,
    model_id: str,
    lane_id: str,
    fold_id: str,
    candidate_id: str | None,
) -> tuple[str, str, str, str | None]:
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("calibration requires ModelImprovementRunSpec")
    expected = {
        "v5.1": ("baseline", None),
        "A": ("A", None),
        "A+B": ("B", "required"),
        "A+B+C": ("C", None),
    }.get(model_id)
    if (
        expected is None
        or lane_id != expected[0]
        or fold_id not in spec.probability_calibration_policy.fold_keys
    ):
        raise ValueError("probability calibration identity differs")
    if expected[1] == "required":
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or candidate_id in {".", ".."}
        ):
            raise ValueError("probability calibration candidate differs")
        if not all(part not in candidate_id for part in ("/", "\\")):
            raise ValueError("probability calibration candidate differs")
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate_id):
            raise ValueError("probability calibration candidate differs")
    elif candidate_id is not None:
        raise ValueError("probability calibration candidate differs")
    return model_id, lane_id, fold_id, candidate_id


def calibration_paths(
    spec: ModelImprovementRunSpec,
    model_id: str,
    lane_id: str,
    fold_id: str,
    candidate_id: str | None,
) -> CalibrationPaths:
    """Derive the only admitted calibration card and rows paths."""
    model_id, lane_id, fold_id, candidate_id = _identity(
        spec, model_id, lane_id, fold_id, candidate_id
    )
    policy = spec.probability_calibration_policy
    if model_id == "v5.1":
        values = {"fold_id": fold_id}
        rows_relative = policy.baseline_rows_relative_template.format(**values)
        card_relative = policy.baseline_card_relative_template.format(**values)
    else:
        values = {
            "lane_id": lane_id,
            "candidate_segment": "_none" if candidate_id is None else candidate_id,
            "fold_id": fold_id,
        }
        rows_relative = policy.candidate_rows_relative_template.format(**values)
        card_relative = policy.candidate_card_relative_template.format(**values)
    rows = spec.output_path("calibration", rows_relative)
    card = spec.output_path("calibration", card_relative)
    return CalibrationPaths(
        rows,
        rows.with_suffix(rows.suffix + ".sha256"),
        card,
        card.with_suffix(card.suffix + ".sha256"),
    )


def _missing(path: Path) -> bool:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise ValueError("calibration authority path is unsafe") from exc
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("calibration authority path is unsafe")
    return False


def _canonical_absolute(path: Path | str, label: str) -> Path:
    raw = os.fspath(path)
    candidate = Path(raw)
    if (
        not isinstance(raw, str)
        or not candidate.is_absolute()
        or raw != str(candidate)
        or raw != os.path.normpath(raw)
    ):
        raise ValueError(f"{label} path differs")
    return candidate


def _sidecar(path: Path, digest: str, label: str) -> None:
    raw = read_regular_bytes(path, label)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if raw != canonical_json_bytes(payload) or payload != {"artifact_sha256": digest}:
        raise ValueError(f"{label} differs")


def _rows(
    raw: bytes, card: ProbabilityCalibrationCard, spec: ModelImprovementRunSpec
) -> list[Mapping[str, object]]:
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("calibration rows differ")
    rows: list[Mapping[str, object]] = []
    previous = -1
    pool = spec.probability_calibration_policy.pools[card.calibration_key]
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("calibration row is not JSON") from exc
        if (
            not isinstance(row, Mapping)
            or set(row) != _ROW_FIELDS
            or canonical_json_bytes(row).rstrip(b"\n") != line
        ):
            raise ValueError("calibration row differs")
        game_pk = row["game_pk"]
        if (
            isinstance(game_pk, bool)
            or not isinstance(game_pk, int)
            or game_pk <= previous
        ):
            raise ValueError("calibration rows are not strictly ordered")
        previous = game_pk
        if row["target_year"] != pool.target_year or not isinstance(
            row["official_date"], str
        ):
            raise ValueError("calibration row target differs")
        if not (pool.training_start <= row["official_date"] <= pool.training_end):
            raise ValueError("calibration row is outside its prior-target pool")
        if (
            isinstance(row["edge"], bool)
            or not isinstance(row["edge"], (int, float))
            or not math.isfinite(float(row["edge"]))
        ):
            raise ValueError("calibration edge differs")
        if row["home_win"] not in (0, 1) or isinstance(row["home_win"], bool):
            raise ValueError("calibration outcome differs")
        _require_sha256(row["feature_provenance_sha256"], "feature_provenance_sha256")
        _require_sha256(row["game_evidence_sha256"], "game_evidence_sha256")
        rows.append(row)
    if len(rows) < spec.probability_calibration_policy.fit_spec.min_rows:
        raise ValueError("calibration rows are below minimum")
    if {row["home_win"] for row in rows} != set(
        spec.probability_calibration_policy.fit_spec.required_classes
    ):
        raise ValueError("calibration rows require both classes")
    if (
        sha256(canonical_json_bytes([row["game_pk"] for row in rows])).hexdigest()
        != card.calibration_population_sha256
    ):
        raise ValueError("calibration population differs")
    return rows


def _load_once(
    spec: ModelImprovementRunSpec,
    model_id: str,
    lane_id: str,
    fold_id: str,
    candidate_id: str | None,
) -> ProbabilityCalibrationCard:
    paths = calibration_paths(spec, model_id, lane_id, fold_id, candidate_id)
    if any(
        _missing(path)
        for path in (paths.rows, paths.rows_sidecar, paths.card, paths.card_sidecar)
    ):
        raise CalibrationAuthorityBlockedError()
    card_raw = read_regular_bytes(paths.card, "probability calibration card")
    digest = sha256(card_raw).hexdigest()
    _sidecar(paths.card_sidecar, digest, "probability calibration card sidecar")
    try:
        persisted = json.loads(card_raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("probability calibration card is not JSON") from exc
    if not isinstance(persisted, Mapping) or card_raw != canonical_json_bytes(
        persisted
    ):
        raise ValueError("probability calibration card is not canonical JSON")
    if set(persisted) != _CARD_FIELDS or persisted["schema"] != _SCHEMA:
        raise ValueError("probability calibration card schema differs")
    payload = {key: persisted[key] for key in persisted if key != "schema"}
    card = ProbabilityCalibrationCard.from_payload_mapping(
        payload, digest, str(paths.card)
    )
    if (card.model_id, card.lane_id, card.fold_id, card.candidate_id) != (
        model_id,
        lane_id,
        fold_id,
        candidate_id,
    ):
        raise ValueError("probability calibration card identity differs")
    policy = spec.probability_calibration_policy
    pool = policy.pools[policy.fold_keys[fold_id]]
    if (
        card.calibration_key,
        card.target_year,
        card.training_start,
        card.training_end,
    ) != (
        policy.fold_keys[fold_id],
        pool.target_year,
        pool.training_start,
        pool.training_end,
    ):
        raise ValueError("probability calibration card pool differs")
    if (
        card.run_spec_sha256 != run_spec_sha256(spec)
        or card.prediction_contract_sha256 != spec.prediction_contract_sha256
    ):
        raise ValueError("probability calibration RunSpec binding differs")
    if canonical_json_bytes(card.as_mapping()["fit_spec"]) != canonical_json_bytes(
        policy.fit_spec.as_mapping()
    ):
        raise ValueError("probability calibration fit spec differs")
    if str(_canonical_absolute(card.calibration_rows_path, "calibration rows")) != str(
        paths.rows
    ):
        raise ValueError("calibration rows path differs")
    expected_weight = (
        _repository_root() / spec.baseline.card.path
        if model_id == "v5.1"
        else _canonical_absolute(card.weight_authority_path, "weight authority")
    )
    if str(_canonical_absolute(card.weight_authority_path, "weight authority")) != str(
        expected_weight
    ):
        raise ValueError("weight authority path differs")
    if _missing(expected_weight):
        raise CalibrationAuthorityBlockedError()
    if (
        sha256(read_regular_bytes(expected_weight, "weight authority")).hexdigest()
        != card.weight_authority_sha256
    ):
        raise ValueError("weight authority digest differs")
    evidence_path = _canonical_absolute(
        card.game_evidence_inventory_path, "game evidence inventory"
    )
    if _missing(evidence_path):
        raise CalibrationAuthorityBlockedError()
    if (
        sha256(read_regular_bytes(evidence_path, "game evidence inventory")).hexdigest()
        != card.game_evidence_inventory_sha256
    ):
        raise ValueError("game evidence inventory digest differs")
    source_gate_path = spec.output_path("source_gate", "source_gate_receipt.json")
    if _missing(source_gate_path):
        raise CalibrationAuthorityBlockedError()
    if (
        sha256(read_regular_bytes(source_gate_path, "source gate receipt")).hexdigest()
        != card.source_gate_sha256
    ):
        raise ValueError("source gate digest differs")
    rows_raw = read_regular_bytes(paths.rows, "calibration rows")
    if sha256(rows_raw).hexdigest() != card.calibration_rows_sha256:
        raise ValueError("calibration rows digest differs")
    _sidecar(
        paths.rows_sidecar, card.calibration_rows_sha256, "calibration rows sidecar"
    )
    rows = _rows(rows_raw, card, spec)
    fit = policy.fit_spec
    alpha, beta = fit_logistic(
        (float(row["edge"]) / fit.edge_divisor for row in rows),
        (int(row["home_win"]) for row in rows),
        iterations=fit.iterations,
        l2=fit.l2,
        tolerance=fit.tolerance,
    )
    if not (math.isfinite(alpha) and math.isfinite(beta) and beta > 0):
        raise ValueError("probability calibration fit differs")
    if canonical_json_bytes(
        {"alpha": card.alpha, "beta": card.beta}
    ) != canonical_json_bytes({"alpha": alpha, "beta": beta}):
        raise ValueError("probability calibration coefficients differ")
    return card


def load_probability_calibration_card(
    spec: ModelImprovementRunSpec,
    model_id: str,
    lane_id: str,
    fold_id: str,
    candidate_id: str | None,
) -> ProbabilityCalibrationCard:
    """V1 persists only for inspection compatibility; it is not executable."""
    raise ValueError("ProbabilityCalibrationCard V1 is inspection-only")


def require_probability_calibration_card(
    spec: ModelImprovementRunSpec,
    model_id: str,
    lane_id: str,
    fold_id: str,
    candidate_id: str | None,
) -> ProbabilityCalibrationCard:
    raise ValueError("ProbabilityCalibrationCard V1 is inspection-only")


@dataclass(frozen=True, slots=True)
class CalibrationPathsV2:
    rows: Path
    rows_sidecar: Path
    card: Path
    card_sidecar: Path


def calibration_paths_v2(
    spec: ModelImprovementRunSpec,
    identity: tuple[str, str, str] | tuple[str, str, str, object],
) -> CalibrationPathsV2:
    """The sole baseline V2 calibration output-path constructor."""
    if not isinstance(identity, tuple) or len(identity) not in {3, 4}:
        raise ValueError("V2 calibration identity differs")
    model, lane, fold = identity[:3]
    if (
        model != "v5.1"
        or lane != "baseline"
        or fold not in spec.probability_calibration_policy.fold_keys
    ):
        # Crucially this occurs before any output path/read attempt.
        raise WeightSelectionAuthorityBlockedError()
    if len(identity) == 4 and identity[3] is not None:
        raise WeightSelectionAuthorityBlockedError()
    policy = spec.probability_calibration_policy
    rows = spec.output_path(
        "calibration", policy.baseline_rows_relative_template.format(fold_id=fold)
    )
    card = spec.output_path(
        "calibration", policy.baseline_card_relative_template.format(fold_id=fold)
    )
    return CalibrationPathsV2(
        rows,
        rows.with_suffix(rows.suffix + ".sha256"),
        card,
        card.with_suffix(card.suffix + ".sha256"),
    )


def _missing_v2(path: Path) -> bool:
    return _missing(path)


def _source_authority_digest(spec: ModelImprovementRunSpec) -> tuple[Path, str]:
    activation = spec.verified_activation
    if activation is None:
        raise ValueError("calibration requires verified activation")
    root = (
        Path(activation.declaration_path).parents[2]
        / spec.source_authority.authority_root
    )
    authority = spec.source_authority
    preimage = {
        "authority_root": str(root),
        "source_run_spec": {
            "path": str(root / authority.source_run_spec.path),
            "sha256": authority.source_run_spec.sha256,
        },
        "acquisition_contract": {
            "path": str(root / authority.acquisition_contract.path),
            "sha256": authority.acquisition_contract.sha256,
        },
        "inventory": {
            "path": str(root / authority.inventory.path),
            "sha256": authority.inventory.sha256,
        },
        "terminal_checkpoint": {
            "path": str(root / authority.terminal_checkpoint.path),
            "sha256": authority.terminal_checkpoint.sha256,
        },
    }
    return root, sha256(canonical_json_bytes(preimage)).hexdigest()


def _iter_verified_calibration_games_v1(
    verified_inventory: object,
) -> tuple[Mapping[str, object], ...]:
    payload = getattr(verified_inventory, "payload", None)
    if (
        not isinstance(payload, Mapping)
        or not isinstance(payload.get("games"), list)
        or not payload["games"]
    ):
        raise ValueError("verified game evidence differs")
    result = []
    previous = 0
    required = {
        "game_pk",
        "official_date",
        "scheduled_start_utc",
        "home_team_id",
        "away_team_id",
        "winner_team_id",
        "home_score",
        "away_score",
        "played",
        "terminal_status",
        "verification_status",
        "source_game_sha256",
    }
    for game in payload["games"]:
        if not isinstance(game, Mapping) or not required <= set(game):
            raise ValueError("verified game evidence differs")
        pk = game["game_pk"]
        if type(pk) is not int or pk <= previous:
            raise ValueError("verified game ordering differs")
        previous = pk
        if not isinstance(game["official_date"], str) or not isinstance(
            game["scheduled_start_utc"], str
        ):
            raise ValueError("verified game evidence differs")
        result.append({key: game[key] for key in required})
    return tuple(result)


def _derive_rows_v2(
    spec: ModelImprovementRunSpec,
    receipt: CalibrationFeatureReceiptV1,
    source_gate: SourceGateReceipt,
) -> tuple[list[dict[str, object]], object]:
    bound = load_bound_source_gate_receipt(spec, source_gate)
    root, authority_digest = _source_authority_digest(spec)
    inv_raw = read_regular_bytes(
        root / spec.source_authority.inventory.path, "calibration source inventory"
    )
    if sha256(inv_raw).hexdigest() != spec.source_authority.inventory.sha256:
        raise ValueError("calibration source inventory digest differs")
    inventory = parse_inventory(inv_raw)
    game_binding = inventory.game_evidence_inventory
    verified = load_verified_game_evidence_inventory(
        root,
        read_only_progress=True,
        run_spec_path=root / spec.source_authority.source_run_spec.path,
        acquisition_contract_path=root
        / spec.source_authority.acquisition_contract.path,
    )
    if (
        getattr(verified, "sha256", None) != game_binding.sha256
        or str(getattr(verified, "path", "")).removeprefix(str(root) + "/")
        != game_binding.path
    ):
        raise ValueError("calibration game inventory binding differs")
    games = {
        game["game_pk"]: game for game in _iter_verified_calibration_games_v1(verified)
    }
    weights = resolve_prediction_weight_authority(spec, "v5.1", None)
    if weights.model_id != "v5.1":
        raise ValueError("calibration weight authority differs")
    pool = spec.probability_calibration_policy.pools[receipt.payload["calibration_key"]]
    if (
        receipt.payload["source_authority_sha256"] != authority_digest
        or receipt.payload["source_inventory_sha256"] != sha256(inv_raw).hexdigest()
    ):
        raise ValueError("calibration receipt source binding differs")
    rows = []
    for feature in receipt.rows:
        row = feature.payload
        game = games.get(row["game_pk"])
        if game is None:
            raise ValueError("calibration feature game differs")
        if (
            row["source_authority_sha256"] != authority_digest
            or row["official_date"] != game["official_date"]
            or row["home_team_id"] != game["home_team_id"]
            or row["away_team_id"] != game["away_team_id"]
        ):
            raise ValueError("calibration feature source differs")
        if (
            not pool.training_start <= row["official_date"] <= pool.training_end
            or row["target_year"] != pool.target_year
        ):
            raise ValueError("calibration feature pool differs")
        if (
            game["played"] is not True
            or game["terminal_status"] not in {"Final", "Completed Early"}
            or game["verification_status"] != "VERIFIED"
        ):
            raise ValueError("calibration game is not final")
        home, away = game["home_score"], game["away_score"]
        if (
            isinstance(home, bool)
            or isinstance(away, bool)
            or not isinstance(home, (int, float))
            or not isinstance(away, (int, float))
            or home < 0
            or away < 0
            or home == away
        ):
            raise ValueError("calibration game score differs")
        home_win = int(home > away)
        if game["winner_team_id"] != (
            game["home_team_id"] if home_win else game["away_team_id"]
        ):
            raise ValueError("calibration winner differs")
        edge = math.fsum(
            weights.component_weights_pp[c]
            / 100.0
            * (row["component_scores"]["home"][c] - row["component_scores"]["away"][c])
            for c in COMPONENTS
        )
        rows.append(
            {
                "game_pk": row["game_pk"],
                "official_date": row["official_date"],
                "target_year": pool.target_year,
                "edge": edge,
                "home_win": home_win,
                "feature_row_sha256": row["feature_row_sha256"],
                "source_game_sha256": game["source_game_sha256"],
            }
        )
    return rows, (bound, inventory, verified, weights, authority_digest)


_V2_FIELDS = frozenset({"schema"}) | (
    frozenset(ProbabilityCalibrationCardV2.__dataclass_fields__)
    - {"artifact_sha256", "path"}
)
_V2_ROW_FIELDS = frozenset(
    {
        "game_pk",
        "official_date",
        "target_year",
        "edge",
        "home_win",
        "feature_row_sha256",
        "source_game_sha256",
    }
)


def _load_v2_once(
    spec: ModelImprovementRunSpec, supplied_gate: SourceGateReceipt, fold_id: str
) -> ProbabilityCalibrationCardV2:
    paths = calibration_paths_v2(spec, ("v5.1", "baseline", fold_id))
    receipt_pin = spec.probability_calibration_policy.calibration_feature_receipts[
        spec.probability_calibration_policy.fold_keys[fold_id]
    ]
    # Only genuine absence of a primary blocks. A primary without an exact
    # sidecar/sibling is evidence corruption, never a recoverable absence.
    for path in (Path(receipt_pin.path), paths.card, paths.rows):
        if _missing_v2(path):
            raise CalibrationAuthorityBlockedError()
    receipt_sidecar = Path(receipt_pin.path).with_suffix(
        Path(receipt_pin.path).suffix + ".sha256"
    )
    receipt_rows = Path(receipt_pin.path).with_name("calibration_feature_rows.jsonl")
    receipt_rows_sidecar = receipt_rows.with_suffix(receipt_rows.suffix + ".sha256")
    for path in (receipt_sidecar, receipt_rows, receipt_rows_sidecar):
        if _missing_v2(path):
            raise ValueError("calibration authority sidecar is missing")
    receipt = load_calibration_feature_receipt(spec, fold_id)
    # Authenticate all source/feature/weight inputs before opening a V2 output.
    try:
        expected, chain = _derive_rows_v2(spec, receipt, supplied_gate)
    except FileNotFoundError as exc:
        raise CalibrationAuthorityBlockedError() from exc
    for path in (paths.card_sidecar, paths.rows_sidecar):
        if _missing_v2(path):
            raise ValueError("calibration authority sidecar is missing")
    card_raw = read_regular_bytes(paths.card, "probability calibration V2 card")
    digest = sha256(card_raw).hexdigest()
    _sidecar(paths.card_sidecar, digest, "probability calibration V2 card sidecar")
    try:
        data = json.loads(card_raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("probability calibration V2 card is not JSON") from exc
    if (
        not isinstance(data, Mapping)
        or card_raw != canonical_json_bytes(data)
        or set(data) != _V2_FIELDS
        or data.get("schema") != "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V2"
    ):
        raise ValueError("probability calibration V2 card differs")
    card = ProbabilityCalibrationCardV2.from_payload_mapping(
        {k: v for k, v in data.items() if k != "schema"}, digest, str(paths.card)
    )
    policy = spec.probability_calibration_policy
    pool = policy.pools[policy.fold_keys[fold_id]]
    if (
        card.model_id,
        card.lane_id,
        card.fold_id,
        card.calibration_key,
        card.target_year,
        card.training_start,
        card.training_end,
    ) != (
        "v5.1",
        "baseline",
        fold_id,
        policy.fold_keys[fold_id],
        pool.target_year,
        pool.training_start,
        pool.training_end,
    ):
        raise ValueError("probability calibration V2 identity differs")
    if (
        card.run_spec_sha256 != run_spec_sha256(spec)
        or card.prediction_contract_sha256 != spec.prediction_contract_sha256
        or card.calibration_feature_receipt_path != receipt.path
        or card.calibration_feature_receipt_sha256 != receipt.artifact_sha256
    ):
        raise ValueError("probability calibration V2 binding differs")
    bound_gate, inventory, _, weights, _ = chain
    if (
        card.source_gate_sha256 != bound_gate.artifact_sha256
        or card.weight_authority_path != str(_repository_root() / weights.path)
        or card.weight_authority_sha256 != weights.sha256
        or card.game_evidence_inventory_relative_path
        != inventory.game_evidence_inventory.path
        or card.game_evidence_inventory_sha256
        != inventory.game_evidence_inventory.sha256
    ):
        raise ValueError("probability calibration V2 typed authority differs")
    rows_raw = read_regular_bytes(paths.rows, "calibration rows")
    if sha256(rows_raw).hexdigest() != card.calibration_rows_sha256:
        raise ValueError("calibration rows digest differs")
    _sidecar(
        paths.rows_sidecar, card.calibration_rows_sha256, "calibration rows sidecar"
    )
    actual = []
    for line in rows_raw.splitlines():
        item = json.loads(line)
        if (
            not isinstance(item, Mapping)
            or set(item) != _V2_ROW_FIELDS
            or canonical_json_bytes(item).rstrip(b"\n") != line
        ):
            raise ValueError("calibration V2 rows differ")
        actual.append(dict(item))
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise ValueError("calibration V2 rows recomputation differs")
    if (
        sha256(canonical_json_bytes([row["game_pk"] for row in actual])).hexdigest()
        != card.calibration_population_sha256
    ):
        raise ValueError("calibration V2 population differs")
    alpha, beta = fit_logistic(
        (float(row["edge"]) / policy.fit_spec.edge_divisor for row in actual),
        (int(row["home_win"]) for row in actual),
        iterations=policy.fit_spec.iterations,
        l2=policy.fit_spec.l2,
        tolerance=policy.fit_spec.tolerance,
    )
    if canonical_json_bytes({"alpha": alpha, "beta": beta}) != canonical_json_bytes(
        {"alpha": card.alpha, "beta": card.beta}
    ):
        raise ValueError("calibration V2 fit differs")
    # Full second authority read detects source/receipt/gate drift before return.
    try:
        expected_after, chain_after = _derive_rows_v2(
            spec, load_calibration_feature_receipt(spec, fold_id), supplied_gate
        )
    except FileNotFoundError as exc:
        raise CalibrationAuthorityBlockedError() from exc
    if (
        canonical_json_bytes(expected_after) != canonical_json_bytes(expected)
        or chain_after != chain
    ):
        raise ValueError("calibration authority changed while reading")
    return card


def load_probability_calibration_card_v2(
    spec: ModelImprovementRunSpec, supplied_gate: SourceGateReceipt, fold_id: str
) -> ProbabilityCalibrationCardV2:
    return _load_v2_once(spec, supplied_gate, fold_id)


def require_probability_calibration_card_v2(
    spec: ModelImprovementRunSpec, supplied_gate: SourceGateReceipt, fold_id: str
) -> ProbabilityCalibrationCardV2:
    return load_probability_calibration_card_v2(spec, supplied_gate, fold_id)


__all__ = [
    "CalibrationAuthorityBlockedError",
    "WeightSelectionAuthorityBlockedError",
    "CalibrationPaths",
    "CalibrationPathsV2",
    "ProbabilityCalibrationCard",
    "ProbabilityCalibrationCardV2",
    "calibration_paths",
    "calibration_paths_v2",
    "load_probability_calibration_card",
    "require_probability_calibration_card",
    "load_probability_calibration_card_v2",
    "require_probability_calibration_card_v2",
]
