"""Baseline-only, source-authenticated Task6B outcome receipts.

This module is deliberately the first and only V2 boundary which observes
verified game outcomes.  It accepts no raw labels, outcome paths, candidate
identity, evaluator, or Task8B input.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Sequence

from mlb_backtest.v52_game_evidence_inventory import load_verified_game_evidence_inventory
from mlb_backtest.v52_snapshot_store import parse_inventory

from .baseline_replay_v2 import BaselineReplayReceiptV2, load_baseline_replay_v2
from .canonical import canonical_json_bytes, read_regular_bytes, write_immutable_bytes
from .contracts import PredictionSealV2, _require_exact_mapping, _require_sha256
from .oof_ledger_v2 import load_prediction_rows_v2, load_prediction_seal_v2
from .run_spec import ModelImprovementRunSpec, run_spec_sha256, task8_target_year
from .sealed_features import FeatureSeal, load_feature_seal
from .source_gate import SourceGateReceipt, load_bound_source_gate_receipt


_SCHEMA = "MODEL_IMPROVEMENT_OUTCOME_RECEIPT_V1"
_RECEIPT_FIELDS = frozenset({
    "schema", "ledger_id", "fold_id", "model_id", "run_spec_sha256",
    "source_gate_sha256", "source_inventory_sha256",
    "game_evidence_inventory_relative_path", "game_evidence_inventory_sha256",
    "feature_seal_sha256", "baseline_replay_sha256", "prediction_seal_sha256",
    "prediction_rows_sha256", "prediction_contract_sha256",
    "scoring_model_card_sha256", "population_sha256", "outcomes_sha256",
})


def _require_date_year(value: object, year: int, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} differs")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} differs") from exc
    if parsed.year != year:
        raise ValueError(f"{label} differs")
    return value


def _require_utc(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z") or "T" not in value:
        raise ValueError(f"{label} differs")
    try:
        from datetime import datetime
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} differs") from exc
    return value


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    game_pk: int
    official_date: str
    target_year: int
    scheduled_start_utc: str
    home_win: bool
    source_game_sha256: str

    def __post_init__(self) -> None:
        if type(self.game_pk) is not int or self.game_pk <= 0:
            raise ValueError("outcome game_pk differs")
        if type(self.target_year) is not int:
            raise ValueError("outcome target_year differs")
        object.__setattr__(self, "official_date", _require_date_year(self.official_date, self.target_year, "outcome official_date"))
        object.__setattr__(self, "scheduled_start_utc", _require_utc(self.scheduled_start_utc, "outcome scheduled_start_utc"))
        if type(self.home_win) is not bool:
            raise ValueError("outcome home_win differs")
        object.__setattr__(self, "source_game_sha256", _require_sha256(self.source_game_sha256, "source_game_sha256"))

    def as_mapping(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    @classmethod
    def from_payload_mapping(cls, payload: Mapping[str, object]) -> "OutcomeRow":
        return cls(**dict(_require_exact_mapping(payload, frozenset(cls.__dataclass_fields__), "outcome row")))


@dataclass(frozen=True, slots=True)
class OutcomeReceipt:
    schema: str
    ledger_id: str
    fold_id: str
    model_id: str
    run_spec_sha256: str
    source_gate_sha256: str
    source_inventory_sha256: str
    game_evidence_inventory_relative_path: str
    game_evidence_inventory_sha256: str
    feature_seal_sha256: str
    baseline_replay_sha256: str
    prediction_seal_sha256: str
    prediction_rows_sha256: str
    prediction_contract_sha256: str
    scoring_model_card_sha256: str
    population_sha256: str
    outcomes_sha256: str
    artifact_sha256: str
    path: str

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA or self.model_id != "v5.1":
            raise ValueError("outcome receipt identity differs")
        try:
            task8_target_year(self.fold_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("outcome receipt fold differs") from exc
        if self.ledger_id != f"baseline/v5.1/{self.fold_id}":
            raise ValueError("outcome receipt ledger differs")
        for field in (
            "run_spec_sha256", "source_gate_sha256", "source_inventory_sha256",
            "game_evidence_inventory_sha256", "feature_seal_sha256",
            "baseline_replay_sha256", "prediction_seal_sha256",
            "prediction_rows_sha256", "prediction_contract_sha256",
            "scoring_model_card_sha256", "population_sha256", "outcomes_sha256",
            "artifact_sha256",
        ):
            object.__setattr__(self, field, _require_sha256(getattr(self, field), field))
        relative = self.game_evidence_inventory_relative_path
        if not isinstance(relative, str) or not relative or relative.startswith("/") or any(part in {"", ".", ".."} for part in Path(relative).parts):
            raise ValueError("outcome game inventory path differs")
        if not isinstance(self.path, str) or not Path(self.path).is_absolute():
            raise ValueError("outcome receipt path differs")

    def as_mapping(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__ if field not in {"artifact_sha256", "path"}}

    @classmethod
    def from_payload_mapping(cls, payload: Mapping[str, object], artifact_sha256: str, path: str) -> "OutcomeReceipt":
        return cls(**dict(_require_exact_mapping(payload, _RECEIPT_FIELDS, "outcome receipt")), artifact_sha256=artifact_sha256, path=path)


@dataclass(frozen=True, slots=True)
class OutcomePaths:
    rows: Path
    rows_sidecar: Path
    receipt: Path
    receipt_sidecar: Path


def _paths(spec: ModelImprovementRunSpec, fold_id: str) -> OutcomePaths:
    try:
        task8_target_year(fold_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("outcome fold differs") from exc
    base = f"baseline/v5.1/{fold_id}/v2"
    rows = spec.output_path("oof", f"{base}/outcome_rows.jsonl")
    receipt = spec.output_path("oof", f"{base}/outcome_receipt.json")
    return OutcomePaths(rows, rows.with_suffix(rows.suffix + ".sha256"), receipt, receipt.with_suffix(receipt.suffix + ".sha256"))


def _canonical_absolute(path: Path | str, label: str) -> Path:
    try:
        raw = os.fspath(path)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} path differs") from exc
    candidate = Path(raw) if isinstance(raw, str) else None
    if not isinstance(raw, str) or candidate is None or not candidate.is_absolute() or raw != str(candidate) or raw != os.path.normpath(raw) or "//" in raw:
        raise ValueError(f"{label} path differs")
    return candidate


def _sidecar_payload(digest: str) -> dict[str, str]:
    return {"artifact_sha256": _require_sha256(digest, "artifact_sha256")}


def _load_sidecar(path: Path, digest: str, label: str) -> None:
    raw = read_regular_bytes(path, label)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if raw != canonical_json_bytes(payload) or payload != _sidecar_payload(digest):
        raise ValueError(f"{label} differs")


def _existing_regular_bytes(path: Path, label: str) -> bytes | None:
    try:
        result = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"unsafe {label}") from exc
    if not stat.S_ISREG(result.st_mode):
        raise ValueError(f"unsafe {label}")
    return read_regular_bytes(path, label)


def _preflight(primary: Path, contents: bytes, sidecar: Path, label: str) -> None:
    existing = _existing_regular_bytes(primary, label)
    existing_sidecar = _existing_regular_bytes(sidecar, f"{label} sidecar")
    digest = sha256(contents).hexdigest()
    if existing is not None:
        if existing != contents:
            raise ValueError(f"immutable {label} differs")
        if existing_sidecar is None:
            raise ValueError(f"{label} primary requires sidecar")
        _load_sidecar(sidecar, digest, f"{label} sidecar")
    elif existing_sidecar is not None:
        _load_sidecar(sidecar, digest, f"{label} sidecar")


def _source_root(spec: ModelImprovementRunSpec) -> Path:
    if not isinstance(spec, ModelImprovementRunSpec) or spec.verified_activation is None:
        raise ValueError("outcomes require verified ModelImprovementRunSpec")
    return Path(spec.verified_activation.declaration_path).parents[2] / spec.source_authority.authority_root


def _validate_supplied_types(spec: object, source_gate: object, feature_seal: object, baseline_replay: object, prediction: object) -> tuple[ModelImprovementRunSpec, FeatureSeal, BaselineReplayReceiptV2, PredictionSealV2, str]:
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("outcomes require ModelImprovementRunSpec")
    if not isinstance(source_gate, SourceGateReceipt):
        raise ValueError("outcomes require SourceGateReceipt")
    if not isinstance(feature_seal, FeatureSeal) or feature_seal.feature_set_id != "common":
        raise ValueError("outcomes require Common FeatureSeal")
    if not isinstance(baseline_replay, BaselineReplayReceiptV2):
        raise ValueError("outcomes require BaselineReplayReceiptV2")
    if not isinstance(prediction, PredictionSealV2):
        raise ValueError("outcomes require PredictionSealV2")
    try:
        year = task8_target_year(feature_seal.fold_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("outcome feature fold differs") from exc
    fold_id = feature_seal.fold_id
    if (
        prediction.fold_id != fold_id or prediction.ledger_id != f"baseline/v5.1/{fold_id}"
        or prediction.model_id != "v5.1" or baseline_replay.fold_id != fold_id
        or baseline_replay.ledger_id != prediction.ledger_id or year != task8_target_year(prediction.fold_id)
    ):
        raise ValueError("outcome baseline identity differs")
    return spec, feature_seal, baseline_replay, prediction, fold_id


def _authenticate_gate(spec: ModelImprovementRunSpec, supplied: SourceGateReceipt) -> SourceGateReceipt:
    loaded = load_bound_source_gate_receipt(spec)
    expected_path = spec.output_path("source_gate", "source_gate_receipt.json")
    if (
        loaded != supplied or loaded.path != str(expected_path)
        or loaded.run_spec_sha256 != run_spec_sha256(spec)
        or loaded.inventory_sha256 != spec.source_authority.inventory.sha256
    ):
        raise ValueError("outcome source gate binding differs")
    return loaded


def _authenticated_games(spec: ModelImprovementRunSpec, gate: SourceGateReceipt) -> tuple[str, str, Mapping[int, Mapping[str, object]]]:
    root = _source_root(spec)
    inventory_path = root / spec.source_authority.inventory.path
    inventory_bytes = read_regular_bytes(inventory_path, "outcome source inventory")
    inventory_digest = sha256(inventory_bytes).hexdigest()
    if inventory_digest != spec.source_authority.inventory.sha256 or inventory_digest != gate.inventory_sha256:
        raise ValueError("outcome source inventory binding differs")
    inventory = parse_inventory(inventory_bytes)
    binding = inventory.game_evidence_inventory
    relative = binding.path
    if not isinstance(relative, str) or binding.sha256 != _require_sha256(binding.sha256, "game_evidence_inventory_sha256"):
        raise ValueError("outcome game inventory binding differs")
    read_regular_bytes(root / relative, "outcome game evidence inventory")
    verified = load_verified_game_evidence_inventory(
        root, read_only_progress=True,
        run_spec_path=root / spec.source_authority.source_run_spec.path,
        acquisition_contract_path=root / spec.source_authority.acquisition_contract.path,
    )
    if getattr(verified, "sha256", None) != binding.sha256 or getattr(verified, "path", None) != root / relative:
        raise ValueError("outcome game inventory binding differs")
    payload = getattr(verified, "payload", None)
    games = payload.get("games") if isinstance(payload, Mapping) else None
    if not isinstance(games, list):
        raise ValueError("outcome verified games differ")
    indexed: dict[int, Mapping[str, object]] = {}
    for game in games:
        if not isinstance(game, Mapping) or type(game.get("game_pk")) is not int or game["game_pk"] <= 0 or game["game_pk"] in indexed:
            raise ValueError("outcome verified games differ")
        indexed[game["game_pk"]] = game
    return relative, binding.sha256, indexed


def _validate_game(prediction: object, game: Mapping[str, object], fold_id: str) -> OutcomeRow:
    required = {"game_pk", "official_date", "scheduled_start_utc", "season", "home_team_id", "away_team_id", "winner_team_id", "home_score", "away_score", "played", "terminal_status", "verification_status", "source_game_sha256"}
    if not required <= set(game):
        raise ValueError("outcome game fields differ")
    year = task8_target_year(fold_id)
    if (
        game["game_pk"] != prediction.game_pk or game["official_date"] != prediction.official_date
        or game["scheduled_start_utc"] != prediction.scheduled_start_utc
        or game["season"] != year or prediction.target_year != year
        or date.fromisoformat(prediction.official_date).year != year
        or game["played"] is not True or game["terminal_status"] not in {"Final", "Completed Early"}
        or game["verification_status"] != "VERIFIED"
    ):
        raise ValueError("outcome game identity differs")
    home, away = game["home_score"], game["away_score"]
    if type(home) is not int or type(away) is not int or home < 0 or away < 0 or home == away:
        raise ValueError("outcome game score differs")
    home_win = home > away
    if game["winner_team_id"] != (game["home_team_id"] if home_win else game["away_team_id"]):
        raise ValueError("outcome game winner differs")
    return OutcomeRow(prediction.game_pk, prediction.official_date, year, prediction.scheduled_start_utc, home_win, game["source_game_sha256"])


def _rows_bytes(rows: Sequence[OutcomeRow]) -> bytes:
    return b"".join(canonical_json_bytes(row.as_mapping()) for row in rows)


def _population_digest(rows: Sequence[object]) -> str:
    return sha256(canonical_json_bytes([{"game_pk": row.game_pk, "official_date": row.official_date, "target_year": row.target_year} for row in rows])).hexdigest()


def _materialize(spec: ModelImprovementRunSpec, source_gate: SourceGateReceipt, feature_seal: FeatureSeal, baseline_replay: BaselineReplayReceiptV2, prediction: PredictionSealV2) -> tuple[SourceGateReceipt, FeatureSeal, BaselineReplayReceiptV2, PredictionSealV2, tuple[OutcomeRow, ...], str, str]:
    fold_id = feature_seal.fold_id
    gate = _authenticate_gate(spec, source_gate)
    feature_path = spec.output_path("features", f"common/{fold_id}/feature_seal.json")
    feature = load_feature_seal(feature_path, feature_seal.artifact_sha256)
    if feature != feature_seal or feature.run_spec_sha256 != run_spec_sha256(spec) or feature.source_gate_sha256 != gate.artifact_sha256:
        raise ValueError("outcome feature seal binding differs")
    replay = load_baseline_replay_v2(spec, gate, feature, baseline_replay.path, baseline_replay.artifact_sha256)
    if replay != baseline_replay or replay.feature_seal_sha256 != feature.artifact_sha256:
        raise ValueError("outcome baseline replay binding differs")
    seal = load_prediction_seal_v2(spec, gate, feature, None, replay.prediction_seal_path, replay.prediction_seal_sha256)
    if seal != prediction or seal.artifact_sha256 != replay.prediction_seal_sha256:
        raise ValueError("outcome prediction seal binding differs")
    predictions = load_prediction_rows_v2(spec, gate, feature, None, seal)
    if not predictions or seal.rows_sha256 != replay.prediction_rows_sha256 or _population_digest(predictions) != seal.population_sha256:
        raise ValueError("outcome prediction population differs")
    relative, evidence_digest, games = _authenticated_games(spec, gate)
    outcomes = tuple(_validate_game(prediction_row, games.get(prediction_row.game_pk, {}), fold_id) for prediction_row in predictions)
    if len({row.game_pk for row in outcomes}) != len(outcomes) or _population_digest(outcomes) != seal.population_sha256:
        raise ValueError("outcome population differs")
    return gate, feature, replay, seal, outcomes, relative, evidence_digest


def _receipt_path_from_argument(spec: ModelImprovementRunSpec, path: Path | str) -> tuple[Path, str]:
    provided = _canonical_absolute(path, "outcome receipt")
    root = _paths(spec, "H22").receipt.parents[4]
    try:
        relative = provided.relative_to(root)
    except ValueError as exc:
        raise ValueError("outcome receipt path differs") from exc
    parts = relative.parts
    if len(parts) != 5 or parts[:2] != ("baseline", "v5.1") or parts[3:] != ("v2", "outcome_receipt.json"):
        raise ValueError("outcome receipt path differs")
    fold_id = parts[2]
    expected = _paths(spec, fold_id).receipt
    if provided != expected:
        raise ValueError("outcome receipt path differs")
    return expected, fold_id


def _load_rows_bytes(path: Path, expected: str) -> tuple[OutcomeRow, ...]:
    raw = read_regular_bytes(path, "outcome rows")
    if sha256(raw).hexdigest() != expected or not raw or not raw.endswith(b"\n") or b"\r" in raw or b"\n\n" in raw:
        raise ValueError("outcome rows digest or canonical JSONL differs")
    rows: list[OutcomeRow] = []
    for line in raw.splitlines(keepends=True):
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("outcome rows are not JSONL") from exc
        if not line.endswith(b"\n") or not isinstance(payload, Mapping) or canonical_json_bytes(payload) != line:
            raise ValueError("outcome rows are not canonical JSONL")
        rows.append(OutcomeRow.from_payload_mapping(payload))
    return tuple(rows)


def seal_outcome_receipt(spec: ModelImprovementRunSpec, source_gate: SourceGateReceipt, feature_seal: FeatureSeal, baseline_replay: BaselineReplayReceiptV2, baseline_prediction_seal: PredictionSealV2) -> OutcomeReceipt:
    """Derive and publish one immutable V2 baseline outcome receipt."""
    spec, feature_seal, baseline_replay, baseline_prediction_seal, fold_id = _validate_supplied_types(spec, source_gate, feature_seal, baseline_replay, baseline_prediction_seal)
    first = _materialize(spec, source_gate, feature_seal, baseline_replay, baseline_prediction_seal)
    second = _materialize(spec, first[0], first[1], first[2], first[3])
    if first != second:
        raise ValueError("outcome independent materialization differs")
    gate, feature, replay, seal, outcomes, relative, evidence_digest = first
    paths = _paths(spec, fold_id)
    row_bytes = _rows_bytes(outcomes)
    rows_digest = sha256(row_bytes).hexdigest()
    provisional = OutcomeReceipt(
        _SCHEMA, seal.ledger_id, fold_id, "v5.1", run_spec_sha256(spec), gate.artifact_sha256,
        gate.inventory_sha256, relative, evidence_digest, feature.artifact_sha256,
        replay.artifact_sha256, seal.artifact_sha256, seal.rows_sha256,
        seal.prediction_contract_sha256, seal.scoring_model_card_sha256,
        seal.population_sha256, rows_digest, "0" * 64, str(paths.receipt),
    )
    receipt_bytes = canonical_json_bytes(provisional.as_mapping())
    receipt_digest = sha256(receipt_bytes).hexdigest()
    _preflight(paths.rows, row_bytes, paths.rows_sidecar, "outcome rows")
    _preflight(paths.receipt, receipt_bytes, paths.receipt_sidecar, "outcome receipt")
    if _existing_regular_bytes(paths.receipt, "outcome receipt") is not None and _existing_regular_bytes(paths.rows, "outcome rows") is None:
        raise ValueError("outcome receipt primary requires rows primary")
    write_immutable_bytes(paths.rows_sidecar, canonical_json_bytes(_sidecar_payload(rows_digest)))
    write_immutable_bytes(paths.receipt_sidecar, canonical_json_bytes(_sidecar_payload(receipt_digest)))
    write_immutable_bytes(paths.rows, row_bytes)
    final = _materialize(spec, source_gate, feature_seal, baseline_replay, baseline_prediction_seal)
    if final != first:
        raise ValueError("outcome authority changed before commit")
    write_immutable_bytes(paths.receipt, receipt_bytes)
    loaded = load_outcome_receipt(spec, gate, feature, replay, paths.receipt, receipt_digest)
    if loaded.as_mapping() != provisional.as_mapping() or load_outcome_rows(spec, gate, feature, replay, loaded) != outcomes:
        raise ValueError("outcome receipt reread differs")
    return loaded


def load_outcome_receipt(spec: ModelImprovementRunSpec, source_gate: SourceGateReceipt, feature_seal: FeatureSeal, baseline_replay: BaselineReplayReceiptV2, path: Path | str, expected_sha256: str) -> OutcomeReceipt:
    """Reload a receipt only after reproducing the sealed source-derived rows."""
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("outcomes require ModelImprovementRunSpec")
    expected = _require_sha256(expected_sha256, "expected outcome receipt sha256")
    receipt_path, fold_id = _receipt_path_from_argument(spec, path)
    if (
        not isinstance(source_gate, SourceGateReceipt)
        or not isinstance(feature_seal, FeatureSeal)
        or feature_seal.feature_set_id != "common"
        or not isinstance(baseline_replay, BaselineReplayReceiptV2)
        or feature_seal.fold_id != fold_id
        or baseline_replay.fold_id != fold_id
        or baseline_replay.ledger_id != f"baseline/v5.1/{fold_id}"
    ):
        raise ValueError("outcome receipt baseline binding differs")
    raw = read_regular_bytes(receipt_path, "outcome receipt")
    actual = sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError("outcome receipt digest differs")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("outcome receipt is not JSON") from exc
    if not isinstance(payload, Mapping) or raw != canonical_json_bytes(payload):
        raise ValueError("outcome receipt is not canonical JSON")
    _load_sidecar(receipt_path.with_suffix(receipt_path.suffix + ".sha256"), actual, "outcome receipt sidecar")
    receipt = OutcomeReceipt.from_payload_mapping(payload, actual, str(receipt_path))
    prediction = load_prediction_seal_v2(spec, source_gate, feature_seal, None, baseline_replay.prediction_seal_path, baseline_replay.prediction_seal_sha256)
    _validate_supplied_types(spec, source_gate, feature_seal, baseline_replay, prediction)
    gate, feature, replay, seal, outcomes, relative, evidence_digest = _materialize(spec, source_gate, feature_seal, baseline_replay, prediction)
    paths = _paths(spec, fold_id)
    rows_bytes = _rows_bytes(outcomes)
    if (
        receipt.ledger_id != seal.ledger_id or receipt.fold_id != fold_id
        or receipt.run_spec_sha256 != run_spec_sha256(spec)
        or receipt.source_gate_sha256 != gate.artifact_sha256
        or receipt.source_inventory_sha256 != gate.inventory_sha256
        or receipt.game_evidence_inventory_relative_path != relative
        or receipt.game_evidence_inventory_sha256 != evidence_digest
        or receipt.feature_seal_sha256 != feature.artifact_sha256
        or receipt.baseline_replay_sha256 != replay.artifact_sha256
        or receipt.prediction_seal_sha256 != seal.artifact_sha256
        or receipt.prediction_rows_sha256 != seal.rows_sha256
        or receipt.prediction_contract_sha256 != seal.prediction_contract_sha256
        or receipt.scoring_model_card_sha256 != seal.scoring_model_card_sha256
        or receipt.population_sha256 != seal.population_sha256
        or receipt.outcomes_sha256 != sha256(rows_bytes).hexdigest()
    ):
        raise ValueError("outcome receipt binding differs")
    _load_sidecar(paths.rows_sidecar, receipt.outcomes_sha256, "outcome rows sidecar")
    if _load_rows_bytes(paths.rows, receipt.outcomes_sha256) != outcomes:
        raise ValueError("outcome rows differ")
    if read_regular_bytes(receipt_path, "outcome receipt") != raw:
        raise ValueError("outcome receipt changed while reading")
    return receipt


def load_outcome_rows(spec: ModelImprovementRunSpec, source_gate: SourceGateReceipt, feature_seal: FeatureSeal, baseline_replay: BaselineReplayReceiptV2, receipt: OutcomeReceipt) -> tuple[OutcomeRow, ...]:
    """Load the sole prediction-bound outcome row stream."""
    if not isinstance(receipt, OutcomeReceipt):
        raise ValueError("outcome rows require OutcomeReceipt")
    loaded = load_outcome_receipt(spec, source_gate, feature_seal, baseline_replay, receipt.path, receipt.artifact_sha256)
    if loaded != receipt:
        raise ValueError("outcome receipt descriptor differs")
    paths = _paths(spec, receipt.fold_id)
    rows = _load_rows_bytes(paths.rows, receipt.outcomes_sha256)
    if _population_digest(rows) != receipt.population_sha256:
        raise ValueError("outcome row population differs")
    return rows


__all__ = ["OutcomeReceipt", "OutcomeRow", "load_outcome_receipt", "load_outcome_rows", "seal_outcome_receipt"]
