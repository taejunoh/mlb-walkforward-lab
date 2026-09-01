from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mlb_backtest.live_top3_contracts import SourceBinding, canonical_json_bytes


TARGET = "2025-05-01"
CUTOFF = "2025-04-30"


@dataclass(frozen=True)
class SourceFixture:
    report: Path
    history_payload: Path
    live_payload: Path


def _schedule(
    *,
    date: str,
    final: bool,
    game_type: str = "R",
    status: str | None = None,
    sport_id: object | None = None,
    league_id: object | None = None,
) -> bytes:
    status = status or ("Final" if final else "Pre-Game")
    away = {"team": {"id": 11, "name": "Away"}}
    home = {"team": {"id": 22, "name": "Home"}}
    if final:
        away.update(score=3, isWinner=False)
        home.update(score=4, isWinner=True)
    game = {
        "gamePk": 1001 if final else 2001,
        "gameType": game_type,
        "officialDate": date,
        "gameDate": f"{date}T19:00:00Z",
        "status": {"detailedState": status},
        "teams": {"away": away, "home": home},
        "venue": {"id": 33, "name": "Park"},
        "doubleHeader": "N",
        "gameNumber": 1,
    }
    if sport_id is not None:
        game["sport"] = {"id": sport_id}
    if league_id is not None:
        game["league"] = {"id": league_id}
    return json.dumps({"dates": [{"date": date, "games": [game]}]}).encode()


def _binding(
    *, role: str, observation_id: str, payload: bytes, payload_relative: Path
) -> SourceBinding:
    if role == "HISTORY_SCHEDULE":
        url = (
            "https://statsapi.mlb.com/api/v1/schedule?sportId=1&gameType=R"
            f"&startDate={CUTOFF[:4]}-01-01&endDate={CUTOFF}"
        )
    else:
        url = (
            "https://statsapi.mlb.com/api/v1/schedule?sportId=1&gameType=R"
            f"&startDate={TARGET}&endDate={TARGET}"
        )
    identity = {"url": url}
    if role == "HISTORY_SCHEDULE":
        identity.update(as_of_date=CUTOFF, start=f"{CUTOFF[:4]}-01-01", end=CUTOFF, gameType="R", role="history")
    else:
        identity["observation_id"] = observation_id
        identity["target_date"] = TARGET
    values: dict[str, object] = {
        "source": "stats_api",
        "binding_role": role,
        "game_pks": [] if role == "HISTORY_SCHEDULE" else [2001],
        "observation_id": observation_id,
        "query_identity_json": canonical_json_bytes(identity).decode().strip(),
        "requested_url": url,
        "final_url": url,
        "response_completed_at_utc": "2025-05-01T10:00:00Z",
        "durable_at_utc": "2025-05-01T10:00:01Z",
        "http_status": 200,
        "content_type": "application/json",
        "etag": None,
        "last_modified": None,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "byte_length": len(payload),
        "payload_relative": payload_relative.as_posix(),
        "manifest_sha256": "0" * 64,
        "receipt_relative": (
            Path("audit/experimental_daily/history_receipts") / f"{observation_id}.json"
            if role == "HISTORY_SCHEDULE"
            else Path("audit/experimental_daily/live_receipts") / f"{observation_id}.json"
        ).as_posix(),
        "receipt_sha256": "0" * 64,
    }
    # These two hashes are patched after all canonical evidence is written.
    values["binding_sha256"] = hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    return SourceBinding(**values)  # type: ignore[arg-type]


def _write(root: Path, relative: Path, contents: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


def _write_evidence(
    root: Path,
    binding: SourceBinding,
    payload: bytes,
    *,
    manifest_overrides: dict[str, object] | None = None,
) -> SourceBinding:
    raw_source = "mlb_schedule" if binding.binding_role == "HISTORY_SCHEDULE" else "mlb_live_schedule"
    identity = json.loads(binding.query_identity_json)
    query_digest = hashlib.sha256(canonical_json_bytes(identity).rstrip(b"\n")).hexdigest()
    payload_digest = hashlib.sha256(payload).hexdigest()
    payload_relative = Path(binding.payload_relative)
    manifest_relative = payload_relative.with_name(
        f"{payload_digest}.{query_digest}.manifest.json"
    )
    manifest = {
        "source": raw_source,
        "query": identity,
        "sha256": payload_digest,
        "retrieved_at_utc": "2025-05-01T10:00:00+00:00",
        "row_count": 1,
        "schema_version": 1,
    }
    if manifest_overrides is not None:
        manifest.update(manifest_overrides)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    _write(root, payload_relative, payload)
    _write(root, manifest_relative, manifest_bytes)
    _write(
        root,
        manifest_relative.with_name(manifest_relative.name[:-5] + ".sha256"),
        f"{manifest_digest}\n".encode(),
    )

    values = asdict(binding)
    values["manifest_sha256"] = manifest_digest
    receipt_relative = Path(binding.receipt_relative)
    receipt = {
        "source": binding.source,
        "binding_role": binding.binding_role,
        "game_pks": list(binding.game_pks),
        "observation_id": binding.observation_id,
        "query_identity_json": binding.query_identity_json,
        "requested_url": binding.requested_url,
        "final_url": binding.final_url,
        "response_completed_at_utc": binding.response_completed_at_utc,
        "http_status": binding.http_status,
        "content_type": binding.content_type,
        "etag": binding.etag,
        "last_modified": binding.last_modified,
        "payload_sha256": payload_digest,
        "byte_length": len(payload),
        "payload_relative": binding.payload_relative,
        "manifest_sha256": manifest_digest,
    }
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    values["receipt_sha256"] = receipt_digest
    values.pop("binding_sha256")
    values["binding_sha256"] = hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    bound = SourceBinding(**values)  # type: ignore[arg-type]

    if binding.binding_role == "HISTORY_SCHEDULE":
        claim = {"receipt": receipt, "receipt_sha256": receipt_digest}
        _write(root, receipt_relative.with_suffix(".claim.json"), canonical_json_bytes(claim))
        _write(root, receipt_relative, receipt_bytes)
        _write(root, receipt_relative.with_suffix(".sha256"), f"{receipt_digest}\n".encode())
        completion = {"receipt_sha256": receipt_digest, "durable_at_utc": bound.durable_at_utc}
    else:
        claim = {
            "schema_version": 1,
            "raw_source": raw_source,
            "source": binding.source,
            "binding_role": binding.binding_role,
            "observation_id": binding.observation_id,
            "query_identity_json": binding.query_identity_json,
            "requested_url": binding.requested_url,
            "final_url": binding.final_url,
            "started_at_utc": binding.response_completed_at_utc,
            "response_completed_at_utc": binding.response_completed_at_utc,
            "http_status": binding.http_status,
            "content_type": binding.content_type,
            "etag": binding.etag,
            "last_modified": binding.last_modified,
            "payload_sha256": payload_digest,
            "byte_length": len(payload),
        }
        _write(
            root,
            Path("audit/experimental_daily/live_observation_claims") / f"{binding.observation_id}.json",
            canonical_json_bytes(claim),
        )
        receipt = {
            **receipt,
            "schema_version": 1,
            "manifest_relative": manifest_relative.as_posix(),
            "claim_relative": (Path("audit/experimental_daily/live_observation_claims") / f"{binding.observation_id}.json").as_posix(),
            "claim_sha256": hashlib.sha256(canonical_json_bytes(claim)).hexdigest(),
            "started_at_utc": binding.response_completed_at_utc,
        }
        receipt.pop("game_pks")
        receipt_bytes = canonical_json_bytes(receipt)
        receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
        values = asdict(bound)
        values["receipt_sha256"] = receipt_digest
        values.pop("binding_sha256")
        values["binding_sha256"] = hashlib.sha256(canonical_json_bytes(values)).hexdigest()
        bound = SourceBinding(**values)  # type: ignore[arg-type]
        _write(root, receipt_relative, receipt_bytes)
        completion = {
            "schema_version": 1,
            "source": bound.source,
            "binding_role": bound.binding_role,
            "observation_id": bound.observation_id,
            "query_identity_json": bound.query_identity_json,
            "receipt_relative": bound.receipt_relative,
            "receipt_sha256": receipt_digest,
            "payload_sha256": payload_digest,
            "manifest_sha256": manifest_digest,
            "durable_at_utc": bound.durable_at_utc,
        }
    completion_bytes = canonical_json_bytes(completion)
    _write(root, receipt_relative.with_suffix(".completion.json"), completion_bytes)
    _write(root, receipt_relative.with_suffix(".completion.sha256"), f"{hashlib.sha256(completion_bytes).hexdigest()}\n".encode())
    return bound


def write_source_fixture(
    root: Path,
    *,
    history_date: str = CUTOFF,
    history_game_type: str = "R",
    history_sport_id: object | None = None,
    history_league_id: object | None = None,
    live_game_type: str = "R",
    live_status: str | None = None,
    history_manifest_overrides: dict[str, object] | None = None,
    live_manifest_overrides: dict[str, object] | None = None,
) -> SourceFixture:
    from tests.test_live_top3_service import _build, _final_observer, _game, _slate

    history_payload = _schedule(
        date=history_date,
        final=True,
        game_type=history_game_type,
        sport_id=history_sport_id,
        league_id=history_league_id,
    )
    live_payload = _schedule(
        date=TARGET, final=False, game_type=live_game_type, status=live_status
    )
    history = _write_evidence(
        root,
        _binding(
            role="HISTORY_SCHEDULE",
            observation_id="history-source",
            payload=history_payload,
            payload_relative=(
                Path("data/experimental_daily/history_cache/mlb_schedule")
                / f"{hashlib.sha256(history_payload).hexdigest()}.raw"
            ),
        ),
        history_payload,
        manifest_overrides=history_manifest_overrides,
    )
    live = _write_evidence(
        root,
        _binding(
            role="LIVE_SCHEDULE",
            observation_id="live-source",
            payload=live_payload,
            payload_relative=(
                Path("data/experimental_daily/live_observations/mlb_live_schedule")
                / f"{hashlib.sha256(live_payload).hexdigest()}.raw"
            ),
        ),
        live_payload,
        manifest_overrides=live_manifest_overrides,
    )
    report = _build(_slate((_game(1),)), _final_observer((_game(1),)))
    report["target_date"] = TARGET
    report["history_cutoff_date"] = CUTOFF
    report["command_inputs"]["date"] = TARGET
    bindings = [
        asdict(history) if binding["binding_role"] == "HISTORY_SCHEDULE" else
        asdict(live) if binding["binding_role"] == "LIVE_SCHEDULE" else binding
        for binding in report["source_bindings"]
    ]
    report["source_bindings"] = bindings
    report["preliminary_top3"] = []
    report["unscored_games"] = []
    report["excluded_games"] = []
    report.pop("content_sha256")
    report["content_sha256"] = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    report_relative = Path("reports/experimental_daily/fixture.json")
    report_path = _write(root, report_relative, canonical_json_bytes(report))
    return SourceFixture(
        report=report_path,
        history_payload=root / history.payload_relative,
        live_payload=root / live.payload_relative,
    )


def _replace_report_binding(
    report_path: Path, role: str, updates: dict[str, object]
) -> None:
    report = json.loads(report_path.read_text())
    bindings = []
    for binding in report["source_bindings"]:
        if binding["binding_role"] != role:
            bindings.append(binding)
            continue
        replacement = {**binding, **updates}
        replacement.pop("binding_sha256")
        replacement["binding_sha256"] = hashlib.sha256(
            canonical_json_bytes(replacement)
        ).hexdigest()
        bindings.append(replacement)
    report["source_bindings"] = bindings
    report.pop("content_sha256")
    report["content_sha256"] = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    report_path.write_bytes(canonical_json_bytes(report))


class TotalsSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_loader_admits_bound_final_history_and_pregame_target(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root)
        bundle = load_totals_sources(self.root, fixture.report.relative_to(self.root))

        self.assertEqual(bundle.target_date, TARGET)
        self.assertEqual(bundle.cutoff_date, CUTOFF)
        self.assertEqual(bundle.history_games[0].game_pk, 1001)
        self.assertEqual(bundle.target_games[0].game_pk, 2001)
        self.assertEqual(bundle.history_identity.payload_relative, fixture.history_payload.relative_to(self.root).as_posix())
        self.assertEqual(bundle.live_identity.payload_relative, fixture.live_payload.relative_to(self.root).as_posix())
        self.assertTrue(
            bundle.live_identity.manifest_sidecar_relative.endswith(".manifest.sha256")
        )
        self.assertEqual(
            hashlib.sha256(
                (self.root / bundle.live_identity.manifest_sidecar_relative).read_bytes()
            ).hexdigest(),
            bundle.live_identity.manifest_sidecar_sha256,
        )

    def test_loader_rejects_history_before_authenticated_start_date(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root, history_date="2024-12-31")

        with self.assertRaisesRegex(ValueError, "history schedule date differs"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rejects_boolean_optional_sport_id(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root, history_sport_id=True)

        with self.assertRaisesRegex(ValueError, "schedule sport differs"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rejects_float_optional_league_id(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root, history_league_id=103.0)

        with self.assertRaisesRegex(ValueError, "schedule league differs"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rejects_boolean_manifest_schema_version(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(
            self.root, history_manifest_overrides={"schema_version": True}
        )

        with self.assertRaisesRegex(ValueError, "schedule manifest differs"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rejects_boolean_manifest_row_count(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(
            self.root, live_manifest_overrides={"row_count": True}
        )

        with self.assertRaisesRegex(ValueError, "schedule manifest differs"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rejects_malformed_manifest_retrieval_time(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(
            self.root, live_manifest_overrides={"retrieved_at_utc": "not-a-timestamp"}
        )

        with self.assertRaisesRegex(ValueError, "schedule manifest differs"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rejects_absolute_report_path(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root)

        with self.assertRaisesRegex(ValueError, "report path"):
            load_totals_sources(self.root, fixture.report)

    def test_loader_rejects_live_sidecars_that_the_contract_does_not_define(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root)
        report = json.loads(fixture.report.read_text())
        live = next(
            item for item in report["source_bindings"] if item["binding_role"] == "LIVE_SCHEDULE"
        )
        receipt = Path(live["receipt_relative"])
        (self.root / receipt.with_suffix(".sha256")).write_bytes(b"0" * 64 + b"\n")

        with self.assertRaisesRegex(ValueError, "unexpected sidecar"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rechecks_late_created_live_receipt_sidecar(self) -> None:
        from mlb_backtest import experimental_totals_sources

        fixture = write_source_fixture(self.root)
        report = json.loads(fixture.report.read_text())
        live = next(
            item for item in report["source_bindings"] if item["binding_role"] == "LIVE_SCHEDULE"
        )
        forbidden = Path(live["receipt_relative"]).with_suffix(".sha256")

        def create_late_sidecar(stage: str, _relative: Path) -> None:
            if stage == "before_final_rebind":
                (self.root / forbidden).write_bytes(b"0" * 64 + b"\n")

        with mock.patch.object(
            experimental_totals_sources,
            "_totals_sources_test_hook",
            side_effect=create_late_sidecar,
        ):
            with self.assertRaisesRegex(ValueError, "receipt sidecar"):
                experimental_totals_sources.load_totals_sources(
                    self.root, fixture.report.relative_to(self.root)
                )

    def test_loader_requires_live_manifest_sidecar(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root)
        report = json.loads(fixture.report.read_text())
        live = next(
            item for item in report["source_bindings"] if item["binding_role"] == "LIVE_SCHEDULE"
        )
        payload = Path(live["payload_relative"])
        query_digest = hashlib.sha256(live["query_identity_json"].encode()).hexdigest()
        manifest = payload.with_name(f"{live['payload_sha256']}.{query_digest}.manifest.json")
        (self.root / manifest.with_name(manifest.name[:-5] + ".sha256")).unlink()

        with self.assertRaisesRegex(ValueError, "schedule manifest"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rejects_authenticated_non_mlb_request(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root)
        report = json.loads(fixture.report.read_text())
        history = next(
            item for item in report["source_bindings"]
            if item["binding_role"] == "HISTORY_SCHEDULE"
        )
        bad_url = history["requested_url"].replace("sportId=1", "sportId=2")
        identity = json.loads(history["query_identity_json"])
        identity["url"] = bad_url
        _replace_report_binding(
            fixture.report,
            "HISTORY_SCHEDULE",
            {
                "requested_url": bad_url,
                "final_url": bad_url,
                "query_identity_json": canonical_json_bytes(identity).decode().strip(),
            },
        )

        with self.assertRaisesRegex(ValueError, "schedule request differs"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_requires_live_binding_population_to_match_parsed_games(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root)
        _replace_report_binding(fixture.report, "LIVE_SCHEDULE", {"game_pks": [9999]})

        with self.assertRaisesRegex(ValueError, "binding game_pks"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rejects_payload_mutation_after_report_validation(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root)
        fixture.history_payload.write_bytes(b"{}")

        with self.assertRaisesRegex(ValueError, "history schedule payload differs"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_requires_one_binding_for_each_schedule_role(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        fixture = write_source_fixture(self.root)
        report = json.loads(fixture.report.read_text())
        history = next(
            item for item in report["source_bindings"]
            if item["binding_role"] == "HISTORY_SCHEDULE"
        )
        report["source_bindings"].append(history)
        report.pop("content_sha256")
        report["content_sha256"] = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
        fixture.report.write_bytes(canonical_json_bytes(report))

        with self.assertRaisesRegex(ValueError, "duplicate observation id|exactly one HISTORY_SCHEDULE"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

    def test_loader_rejects_non_regular_or_postgame_schedule_rows(self) -> None:
        from mlb_backtest.experimental_totals_sources import load_totals_sources

        spring = write_source_fixture(self.root, history_game_type="S")
        with self.assertRaisesRegex(ValueError, "game type"):
            load_totals_sources(self.root, spring.report.relative_to(self.root))

        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temporary.name)
        postgame = write_source_fixture(self.root, live_status="Final")
        with self.assertRaisesRegex(ValueError, "final game requires both scores|not pregame"):
            load_totals_sources(self.root, postgame.report.relative_to(self.root))

    def test_loader_rebinds_every_component_before_returning(self) -> None:
        from mlb_backtest import experimental_totals_sources

        fixture = write_source_fixture(self.root)
        report = json.loads(fixture.report.read_text())
        targets = [fixture.report.relative_to(self.root)]
        for role in ("HISTORY_SCHEDULE", "LIVE_SCHEDULE"):
            binding = next(
                item for item in report["source_bindings"] if item["binding_role"] == role
            )
            receipt = Path(binding["receipt_relative"])
            targets.extend((receipt, receipt.with_suffix(".completion.json"), receipt.with_suffix(".completion.sha256")))
            if role == "HISTORY_SCHEDULE":
                targets.extend((receipt.with_suffix(".claim.json"), receipt.with_suffix(".sha256")))
            else:
                targets.append(
                    Path("audit/experimental_daily/live_observation_claims")
                    / f"{binding['observation_id']}.json"
                )
            payload = Path(binding["payload_relative"])
            query_digest = hashlib.sha256(binding["query_identity_json"].encode()).hexdigest()
            manifest = payload.with_name(f"{binding['payload_sha256']}.{query_digest}.manifest.json")
            targets.extend((payload, manifest))
            targets.append(manifest.with_name(manifest.name[:-5] + ".sha256"))

        for target in targets:
            with self.subTest(component=target):
                fixture = write_source_fixture(self.root)
                mutated = False

                def mutate_after_read(stage: str, relative: Path) -> None:
                    nonlocal mutated
                    if stage == "after_snapshot" and relative == target and not mutated:
                        (self.root / relative).write_bytes(b"{}")
                        mutated = True

                with mock.patch.object(
                    experimental_totals_sources,
                    "_totals_sources_test_hook",
                    side_effect=mutate_after_read,
                ):
                    with self.assertRaises(ValueError):
                        experimental_totals_sources.load_totals_sources(
                            self.root, fixture.report.relative_to(self.root)
                        )
                self.assertTrue(mutated)


if __name__ == "__main__":
    unittest.main()
