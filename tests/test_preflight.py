from __future__ import annotations

from collections.abc import Mapping
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mlb_backtest.diagnostics import canonical_json_bytes, sha256_file
from mlb_backtest.preflight import (
    MINIMUM_SOURCES,
    OPTIONAL_REQUIRED_FIELDS,
    PREFLIGHT_REQUIRED_KEYS,
    QUERY_EVIDENCE_KEYS,
    REQUIRED_FIELDS,
    build_preflight,
    child_logical_query_id,
    parent_logical_query_id,
    parse_preflight,
    preflight_schema_document,
    protected_inventory,
    render_preflight_markdown,
    write_preflight,
)
from mlb_backtest.protocols import (
    expected_historical_run,
    load_run_spec,
    parse_run_spec,
)


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
PREFLIGHT_SCHEMA = ROOT / "schemas/v5_2_preflight.schema.json"


class PreflightTest(unittest.TestCase):
    def test_current_workspace_reports_required_gap_without_fetching(self):
        before = protected_inventory(ROOT)
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ),
            patch(
                "http.client.HTTPConnection.request",
                side_effect=AssertionError("network forbidden"),
            ),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("network forbidden"),
            ),
        ):
            report = build_preflight(ROOT, load_run_spec(HISTORICAL_SPEC))
        self.assertEqual(before, protected_inventory(ROOT))
        self.assertEqual(report["status"], "BLOCKED_REQUIRED_ACQUISITION")
        for year in (2018, 2019, 2020, 2021):
            self.assertEqual(
                report["seasons"][str(year)]["minimum_contract"], "MISSING"
            )
        legacy_statuses = {
            source["status"]
            for year in (2022, 2023, 2024)
            for source in report["seasons"][str(year)]["sources"].values()
        }
        self.assertIn("OBSERVED_LEGACY_NOT_V52_COMPLETE", legacy_statuses)
        markdown = render_preflight_markdown(report)
        self.assertIn("OBSERVED_LEGACY_NOT_V52_COMPLETE", markdown)
        self.assertFalse(report["may_fit_H22"])
        self.assertFalse(report["fitted_model_cards_present"])
        self.assertEqual(
            report["fitted_model_card_namespace"],
            "models/v5_2_qd/folds/{H22,H23,H24}/model_card.json",
        )
        self.assertTrue(
            all(
                step["status"] == "NOT_IMPLEMENTED_PLANNED"
                for step in report["acquisition_steps"]
                + report["execution_steps_after_ready"]
            )
        )

    def test_optional_sources_never_fail_minimum_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_complete_inventory(Path(directory))
            report = build_preflight(root, parse_run_spec(expected_historical_run()))
        self.assertEqual(report["status"], "READY")
        self.assertEqual(
            report["optional_sources"]["market_odds"]["status"], "ABSENT_NULL"
        )
        self.assertEqual(
            report["optional_sources"]["environment"]["status"], "ABSENT_NEUTRAL"
        )

    def test_valid_optional_evidence_activates_without_changing_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_complete_inventory(Path(directory))
            make_optional_inventory(root, "market_odds")
            make_optional_inventory(root, "environment", namespace="inventory/2022")
            report = build_preflight(root, parse_run_spec(expected_historical_run()))
        self.assertEqual(report["status"], "READY")
        for source in ("market_odds", "environment"):
            optional = report["optional_sources"][source]
            self.assertEqual(optional["status"], "PRESENT_VALID")
            self.assertEqual(optional["fallback_status"], None)
            self.assertEqual(len(optional["queries"]), 1)
            self.assertEqual(set(optional["queries"][0]), QUERY_EVIDENCE_KEYS)

    def test_optional_present_valid_rejects_legacy_query_and_null_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_complete_inventory(Path(directory))
            make_optional_inventory(root, "market_odds")
            report = build_preflight(root, parse_run_spec(expected_historical_run()))
        query = report["optional_sources"]["market_odds"]["queries"][0]
        query["evidence_kind"] = "LEGACY_MANIFEST_ONLY"
        query["parent_logical_query_id"] = None
        query["logical_query_id"] = (
            f"legacy:{query['manifest_sha256']}:{query['query']['season']}:market_odds"
        )
        with self.assertRaisesRegex(ValueError, "optional query evidence"):
            parse_preflight(report)

    def test_optional_query_schema_requires_v52_evidence_and_parent_identity(self):
        schema = preflight_schema_document()
        optional_query = schema["$defs"]["optional_query_evidence"]
        self.assertEqual(
            schema["properties"]["optional_sources"]["properties"]["market_odds"]
            ["properties"]["queries"]["items"],
            {"$ref": "#/$defs/optional_query_evidence"},
        )
        self.assertEqual(
            optional_query["properties"]["evidence_kind"],
            {"const": "V52_MANIFEST_SIDECAR"},
        )
        self.assertEqual(
            optional_query["properties"]["parent_logical_query_id"],
            {"$ref": "#/$defs/sha256"},
        )
        self.assertEqual(
            optional_query["properties"]["split_index"],
            {"type": "integer", "minimum": 1},
        )
        self.assertEqual(
            optional_query["properties"]["split_count"],
            {"type": "integer", "minimum": 1},
        )
        self.assertEqual(
            optional_query["properties"]["logical_query_id"]["pattern"],
            "^[0-9a-f]{64}:[1-9][0-9]*$",
        )
        common_query = schema["$defs"]["query_evidence"]
        self.assertEqual(
            common_query["properties"]["logical_query_id"]["anyOf"][0]["pattern"],
            "^[0-9a-f]{64}:[1-9][0-9]*$",
        )
        self.assertIn("split_index", common_query["properties"])
        self.assertIn("split_count", common_query["properties"])
        self.assertEqual(
            common_query["allOf"][0]["if"]["properties"]["evidence_kind"],
            {"const": "V52_MANIFEST_SIDECAR"},
        )
        self.assertEqual(
            common_query["allOf"][0]["then"]["properties"]["split_index"],
            {"type": "integer", "minimum": 1},
        )
        self.assertEqual(
            common_query["allOf"][1]["then"]["properties"]["split_index"],
            {"type": "null"},
        )
        self.assertNotIn(
            "LEGACY_MANIFEST_ONLY",
            json.dumps(optional_query, sort_keys=True),
        )

    def test_required_complete_rejects_serialized_query_coverage_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = build_preflight(
                make_complete_inventory(Path(directory)),
                parse_run_spec(expected_historical_run()),
            )
        source = MINIMUM_SOURCES[0]
        record = payload["seasons"]["2018"]["sources"][source]

        narrowed = copy.deepcopy(payload)
        narrowed_record = narrowed["seasons"]["2018"]["sources"][source]
        narrowed_record["queries"][0]["child_range"]["end_date"] = "2018-06-29"

        rebound = copy.deepcopy(payload)
        rebound_record = rebound["seasons"]["2018"]["sources"][source]
        parent = parent_logical_query_id(source, 2018, "2018-02-01", "2018-12-31")
        for index, query in enumerate(rebound_record["queries"], 1):
            query["query"]["start_date"] = "2018-02-01"
            query["parent_logical_query_id"] = parent
            query["logical_query_id"] = f"{parent}:{index}"
        rebound_record["queries"][0]["child_range"]["start_date"] = "2018-02-01"
        rebound_record["logical_query_ids"] = [parent]

        tampered = copy.deepcopy(payload)
        tampered["seasons"]["2018"]["sources"][source]["queries"][0][
            "logical_query_id"
        ] = "tampered"

        duplicated = copy.deepcopy(payload)
        duplicate_queries = duplicated["seasons"]["2018"]["sources"][source][
            "queries"
        ]
        duplicate_queries[1]["logical_query_id"] = duplicate_queries[0][
            "logical_query_id"
        ]

        wrong_count = copy.deepcopy(payload)
        wrong_count["seasons"]["2018"]["sources"][source]["queries"][0][
            "split_count"
        ] = 3

        wrong_order = copy.deepcopy(payload)
        ordered_queries = wrong_order["seasons"]["2018"]["sources"][source][
            "queries"
        ]
        for query, split_index in zip(ordered_queries, (2, 1), strict=True):
            query["split_index"] = split_index
            query["logical_query_id"] = child_logical_query_id(
                query["parent_logical_query_id"], split_index
            )

        self.assertEqual(record["status"], "COMPLETE")
        for label, changed in {
            "narrowed child": narrowed,
            "rebound full set": rebound,
            "tampered logical id": tampered,
            "duplicated logical id": duplicated,
            "wrong split count": wrong_count,
            "wrong split order": wrong_order,
        }.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                parse_preflight(changed)

    def test_optional_present_valid_rejects_narrowed_child_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_complete_inventory(Path(directory))
            make_optional_inventory(root, "market_odds")
            payload = build_preflight(
                root, parse_run_spec(expected_historical_run())
            )
        payload["optional_sources"]["market_odds"]["queries"][0]["child_range"][
            "start_date"
        ] = "2022-02-01"
        with self.assertRaisesRegex(ValueError, "optional query coverage"):
            parse_preflight(payload)

    def test_malformed_optional_evidence_is_separate_and_never_blocks_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_complete_inventory(Path(directory))
            path = root / "inventory/2022/environment/bad.manifest.json"
            path.parent.mkdir(parents=True)
            path.write_text("{")
            report = build_preflight(root, parse_run_spec(expected_historical_run()))
        self.assertEqual(report["status"], "READY")
        self.assertEqual(report["inventory_errors"], [])
        optional = report["optional_sources"]["environment"]
        self.assertEqual(optional["status"], "PRESENT_INVALID")
        self.assertEqual(optional["fallback_status"], "ABSENT_NEUTRAL")
        self.assertEqual(optional["errors"][0]["code"], "MALFORMED_MANIFEST_JSON")

    def test_complete_fixture_is_ready_and_json_serializable(self):
        with tempfile.TemporaryDirectory() as directory:
            report = build_preflight(
                make_complete_inventory(Path(directory)),
                parse_run_spec(expected_historical_run()),
            )
        self.assertEqual(report["status"], "READY")
        self.assertTrue(report["may_fit_H22"])
        self.assertEqual(json.loads(json.dumps(report)), report)

    def test_each_integrity_failure_blocks_ready(self):
        mutations = {
            "raw_hash": lambda root: (
                root / first_manifest(root)["raw_path"]
            ).write_bytes(b"changed"),
            "sidecar_hash": mutate_sidecar_without_manifest_update,
            "schema": lambda root: rewrite_sidecar(root, {"schema_fields": ["gamePk"]}),
            "duplicate_game_pk": lambda root: rewrite_sidecar(
                root, {"game_pks": [1, 1]}
            ),
            "invalid_game_pk": lambda root: rewrite_sidecar(root, {"game_pks": [0]}),
            "date_range": lambda root: rewrite_manifest(
                root,
                {
                    "query": {
                        "season": 2018,
                        "start_date": "2018-02-01",
                        "end_date": "2018-12-31",
                    }
                },
            ),
            "date_range_rebound_parent": rebind_manifest_to_partial_query,
            "split_parent": lambda root: rewrite_manifest(
                root, {"parent_logical_query_id": "wrong"}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = make_complete_inventory(Path(directory))
                mutate(root)
                report = build_preflight(
                    root, parse_run_spec(expected_historical_run())
                )
                self.assertEqual(report["status"], "BLOCKED_REQUIRED_ACQUISITION")

    def test_malformed_json_is_recorded_and_blocks_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_complete_inventory(Path(directory))
            next((root / "inventory").rglob("*.manifest.json")).write_text("{")
            report = build_preflight(root, parse_run_spec(expected_historical_run()))
        self.assertEqual(report["status"], "BLOCKED_REQUIRED_ACQUISITION")
        self.assertIn(
            "MALFORMED_MANIFEST_JSON",
            {row["code"] for row in report["inventory_errors"]},
        )

    def test_malformed_sidecar_is_recorded_with_specific_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_complete_inventory(Path(directory))
            manifest = first_manifest(root)
            (root / str(manifest["sidecar_path"])).write_text("{")
            report = build_preflight(root, parse_run_spec(expected_historical_run()))
        self.assertEqual(report["status"], "BLOCKED_REQUIRED_ACQUISITION")
        self.assertIn(
            "MALFORMED_SIDECAR_JSON",
            {row["code"] for row in report["inventory_errors"]},
        )

    def test_scanner_never_uses_network_or_mutates_data_or_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_complete_inventory(Path(directory))
            before = protected_inventory(root)
            with (
                patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch(
                    "http.client.HTTPConnection.request",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch(
                    "socket.create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch(
                    "pathlib.Path.write_bytes",
                    side_effect=AssertionError("write forbidden"),
                ),
                patch(
                    "pathlib.Path.write_text",
                    side_effect=AssertionError("write forbidden"),
                ),
                patch("os.replace", side_effect=AssertionError("write forbidden")),
                patch("os.rename", side_effect=AssertionError("write forbidden")),
                patch("os.remove", side_effect=AssertionError("write forbidden")),
                patch("os.unlink", side_effect=AssertionError("write forbidden")),
            ):
                report = build_preflight(
                    root, parse_run_spec(expected_historical_run())
                )
            self.assertEqual(report["status"], "READY")
            self.assertEqual(before, protected_inventory(root))

    def test_preflight_parser_and_schema_are_strict_at_fixed_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = build_preflight(
                make_complete_inventory(Path(directory)),
                parse_run_spec(expected_historical_run()),
            )
        schema = preflight_schema_document()
        self.assertEqual(json.loads(PREFLIGHT_SCHEMA.read_text()), schema)
        self.assertEqual(set(payload), PREFLIGHT_REQUIRED_KEYS)
        self.assertEqual(set(schema["required"]), PREFLIGHT_REQUIRED_KEYS)
        self.assertEqual(set(schema["properties"]), PREFLIGHT_REQUIRED_KEYS)
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["seasons"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["season"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["source_record"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["step"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["query_evidence"]["additionalProperties"])
        self.assertEqual(
            set(schema["$defs"]["query_evidence"]["properties"]),
            QUERY_EVIDENCE_KEYS,
        )

        mutations = []
        changed = copy.deepcopy(payload)
        changed["unknown"] = 1
        mutations.append(changed)
        changed = copy.deepcopy(payload)
        changed["seasons"]["2018"]["unknown"] = 1
        mutations.append(changed)
        changed = copy.deepcopy(payload)
        changed["seasons"]["2018"]["sources"][MINIMUM_SOURCES[0]]["status"] = "BOGUS"
        mutations.append(changed)
        changed = copy.deepcopy(payload)
        changed["optional_sources"]["market_odds"]["status"] = "ABSENT_NEUTRAL"
        mutations.append(changed)
        changed = copy.deepcopy(payload)
        changed["acquisition_steps"][0]["status"] = "READY"
        mutations.append(changed)
        changed = copy.deepcopy(payload)
        del changed["seasons"]["2024"]
        mutations.append(changed)
        for changed in mutations:
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    parse_preflight(changed)

    def test_recursive_parser_rejects_nested_type_hash_date_path_and_error_tampering(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            payload = build_preflight(
                make_complete_inventory(Path(directory)),
                parse_run_spec(expected_historical_run()),
            )
        source = MINIMUM_SOURCES[0]
        mutations = {
            "negative count": ("row_count", -1),
            "bool count": ("game_count", True),
            "bad content hash": ("content_sha256", ["not-a-hash"]),
            "bad expected date": ("expected_start", "2018-1-1"),
            "unsafe child path": ("split_children", ["../escape"]),
        }
        for label, (field, value) in mutations.items():
            changed = copy.deepcopy(payload)
            changed["seasons"]["2018"]["sources"][source][field] = value
            with self.subTest(label=label), self.assertRaises(ValueError):
                parse_preflight(changed)
        changed = copy.deepcopy(payload)
        changed["status"] = "BLOCKED_REQUIRED_ACQUISITION"
        changed["inventory_errors"] = [
            {"path": "inventory/x", "code": "RAW_HASH_MISMATCH", "detail": 1}
        ]
        with self.assertRaises(ValueError):
            parse_preflight(changed)

    def test_every_source_child_preserves_exact_canonical_query_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            report = build_preflight(
                make_complete_inventory(Path(directory)),
                parse_run_spec(expected_historical_run()),
            )
        for year in map(str, range(2018, 2025)):
            for source in MINIMUM_SOURCES:
                record = report["seasons"][year]["sources"][source]
                self.assertEqual(len(record["queries"]), 2)
                for query in record["queries"]:
                    self.assertEqual(set(query), QUERY_EVIDENCE_KEYS)
                    self.assertEqual(query["source"], source)
                    self.assertEqual(query["query"]["season"], int(year))

    def test_committed_json_round_trips_to_exact_markdown_in_frozen_source_order(self):
        payload = parse_preflight(
            json.loads(
                (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").read_text()
            )
        )
        rendered = render_preflight_markdown(payload).encode()
        committed = (ROOT / "reports/v5_2_qd/walkforward_500_preflight.md").read_bytes()
        self.assertEqual(rendered, committed)
        row = next(
            line
            for line in rendered.decode().splitlines()
            if line.startswith("| 2022 |")
        )
        positions = [row.index(source) for source in MINIMUM_SOURCES]
        self.assertEqual(positions, sorted(positions))

    def test_parser_rejects_season_contract_inconsistent_with_source_status(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = build_preflight(
                make_complete_inventory(Path(directory)),
                parse_run_spec(expected_historical_run()),
            )
        payload["seasons"]["2024"]["sources"][MINIMUM_SOURCES[0]]["status"] = "MISSING"
        with self.assertRaises(ValueError):
            parse_preflight(payload)

    def test_writer_is_deterministic_and_does_not_claim_commands_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_complete_inventory(Path(directory) / "root")
            run_path = Path(directory) / "run.json"
            run_path.write_bytes(
                canonical_json_bytes(expected_historical_run()) + b"\n"
            )
            outputs = []
            for suffix in ("one", "two"):
                base = Path(directory) / suffix
                json_path = base / "preflight.json"
                markdown_path = base / "preflight.md"
                schema_path = base / "schema.json"
                write_preflight(root, run_path, json_path, markdown_path, schema_path)
                outputs.append(
                    (
                        json_path.read_bytes(),
                        markdown_path.read_bytes(),
                        schema_path.read_bytes(),
                    )
                )
            self.assertEqual(outputs[0], outputs[1])
            report = json.loads(outputs[0][0])
            self.assertTrue(
                all(
                    step["status"] == "NOT_IMPLEMENTED_PLANNED"
                    for step in report["acquisition_steps"]
                    + report["execution_steps_after_ready"]
                )
            )


def make_complete_inventory(root: Path) -> Path:
    for namespace in ("data", "audit"):
        (root / namespace).mkdir(parents=True, exist_ok=True)
    for year in range(2018, 2025):
        for source in MINIMUM_SOURCES:
            logical = parent_logical_query_id(
                source, year, f"{year}-01-01", f"{year}-12-31"
            )
            parts = (
                (f"{year}-01-01", f"{year}-06-30"),
                (f"{year}-07-01", f"{year}-12-31"),
            )
            for split_index, (start, end) in enumerate(parts, 1):
                base = root / "inventory" / str(year) / source / f"part-{split_index}"
                base.parent.mkdir(parents=True, exist_ok=True)
                raw = base.with_suffix(".raw")
                raw.write_bytes(f"{source}:{year}:{split_index}".encode())
                sidecar = base.with_suffix(".sidecar.json")
                side_payload = {
                    "schema_version": 1,
                    "schema_fields": list(REQUIRED_FIELDS[source]),
                    "row_count": 2,
                    "game_count": 1,
                    "game_pks": [year * 100 + split_index],
                    "event_dates": [start, end],
                }
                sidecar.write_bytes(canonical_json_bytes(side_payload) + b"\n")
                manifest = {
                    "schema_version": 1,
                    "source": source,
                    "logical_query_id": f"{logical}:{split_index}",
                    "parent_logical_query_id": logical,
                    "query": {
                        "season": year,
                        "start_date": f"{year}-01-01",
                        "end_date": f"{year}-12-31",
                    },
                    "child_range": {"start_date": start, "end_date": end},
                    "raw_path": raw.relative_to(root).as_posix(),
                    "raw_sha256": sha256_file(raw),
                    "sidecar_path": sidecar.relative_to(root).as_posix(),
                    "sidecar_sha256": sha256_file(sidecar),
                    "retrieved_at_utc": "2026-07-12T00:00:00Z",
                    "split_index": split_index,
                    "split_count": 2,
                }
                base.with_suffix(".manifest.json").write_bytes(
                    canonical_json_bytes(manifest) + b"\n"
                )
    return root


def make_optional_inventory(
    root: Path, source: str, *, namespace: str = "inventory_optional"
) -> None:
    base = root / namespace / source / "part-1"
    base.parent.mkdir(parents=True, exist_ok=True)
    raw = base.with_suffix(".raw")
    raw.write_bytes(source.encode())
    sidecar = base.with_suffix(".sidecar.json")
    side_payload = {
        "schema_version": 1,
        "schema_fields": list(OPTIONAL_REQUIRED_FIELDS[source]),
        "row_count": 1,
        "game_count": 1,
        "game_pks": [1],
        "event_dates": ["2022-01-01", "2022-12-31"],
    }
    sidecar.write_bytes(canonical_json_bytes(side_payload) + b"\n")
    parent = parent_logical_query_id(source, 2022, "2022-01-01", "2022-12-31")
    manifest = {
        "schema_version": 1,
        "source": source,
        "logical_query_id": f"{parent}:1",
        "parent_logical_query_id": parent,
        "query": {"season": 2022, "start_date": "2022-01-01", "end_date": "2022-12-31"},
        "child_range": {"start_date": "2022-01-01", "end_date": "2022-12-31"},
        "raw_path": raw.relative_to(root).as_posix(),
        "raw_sha256": sha256_file(raw),
        "sidecar_path": sidecar.relative_to(root).as_posix(),
        "sidecar_sha256": sha256_file(sidecar),
        "retrieved_at_utc": "2026-07-12T00:00:00Z",
        "split_index": 1,
        "split_count": 1,
    }
    base.with_suffix(".manifest.json").write_bytes(
        canonical_json_bytes(manifest) + b"\n"
    )


def first_manifest(root: Path) -> dict[str, object]:
    return json.loads(next((root / "inventory").rglob("*.manifest.json")).read_text())


def rewrite_manifest(root: Path, patch_values: Mapping[str, object]) -> None:
    path = next((root / "inventory").rglob("*.manifest.json"))
    value = json.loads(path.read_text())
    value.update(patch_values)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def rewrite_sidecar(root: Path, patch_values: Mapping[str, object]) -> None:
    manifest_path = next((root / "inventory").rglob("*.manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    path = root / str(manifest["sidecar_path"])
    value = json.loads(path.read_text())
    value.update(patch_values)
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    manifest["sidecar_sha256"] = sha256_file(path)
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")


def mutate_sidecar_without_manifest_update(root: Path) -> None:
    manifest = first_manifest(root)
    path = root / str(manifest["sidecar_path"])
    value = json.loads(path.read_text())
    value["row_count"] += 1
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def rebind_manifest_to_partial_query(root: Path) -> None:
    first = next((root / "inventory").rglob("*.manifest.json"))
    value = json.loads(first.read_text())
    source = str(value["source"])
    parent = parent_logical_query_id(source, 2018, "2018-02-01", "2018-12-31")
    for path in (root / "inventory").rglob("*.manifest.json"):
        value = json.loads(path.read_text())
        if value["source"] != source or value["query"]["season"] != 2018:
            continue
        value["query"] = {
            "season": 2018,
            "start_date": "2018-02-01",
            "end_date": "2018-12-31",
        }
        value["parent_logical_query_id"] = parent
        path.write_bytes(canonical_json_bytes(value) + b"\n")


if __name__ == "__main__":
    unittest.main()
