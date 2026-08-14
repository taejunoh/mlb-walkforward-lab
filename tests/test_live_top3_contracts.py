import hashlib
import inspect
import json
import unittest
from dataclasses import FrozenInstanceError, asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import get_type_hints

from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validate

import mlb_backtest.live_top3_contracts as contracts
from mlb_backtest.live_top3_contracts import (
    CANDIDATE_STATES,
    MODEL_LABEL,
    ExecutionTimes,
    FinalGameObservation,
    IntegrityError,
    LiveHistoryBundle,
    SourceBinding,
    canonical_json_bytes,
    validate_run_payload,
    validate_target_date,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
HISTORY_ROLES = (
    "HISTORY_SCHEDULE",
    "HISTORY_STATCAST",
    "HISTORY_ERA",
    "HISTORY_VENUE",
)
LIVE_ROLES = {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS", "FINAL_SCHEDULE"}


def canonical_query_identity(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8").removesuffix("\n")


def live_http_observation(**overrides: object) -> contracts.LiveHttpObservation:
    values: dict[str, object] = {
        "requested_url": "https://statsapi.mlb.com/api/v1/schedule?sportId=1",
        "final_url": "https://statsapi.mlb.com/api/v1/schedule?sportId=1",
        "started_at_utc": "2026-07-21T11:59:59Z",
        "response_completed_at_utc": "2026-07-21T12:00:00Z",
        "http_status": 200,
        "content_type": "application/json",
        "etag": '"abc"',
        "last_modified": None,
        "body": b"{}",
    }
    values.update(overrides)
    return contracts.LiveHttpObservation(**values)


def source_binding(
    index: int = 1,
    role: str = "LIVE_SCHEDULE",
    game_pks: list[int] | None = None,
) -> dict[str, object]:
    observation_id = f"obs-{index}"
    requested_url = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
    query_identity: dict[str, object] = {
        "url": requested_url,
        "target_date": "2026-07-21" if role in LIVE_ROLES else "2026-07-20",
    }
    if role in LIVE_ROLES:
        query_identity["observation_id"] = observation_id
    else:
        query_identity["as_of_date"] = "2026-07-20"
    binding = {
        "source": "stats_api",
        "binding_role": role,
        "game_pks": [1] if game_pks is None else game_pks,
        "observation_id": observation_id,
        "query_identity_json": canonical_query_identity(query_identity),
        "requested_url": requested_url,
        "final_url": requested_url,
        "response_completed_at_utc": "2026-07-21T12:00:00Z",
        "durable_at_utc": "2026-07-21T12:00:01Z",
        "http_status": 200,
        "content_type": "application/json",
        "etag": '"abc"',
        "last_modified": None,
        "payload_sha256": SHA_A,
        "byte_length": 12,
        "payload_relative": "data/experimental_daily/live_observations/payload.json",
        "manifest_sha256": SHA_B,
        "receipt_relative": f"audit/experimental_daily/http/obs-{index}.json",
        "receipt_sha256": SHA_C,
    }
    binding["binding_sha256"] = hashlib.sha256(canonical_json_bytes(binding)).hexdigest()
    return binding


def ranked_row(game_pk: int = 1, rank: int = 1) -> dict[str, object]:
    bindings = [
        source_binding(index, role, [] if role.startswith("HISTORY_") or role == "CAREER_BF" else [game_pk])
        for index, role in enumerate(
            (
                "LIVE_SCHEDULE",
                "LIVE_PARTICIPANTS",
                "FINAL_SCHEDULE",
                "HISTORY_SCHEDULE",
                "HISTORY_STATCAST",
                "HISTORY_ERA",
                "HISTORY_VENUE",
                "CAREER_BF",
            ),
            start=1,
        )
    ]
    return {
        "game_pk": game_pk,
        "target_date": "2026-07-21",
        "away_team": "Away",
        "home_team": "Home",
        "predicted_winner": "Home",
        "pick_probability": 0.7,
        "pick_probability_display": "70.0%",
        "state": "PRELIMINARY",
        "rank": rank,
        "data_completeness": 0.9,
        "starter_edge_abs": 0.1,
        "starter_provenance": "PROBABLE_CONSISTENT",
        "lineup_provenance": "OBSERVED_PREGAME_LINEUP",
        "scheduled_start_utc": "2026-07-21T19:00:00Z",
        "observation_boundary_utc": "2026-07-21T12:00:01Z",
        "reason_codes": ["COMPLETE"],
        "model_card_sha256": SHA_B,
        "feature_sha256": SHA_B,
        "source_binding_sha256s": sorted(item["binding_sha256"] for item in bindings),
    }


def unranked_row(game_pk: int = 2, state: str = "UNSCORED") -> dict[str, object]:
    return {
        "game_pk": game_pk,
        "target_date": "2026-07-21",
        "away_team": "Away",
        "home_team": "Home",
        "state": state,
        "scheduled_start_utc": "2026-07-21T20:00:00Z",
        "reason_codes": ["MISSING_LINEUP"],
        "source_binding_sha256s": [],
    }


def valid_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "model_label": MODEL_LABEL,
        "target_date": "2026-07-21",
        "command_inputs": {
            "date": "2026-07-21",
            "model_card": "audit/v5_1_bt/model_card.json",
        },
        "executed_at_eastern": "2026-07-21T08:00:00-04:00",
        "executed_at_utc": "2026-07-21T12:00:00Z",
        "history_cutoff_date": "2026-07-20",
        "cache_authority": {
            "namespace": "experimental_daily/history_cache",
            "cutoff_date": "2026-07-20",
            "catalog_sha256": SHA_A,
        },
        "model_card_sha256": SHA_B,
        "source_bindings": [
            source_binding(index, role, [] if role.startswith("HISTORY_") or role == "CAREER_BF" else [1])
            for index, role in enumerate(
                (
                    "LIVE_SCHEDULE",
                    "LIVE_PARTICIPANTS",
                    "FINAL_SCHEDULE",
                    "HISTORY_SCHEDULE",
                    "HISTORY_STATCAST",
                    "HISTORY_ERA",
                    "HISTORY_VENUE",
                    "CAREER_BF",
                ),
                start=1,
            )
        ],
        "verified_top3": [],
        "preliminary_top3": [ranked_row()],
        "unscored_games": [unranked_row()],
        "excluded_games": [unranked_row(3, "EXCLUDED")],
        "warnings": [],
        "content_sha256": "",
    }
    without = dict(payload)
    without.pop("content_sha256")
    payload["content_sha256"] = hashlib.sha256(canonical_json_bytes(without)).hexdigest()
    return payload


def rehash(payload: dict[str, object]) -> dict[str, object]:
    without = dict(payload)
    without.pop("content_sha256")
    payload["content_sha256"] = hashlib.sha256(canonical_json_bytes(without)).hexdigest()
    return payload


def rehash_binding(binding: dict[str, object]) -> str:
    binding["binding_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {key: value for key, value in binding.items() if key != "binding_sha256"}
        )
    ).hexdigest()
    return binding["binding_sha256"]


def valid_history_bundle() -> LiveHistoryBundle:
    bindings = tuple(
        SourceBinding(**source_binding(index, role, []))
        for index, role in enumerate(HISTORY_ROLES, start=10)
    )
    return LiveHistoryBundle(
        "2026-07-21",
        "2026-07-20",
        {},
        {},
        {},
        {},
        {
            "namespace": "experimental_daily/history_cache",
            "cutoff_date": "2026-07-20",
            "catalog_sha256": SHA_A,
        },
        bindings,
    )


class LiveTop3ContractTests(unittest.TestCase):
    def test_candidate_states_are_exact_and_mutually_exclusive(self):
        self.assertEqual(
            CANDIDATE_STATES,
            frozenset({"VERIFIED", "PRELIMINARY", "UNSCORED", "EXCLUDED"}),
        )

    def test_canonical_json_rejects_nonfinite_values(self):
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            canonical_json_bytes({"p": float("nan")})
        self.assertTrue(canonical_json_bytes({"é": 1}).endswith(b"\n"))
        self.assertEqual(canonical_json_bytes({"b": 1, "a": 2}), b'{"a":2,"b":1}\n')

    def test_source_binding_uses_canonical_query_identity_json_field(self):
        field_names = {field.name for field in fields(SourceBinding)}
        self.assertIn("query_identity_json", field_names)
        self.assertNotIn("query_identity", field_names)

    def test_source_binding_is_json_safe_and_binds_receipt(self):
        binding = SourceBinding(**source_binding())
        encoded = canonical_json_bytes(asdict(binding))
        self.assertIn(binding.receipt_sha256.encode("ascii"), encoded)
        self.assertIsInstance(binding.payload_relative, str)
        self.assertIsInstance(binding.receipt_relative, str)
        with self.assertRaises(FrozenInstanceError):
            binding.source = "savant"

    def test_source_binding_binds_exact_raw_store_query_identity(self):
        candidate = source_binding()
        self.assertNotIn(candidate["observation_id"], candidate["requested_url"])
        query_identity = json.loads(candidate["query_identity_json"])
        query_identity["projection"] = {"hydrate": ["venue"]}
        candidate["query_identity_json"] = canonical_query_identity(query_identity)
        rehash_binding(candidate)
        binding = SourceBinding(**candidate)
        self.assertEqual(
            json.loads(asdict(binding)["query_identity_json"])["observation_id"],
            binding.observation_id,
        )
        self.assertNotIn("\n", binding.query_identity_json)

        for role, query_identity in (
            (
                "LIVE_SCHEDULE",
                {
                    "observation_id": "different",
                    "target_date": "2026-07-21",
                    "url": candidate["requested_url"],
                },
            ),
            (
                "HISTORY_SCHEDULE",
                {
                    "observation_id": "obs-1",
                    "target_date": "2026-07-20",
                    "as_of_date": "2026-07-20",
                    "url": candidate["requested_url"],
                },
            ),
        ):
            invalid = source_binding(role=role, game_pks=[])
            invalid["query_identity_json"] = canonical_query_identity(query_identity)
            rehash_binding(invalid)
            with self.assertRaises(IntegrityError):
                SourceBinding(**invalid)

        invalid = source_binding()
        internal_url = f"{invalid['requested_url']}&observation_id={invalid['observation_id']}"
        invalid["requested_url"] = internal_url
        identity = json.loads(invalid["query_identity_json"])
        identity["url"] = internal_url
        invalid["query_identity_json"] = canonical_query_identity(identity)
        rehash_binding(invalid)
        with self.assertRaises(IntegrityError):
            SourceBinding(**invalid)

        for invalid_json in (
            '{}',
            candidate["query_identity_json"] + " ",
            '{"url":',
        ):
            invalid = source_binding()
            invalid["query_identity_json"] = invalid_json
            rehash_binding(invalid)
            with self.assertRaises(IntegrityError):
                SourceBinding(**invalid)

        invalid = source_binding()
        identity = json.loads(invalid["query_identity_json"])
        identity["as_of_date"] = "2026-07-20"
        invalid["query_identity_json"] = canonical_query_identity(identity)
        rehash_binding(invalid)
        with self.assertRaisesRegex(IntegrityError, "as_of_date"):
            SourceBinding(**invalid)

        invalid = source_binding(role="HISTORY_SCHEDULE", game_pks=[])
        identity = json.loads(invalid["query_identity_json"])
        identity.pop("as_of_date")
        invalid["query_identity_json"] = canonical_query_identity(identity)
        rehash_binding(invalid)
        with self.assertRaisesRegex(IntegrityError, "as_of_date"):
            SourceBinding(**invalid)

    def test_execution_times_and_final_observation_are_immutable(self):
        times = ExecutionTimes.capture(
            lambda: datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(times.executed_at_utc, "2026-07-21T12:00:00Z")
        self.assertEqual(times.executed_at_eastern, "2026-07-21T08:00:00-04:00")
        final = FinalGameObservation(
            1,
            "2026-07-21",
            {"game_pk": 1, "official_date": "2026-07-21"},
            SourceBinding(**source_binding(role="FINAL_SCHEDULE")),
        )
        with self.assertRaises(FrozenInstanceError):
            final.game_pk = 2
        with self.assertRaises(IntegrityError):
            FinalGameObservation(
                1,
                "2026-07-21",
                [1],
                SourceBinding(**source_binding(role="FINAL_SCHEDULE")),
            )
        bundle = valid_history_bundle()
        with self.assertRaises(FrozenInstanceError):
            bundle.target_date = "2026-07-22"

    def test_recording_byte_fetcher_protocol_returns_bytes(self):
        protocol = getattr(contracts, "RecordingByteFetcher", None)
        self.assertIsNotNone(protocol)
        parameters = inspect.signature(protocol.__call__).parameters
        self.assertEqual(tuple(parameters), ("self", "source", "url", "max_bytes"))
        self.assertIs(get_type_hints(protocol.__call__)["return"], bytes)

    def test_observation_fetcher_protocol_returns_live_http_observation(self):
        observation_type = getattr(contracts, "LiveHttpObservation", None)
        self.assertIsNotNone(observation_type)
        protocol = contracts.ObservationFetcher
        parameters = inspect.signature(protocol.__call__).parameters
        self.assertEqual(tuple(parameters), ("self", "source", "url", "max_bytes"))
        self.assertIs(
            get_type_hints(protocol.__call__)["return"],
            observation_type,
        )

    def test_live_http_observation_is_frozen_and_validates_timing(self):
        observation = live_http_observation()
        self.assertEqual(observation.body, b"{}")
        with self.assertRaises(FrozenInstanceError):
            observation.http_status = 500
        with self.assertRaisesRegex(IntegrityError, "response completion"):
            live_http_observation(
                started_at_utc="2026-07-21T12:00:01Z",
                response_completed_at_utc="2026-07-21T12:00:00Z",
            )

    def test_live_http_observation_validates_authority_status_headers_and_body(self):
        invalid_values = (
            ("requested_url", "https://evil.example/request"),
            ("final_url", "http://statsapi.mlb.com/response"),
            ("final_url", "https://statsapi.mlb.com:443/response"),
            ("final_url", "https://baseballsavant.mlb.com/response"),
            ("http_status", True),
            ("http_status", 600),
            ("content_type", 1),
            ("etag", 1),
            ("last_modified", 1),
            ("body", bytearray(b"{}")),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                with self.assertRaises(IntegrityError):
                    live_http_observation(**{field: value})

    def test_live_history_bundle_authenticates_dates_cache_and_roles(self):
        valid = valid_history_bundle()
        self.assertEqual(valid.cutoff_date, "2026-07-20")
        args = (
            valid.target_date,
            valid.cutoff_date,
            valid.feature_history,
            valid.league_era_by_asof,
            valid.team_code_by_name,
            valid.venue_references,
            dict(valid.cache_authority),
        )
        with self.assertRaises(IntegrityError):
            LiveHistoryBundle("2026-7-21", *args[1:], valid.source_bindings)
        with self.assertRaises(IntegrityError):
            LiveHistoryBundle(args[0], "2026-07-19", *args[2:], valid.source_bindings)
        bad_cache = dict(valid.cache_authority)
        bad_cache["cutoff_date"] = "2026-07-19"
        with self.assertRaises(IntegrityError):
            LiveHistoryBundle(*args[:6], bad_cache, valid.source_bindings)
        with self.assertRaises(IntegrityError):
            LiveHistoryBundle(*args, list(valid.source_bindings))
        with self.assertRaises(IntegrityError):
            LiveHistoryBundle(*args, valid.source_bindings[:-1])
        with self.assertRaises(IntegrityError):
            LiveHistoryBundle(*args, (*valid.source_bindings[:-1], object()))

        for role in ("LIVE_SCHEDULE", "CAREER_BF"):
            extra = SourceBinding(**source_binding(99, role, [1] if role == "LIVE_SCHEDULE" else []))
            with self.assertRaisesRegex(IntegrityError, "history.*roles"):
                LiveHistoryBundle(*args, (*valid.source_bindings, extra))

        changed = []
        for binding in valid.source_bindings:
            candidate = asdict(binding)
            identity = json.loads(candidate["query_identity_json"])
            identity["as_of_date"] = "2026-07-21"
            candidate["query_identity_json"] = canonical_query_identity(identity)
            rehash_binding(candidate)
            changed.append(SourceBinding(**candidate))
        with self.assertRaisesRegex(IntegrityError, "as_of_date"):
            LiveHistoryBundle(*args, tuple(changed))

    def test_final_observation_authenticates_schedule_binding_and_date(self):
        projected = {"game_pk": 1, "official_date": "2026-07-21"}
        with self.assertRaises(IntegrityError):
            FinalGameObservation(1, "2026-07-21", projected, SourceBinding(**source_binding()))
        with self.assertRaises(IntegrityError):
            FinalGameObservation(
                1,
                "2026-07-21",
                projected,
                SourceBinding(**source_binding(role="FINAL_SCHEDULE", game_pks=[2])),
            )
        with self.assertRaises(IntegrityError):
            FinalGameObservation(
                1,
                "2026-07-21",
                {**projected, "official_date": "2026-07-20"},
                SourceBinding(**source_binding(role="FINAL_SCHEDULE")),
            )
        for invalid_projection in (
            {"official_date": "2026-07-21"},
            {"game_pk": 2, "official_date": "2026-07-21"},
        ):
            with self.assertRaisesRegex(IntegrityError, "projected game_pk"):
                FinalGameObservation(
                    1,
                    "2026-07-21",
                    invalid_projection,
                    SourceBinding(**source_binding(role="FINAL_SCHEDULE")),
                )
        with self.assertRaisesRegex(IntegrityError, "official date"):
            FinalGameObservation(
                1,
                "2026-07-21",
                {"game_pk": 1},
                SourceBinding(**source_binding(role="FINAL_SCHEDULE")),
            )

    def test_valid_payload_passes_python_and_jsonschema(self):
        payload = valid_payload()
        self.assertEqual(validate_run_payload(payload), payload)
        schema_path = Path(__file__).parents[1] / "schemas" / "experimental_daily_top3.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validate(payload, schema)

    def test_schema_closes_query_identity_and_normalizes_posix_paths(self):
        schema_path = Path(__file__).parents[1] / "schemas" / "experimental_daily_top3.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        source_binding_schema = schema["$defs"]["sourceBinding"]
        self.assertIn("query_identity_json", source_binding_schema["required"])
        self.assertNotIn("query_identity", source_binding_schema["properties"])
        self.assertEqual(
            source_binding_schema["properties"]["query_identity_json"]["type"],
            "string",
        )

        def assert_closed_objects(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                for child in node.values():
                    assert_closed_objects(child)
            elif isinstance(node, list):
                for child in node:
                    assert_closed_objects(child)

        assert_closed_objects(schema)
        path_validator = Draft202012Validator(
            {
                "$schema": schema["$schema"],
                "$ref": "#/$defs/posixPath",
                "$defs": schema["$defs"],
            }
        )
        for invalid_path in ("audit//model.json", "audit/v5_1_bt/"):
            with self.assertRaises(ValidationError):
                path_validator.validate(invalid_path)

    def test_rejects_unknown_and_missing_fields(self):
        payload = valid_payload()
        payload["unexpected"] = True
        with self.assertRaises(IntegrityError):
            validate_run_payload(payload)
        payload = valid_payload()
        del payload["warnings"]
        with self.assertRaises(IntegrityError):
            validate_run_payload(payload)

    def test_rejects_reserved_verified_population(self):
        payload = valid_payload()
        row = ranked_row()
        row["state"] = "VERIFIED"
        payload["verified_top3"] = [row]
        without = dict(payload)
        without.pop("content_sha256")
        payload["content_sha256"] = hashlib.sha256(canonical_json_bytes(without)).hexdigest()
        with self.assertRaisesRegex(IntegrityError, "reserved"):
            validate_run_payload(payload)

    def test_rejects_dates_times_digests_and_nonfinite_values(self):
        payload = valid_payload()
        payload["preliminary_top3"][0]["pick_probability"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            validate_run_payload(payload)
        for key, value in (
            ("target_date", "2026-7-21"),
            ("executed_at_utc", "2026-07-21T12:00:00+00:00"),
            ("model_card_sha256", "A" * 64),
        ):
            candidate = valid_payload()
            candidate[key] = value
            with self.assertRaises(IntegrityError):
                validate_run_payload(candidate)

    def test_rejects_rank_limit_gaps_duplicates_and_state_mismatch(self):
        payload = valid_payload()
        payload["preliminary_top3"] = [ranked_row(i, i) for i in range(1, 5)]
        with self.assertRaises(IntegrityError):
            validate_run_payload(payload)
        payload = valid_payload()
        payload["preliminary_top3"][0]["rank"] = 2
        with self.assertRaises(IntegrityError):
            validate_run_payload(payload)
        payload = valid_payload()
        payload["unscored_games"][0]["game_pk"] = 1
        with self.assertRaises(IntegrityError):
            validate_run_payload(payload)
        payload = valid_payload()
        payload["preliminary_top3"][0]["state"] = "UNSCORED"
        with self.assertRaises(IntegrityError):
            validate_run_payload(payload)

    def test_rejects_rank_or_probability_on_unscored_and_wrong_digest(self):
        payload = valid_payload()
        payload["unscored_games"][0]["rank"] = 1
        with self.assertRaises(IntegrityError):
            validate_run_payload(payload)
        payload = valid_payload()
        payload["content_sha256"] = "d" * 64
        with self.assertRaises(IntegrityError):
            validate_run_payload(payload)

    def test_validate_target_date_requires_canonical_form(self):
        self.assertEqual(validate_target_date("2026-07-21"), "2026-07-21")
        with self.assertRaises(IntegrityError):
            validate_target_date("2026-7-21")

    def test_direct_binding_rejects_path_traversal_source_and_host(self):
        for field, value in (
            ("payload_relative", "../payload.json"),
            ("payload_relative", "data//payload.json"),
            ("receipt_relative", "audit/receipts/"),
            ("source", "other"),
            ("final_url", "https://evil.example/payload"),
        ):
            candidate = source_binding()
            candidate[field] = value
            candidate["binding_sha256"] = hashlib.sha256(
                canonical_json_bytes({k: v for k, v in candidate.items() if k != "binding_sha256"})
            ).hexdigest()
            with self.assertRaises(IntegrityError):
                SourceBinding(**candidate)

    def test_direct_binding_final_url_requires_exact_https_authority(self):
        for final_url in (
            "http://statsapi.mlb.com/payload",
            "https://statsapi.mlb.com:443/payload",
            "https://user:pass@statsapi.mlb.com/payload",
            "https://STATSAPI.MLB.COM/payload",
        ):
            candidate = source_binding()
            candidate["final_url"] = final_url
            candidate["binding_sha256"] = hashlib.sha256(
                canonical_json_bytes({k: v for k, v in candidate.items() if k != "binding_sha256"})
            ).hexdigest()
            with self.assertRaises(IntegrityError):
                SourceBinding(**candidate)

    def test_direct_binding_rejects_backdated_durable_and_binding_tamper(self):
        candidate = source_binding()
        candidate["durable_at_utc"] = "2026-07-21T11:59:59Z"
        candidate["binding_sha256"] = hashlib.sha256(
            canonical_json_bytes({k: v for k, v in candidate.items() if k != "binding_sha256"})
        ).hexdigest()
        with self.assertRaises(IntegrityError):
            SourceBinding(**candidate)
        candidate = source_binding()
        candidate["binding_sha256"] = "d" * 64
        with self.assertRaises(IntegrityError):
            SourceBinding(**candidate)

    def test_ranked_binding_references_require_scope_roles_and_exact_boundary(self):
        payload = valid_payload()
        payload["preliminary_top3"][0]["source_binding_sha256s"] = []
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))
        payload = valid_payload()
        payload["preliminary_top3"][0]["source_binding_sha256s"] = ["e" * 64]
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))

    def test_ranked_binding_references_require_successful_http_status(self):
        payload = valid_payload()
        binding = payload["source_bindings"][0]
        prior_digest = binding["binding_sha256"]
        binding["http_status"] = 206
        replacement_digest = rehash_binding(binding)
        refs = payload["preliminary_top3"][0]["source_binding_sha256s"]
        payload["preliminary_top3"][0]["source_binding_sha256s"] = sorted(
            replacement_digest if digest == prior_digest else digest for digest in refs
        )
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))

    def test_unranked_binding_references_resolve_and_respect_game_scope(self):
        payload = valid_payload()
        payload["unscored_games"][0]["source_binding_sha256s"] = ["d" * 64]
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))

        payload = valid_payload()
        scoped_elsewhere = payload["source_bindings"][0]["binding_sha256"]
        payload["unscored_games"][0]["source_binding_sha256s"] = [scoped_elsewhere]
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))

        payload = valid_payload()
        unscoped_history = next(
            binding["binding_sha256"]
            for binding in payload["source_bindings"]
            if binding["binding_role"] == "HISTORY_SCHEDULE"
        )
        payload["unscored_games"][0]["source_binding_sha256s"] = [unscoped_history]
        self.assertEqual(validate_run_payload(rehash(payload)), payload)

    def test_ranked_rows_require_all_binding_roles_and_model_card_match(self):
        payload = valid_payload()
        payload["preliminary_top3"][0]["source_binding_sha256s"] = payload["preliminary_top3"][0]["source_binding_sha256s"][:-1]
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))
        payload = valid_payload()
        payload["preliminary_top3"][0]["model_card_sha256"] = SHA_A
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))

    def test_ranked_rows_reject_negative_starter_edge(self):
        payload = valid_payload()
        payload["preliminary_top3"][0]["starter_edge_abs"] = -0.1
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))

    def test_schema_requires_source_bindings_and_ranked_references(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "schemas" / "experimental_daily_top3.schema.json").read_text()
        )
        payload = valid_payload()
        payload["source_bindings"] = []
        with self.assertRaises(ValidationError):
            validate(payload, schema)
        payload = valid_payload()
        payload["preliminary_top3"][0]["source_binding_sha256s"] = []
        with self.assertRaises(ValidationError):
            validate(payload, schema)
        payload = valid_payload()
        replacement_hashes = []
        for binding in payload["source_bindings"]:
            binding["game_pks"] = [99]
            binding["binding_sha256"] = hashlib.sha256(
                canonical_json_bytes({k: v for k, v in binding.items() if k != "binding_sha256"})
            ).hexdigest()
            replacement_hashes.append(binding["binding_sha256"])
        payload["preliminary_top3"][0]["source_binding_sha256s"] = sorted(replacement_hashes)
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))
        payload = valid_payload()
        payload["preliminary_top3"][0]["observation_boundary_utc"] = "2026-07-21T12:00:00Z"
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))
        payload = valid_payload()
        payload["preliminary_top3"][0]["scheduled_start_utc"] = "2026-07-21T12:00:01Z"
        with self.assertRaises(IntegrityError):
            validate_run_payload(rehash(payload))

    def test_execution_et_utc_mismatch_and_deep_freeze(self):
        with self.assertRaises(IntegrityError):
            ExecutionTimes("2026-07-21T08:00:00-04:00", "2026-07-21T13:00:00Z")
        with self.assertRaises(IntegrityError):
            ExecutionTimes("2026-02-30T08:00:00-05:00", "2026-02-30T13:00:00Z")
        with self.assertRaises(IntegrityError):
            ExecutionTimes("2026-07-21T08:00:00.1234567-04:00", "2026-07-21T12:00:00Z")
        nested = {"x": {"items": [1]}}
        valid = valid_history_bundle()
        bundle = LiveHistoryBundle(
            valid.target_date,
            valid.cutoff_date,
            nested,
            valid.league_era_by_asof,
            valid.team_code_by_name,
            valid.venue_references,
            valid.cache_authority,
            valid.source_bindings,
        )
        nested["x"]["items"].append(2)
        self.assertEqual(bundle.feature_history["x"]["items"], (1,))
        projected = {
            "game_pk": 1,
            "official_date": "2026-07-21",
            "nested": [1],
        }
        final = FinalGameObservation(
            1,
            "2026-07-21",
            projected,
            SourceBinding(**source_binding(role="FINAL_SCHEDULE")),
        )
        projected["nested"].append(2)
        self.assertEqual(final.projected_game["nested"], (1,))

    def test_schema_format_and_state_negative_cases(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "schemas" / "experimental_daily_top3.schema.json").read_text()
        )
        payload = valid_payload()
        payload["target_date"] = "2026-02-30"
        with self.assertRaises(ValidationError):
            validate(payload, schema, format_checker=FormatChecker())
        payload = valid_payload()
        payload["excluded_games"][0]["state"] = "UNSCORED"
        with self.assertRaises(ValidationError):
            validate(payload, schema, format_checker=FormatChecker())


if __name__ == "__main__":
    unittest.main()
