"""Sealed, network-free proof of the real Daily Top 3 orchestration.

This deliberately exercises ``run_daily_top3`` instead of assembling a service
payload.  The only seams are the HTTP transport and the nondeterministic clocks
and observation IDs used to authenticate observations.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import multiprocessing
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from mlb_backtest import daily_top3, live_history, live_sources
from mlb_backtest.live_top3_contracts import LiveHttpObservation, canonical_json_bytes


TARGET = "2026-07-21"
FIXTURES = Path(__file__).parent / "fixtures" / "live_top3"
CARD = Path(__file__).parents[1] / "audit" / "v5_1_bt" / "model_card.json"
HISTORY_COMPLETED = "2026-07-21T10:00:01Z"
LIVE_COMPLETED = "2026-07-21T16:00:01Z"
SEALED_SLATE_SHA256 = "2fce5639267743df26cc1bd036dfd275dbd613364c4ffc4fbfd083cced957684"
GOLDEN_CONTENT_SHA256 = "6f03e2ee3be1f214ecf7f262bd6a7f599633efa64caef8c43858cd06c37662f7"
GOLDEN_MARKDOWN_SHA256 = "2db9b30617a719bb7fcc856775970e2db94c2b6948e8c48d2add5204e4de0db4"


class _FixedHistoryDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 7, 21, 10, 0, 2, tzinfo=timezone.utc)
        return instant if tz is not None else instant.replace(tzinfo=None)


def _history_schedule() -> bytes:
    games = []
    for index in range(15):
        games.append({
            "gamePk": 910000 + index,
            "gameType": "R",
            "officialDate": "2026-07-20",
            "gameDate": "2026-07-20T19:00:00Z",
            "status": {"detailedState": "Final"},
            "teams": {
                "away": {"team": {"id": 100 + index, "name": f"Away {index}"}, "score": 1, "isWinner": False},
                "home": {"team": {"id": 200 + index, "name": f"Home {index}"}, "score": 2, "isWinner": True},
            },
            "linescore": {"currentInning": 1, "inningHalf": "Top", "outs": 0},
            "venue": {"id": 1, "name": "Fixture Park"},
            "doubleHeader": "N",
            "gameNumber": 1,
        })
    return json.dumps({"dates": [{"date": "2026-07-20", "games": games}]}).encode()


def _history_statcast(*, poison_target_day: bool = False, flatten_scores: bool = False) -> bytes:
    source = Path(__file__).parent / "fixtures" / "statcast_mini.csv"
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        next(reader)
        template = next(reader)
        fields = reader.fieldnames
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for index in range(15):
        row = dict(template)
        row.update({
            "game_date": "2026-07-20", "game_pk": str(910000 + index),
            "home_team": f"H{index:02}", "away_team": f"A{index:02}",
            "pitcher": str(1000 + index), "batter": str(2000 + index),
        })
        if index == 1 and not flatten_scores:
            row.update({"events": "home_run", "description": "hit_into_play", "woba_value": "2", "woba_denom": "1"})
        elif index == 2 and not flatten_scores:
            row.update({"events": "walk", "description": "ball", "woba_value": "0.7", "woba_denom": "1"})
        elif index == 3 and not flatten_scores:
            row.update({"events": "field_out", "description": "hit_into_play", "woba_value": "0", "woba_denom": "1"})
        writer.writerow(row)
    if poison_target_day:
        poison = dict(template)
        poison.update({"game_date": TARGET, "game_pk": "999999"})
        writer.writerow(poison)
    return output.getvalue().encode()


def _venue(*, valid: bool = True) -> bytes:
    coordinates = {"latitude": 40.0, "longitude": -74.0} if valid else {"latitude": 99.0, "longitude": -74.0}
    return json.dumps({"venues": [{"id": 1, "name": "Fixture Park", "location": {
        "city": "Fixture City", "defaultCoordinates": coordinates,
        "timeZone": {"id": "America/New_York"},
    }}]}).encode()


def _league_era() -> bytes:
    return (Path(__file__).parent / "fixtures" / "mlb_pitching_stats.json").read_bytes()


def _slate(*, variant: str | None = None) -> bytes:
    fixture = json.loads((FIXTURES / "sealed_happy_slate.json").read_bytes())
    games = fixture["dates"][0]["games"]
    if variant == "empty":
        games = []
    if variant == "incomplete":
        return json.dumps({"dates": []}).encode()
    if variant == "target_venue_missing":
        games[0]["venue"] = {"id": 99, "name": "Unknown Park"}
    if variant == "missing_starter":
        del games[0]["teams"]["away"]["probablePitcher"]
    if variant == "unknown_status":
        games[0]["status"]["detailedState"] = "Unknown"
    return json.dumps({"dates": [{"date": TARGET, "games": games}]}).encode()


def _boxscore(index: int, *, conflict: bool = False, partial: bool = False, empty: bool = False, poststart: bool = False) -> bytes:
    if empty:
        return (FIXTURES / "boxscore_pregame_empty_pitchers.json").read_bytes()
    away_pitcher = 9000 + index if conflict else 1000 + index
    home_pitcher = 3000 + index
    def players(base: int, *, abbreviated: bool = False, appearance: bool = False):
        count = 2 if abbreviated else 9
        return {
            f"ID{base + slot}": {"person": {"id": base + slot}, "battingOrder": f"{slot}0{1 if appearance else 0}"}
            for slot in range(1, count + 1)
        }
    value = {"teams": {
        "away": {"team": {"id": 100 + index}, "pitchers": [away_pitcher], "players": players(11000 + index * 20, appearance=poststart)},
        "home": {"team": {"id": 200 + index}, "pitchers": [home_pitcher], "players": players(12000 + index * 20, abbreviated=partial, appearance=poststart)},
    }}
    return json.dumps(value).encode()


def _career(pitcher_id: int) -> bytes:
    # The endpoint is exact through D-1; the parser accepts a MLB split only.
    total = 4000 + pitcher_id
    return json.dumps({"stats": [{"type": {"displayName": "byDateRange"}, "group": {"displayName": "pitching"}, "splits": [
        {"stat": {"battersFaced": total}, "team": {"id": 1}, "sport": {"id": 1}},
        {"stat": {"battersFaced": total}, "sport": {"id": 0}},
    ]}]}).encode()


class SealedLiveFetcher:
    """A strict source URL router: unlisted requests are test failures."""

    def __init__(self, *, variant: str | None = None, redirect: bool = False):
        self.variant = variant
        self.redirect = redirect
        self.current_schedule_calls = 0
        self.calls: list[tuple[str, str]] = []

    def __call__(self, source: str, url: str, *, max_bytes: int) -> LiveHttpObservation:
        self.calls.append((source, url))
        query = parse_qs(urlsplit(url).query)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise AssertionError("live request max_bytes differs")
        completed = LIVE_COMPLETED
        if source == "savant" and "statcast_search" in url:
            if query.get("game_date_gt") != ["2026-07-20"] or query.get("game_date_lt") != ["2026-07-20"]:
                raise AssertionError("Statcast request must be exactly D-1")
            body = _history_statcast(
                poison_target_day=self.variant == "target_poison",
                flatten_scores=self.variant == "constant_statcast",
            )
            completed = HISTORY_COMPLETED
        elif source == "stats_api" and "/schedule" in url and query.get("startDate") != query.get("endDate"):
            if query.get("startDate") != ["2026-01-01"] or query.get("endDate") != ["2026-07-20"] or query.get("gameType") != ["R"]:
                raise AssertionError("history schedule request must end exactly D-1")
            body = _history_schedule()
            completed = HISTORY_COMPLETED
        elif source == "stats_api" and "/venues/" in url:
            if urlsplit(url).path.rsplit("/", 1)[-1] != "1" or query.get("hydrate") != ["location"]:
                raise AssertionError("venue request identity differs")
            body = _venue(valid=self.variant != "invalid_venue")
            completed = HISTORY_COMPLETED
        elif source == "stats_api" and "/api/v1/stats?" in url:
            if query.get("startDate") != ["2026-07-20"] or query.get("endDate") != ["2026-07-20"] or query.get("group") != ["pitching"]:
                raise AssertionError("league ERA request must end exactly D-1")
            body = _league_era()
            completed = HISTORY_COMPLETED
        elif source == "stats_api" and "/people/" in url and "/stats" in url:
            pitcher = int(urlsplit(url).path.split("/")[4])
            if query.get("endDate") != ["2026-07-20"] or query.get("startDate") != ["1876-01-01"]:
                raise AssertionError("career BF request must end exactly D-1")
            body = _career(pitcher)
        elif source == "stats_api" and "/game/" in url and "/boxscore" in url:
            game_pk = int(urlsplit(url).path.split("/")[4])
            index = game_pk - 880001
            if self.variant == "empty_pitchers" and index == 0:
                body = json.dumps({"teams": {
                    "away": {"team": {"id": 100}, "pitchers": [], "players": {}},
                    "home": {"team": {"id": 200}, "pitchers": [], "players": {}},
                }}).encode()
            else:
                body = _boxscore(
                    index, conflict=index == 4, partial=index == 5,
                    poststart=self.variant == "poststart_pitchers" and index == 0,
                )
        elif source == "stats_api" and "/schedule" in url:
            if query.get("startDate") != [TARGET] or query.get("endDate") != [TARGET] or query.get("gameType") != ["R"]:
                raise AssertionError("current schedule request identity differs")
            self.current_schedule_calls += 1
            body = _slate(variant=self.variant)
            if self.variant == "changed_append_only" and self.current_schedule_calls > 1:
                # Same fixed ID must never replace this append-only observation.
                altered = json.loads(body)
                altered["dates"][0]["games"][0]["gameNumber"] = 2
                body = json.dumps(altered).encode()
            if self.variant in {
                "final_start_conflict", "final_identity_conflict", "final_status_conflict",
            } and self.current_schedule_calls > 1:
                altered = json.loads(body)
                first_game = altered["dates"][0]["games"][0]
                if self.variant == "final_start_conflict":
                    first_game["gameDate"] = "2026-07-21T23:10:00Z"
                elif self.variant == "final_identity_conflict":
                    first_game["teams"]["away"]["team"]["name"] = "Changed Away"
                else:
                    first_game["status"]["detailedState"] = "Delayed"
                body = json.dumps(altered).encode()
        else:
            raise AssertionError(f"unsealed request: {source} {url}")
        final_url = "https://evil.invalid/redirect" if self.redirect else url
        if self.variant == "after_start" and completed == LIVE_COMPLETED:
            completed = "2026-07-21T23:05:00Z"
        return LiveHttpObservation(
            requested_url=url, final_url=final_url,
            started_at_utc=completed.replace(":01Z", ":00Z"),
            response_completed_at_utc=completed, http_status=200,
            content_type="application/json", etag='"sealed"', last_modified=None,
            body=body,
        )


class OfflineCurrentFetcher:
    """A cache-reuse probe that permits no network request of any kind."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, source: str, url: str, *, max_bytes: int) -> LiveHttpObservation:
        del max_bytes
        self.calls.append((source, url))
        raise URLError("sealed network is offline")


def _fixed_ids():
    next_id = 0
    def allocate() -> str:
        nonlocal next_id
        next_id += 1
        return f"{next_id:032x}"
    return allocate


def _canonical_payload(payload: dict[str, object]) -> bytes:
    # Report locations depend on the temporary root but must never enter payload.
    return canonical_json_bytes(payload)


def _report_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.glob("reports/experimental_daily/**/*")))


def _assert_result_free(test: unittest.TestCase, value: object) -> None:
    forbidden = {"score", "winner", "winning", "outcome", "result", "odds", "market"}
    if isinstance(value, dict):
        for key, nested in value.items():
            # Normalize snake_case, camelCase (MLB's ``isWinner``), and any
            # punctuation-delimited variant before checking semantic tokens.
            split_camel = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
            tokens = {
                token.lower()
                for token in re.split(r"[^A-Za-z0-9]+", split_camel)
                if token
            }
            test.assertTrue(
                key == "predicted_winner" or not (forbidden & tokens),
                f"result-bearing output field: {key}",
            )
            _assert_result_free(test, nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_result_free(test, nested)


def _fixture_worker(root: str, queue) -> None:
    """Use a fresh interpreter: trusted-root snapshots must be process-local."""
    try:
        payload, json_path, markdown_path, fetcher = DailyTop3EndToEndTests().run_fixture(Path(root))
    except BaseException as error:  # pragma: no cover - returned to parent assertion
        queue.put(("error", type(error).__name__, str(error)))
    else:
        queue.put((
            "ok", canonical_json_bytes(payload), str(json_path), str(markdown_path),
            fetcher.current_schedule_calls,
        ))
class DailyTop3EndToEndTests(unittest.TestCase):
    def run_fixture(
        self, root: Path, *, variant: str | None = None, redirect: bool = False,
        tamper_card: bool = False, component_weight_tamper: bool = False,
        fetcher: SealedLiveFetcher | None = None, durable_at_start: bool = False,
    ):
        card = root / "audit" / "v5_1_bt" / "model_card.json"
        card.parent.mkdir(parents=True, exist_ok=True)
        if not card.exists():
            shutil.copyfile(CARD, card)
        if tamper_card:
            card.write_bytes(card.read_bytes().replace(b'"beta": 0.612', b'"beta": 0.613'))
        if component_weight_tamper:
            card.write_bytes(card.read_bytes().replace(b'"starter": 0.35', b'"starter": 0.34'))
        fetcher = fetcher or SealedLiveFetcher(variant=variant, redirect=redirect)
        fixed_id = _fixed_ids()

        def durable():
            if durable_at_start:
                return datetime(2026, 7, 21, 23, 5, tzinfo=timezone.utc)
            return datetime(2026, 7, 21, 16, 0, 2, tzinfo=timezone.utc)

        slate_defaults = dict(live_sources.observe_live_slate.__kwdefaults__ or {})
        final_defaults = dict(live_sources.observe_final_game_schedule.__kwdefaults__ or {})

        with (
            mock.patch.object(live_sources, "new_observation_id", side_effect=fixed_id),
            mock.patch.object(live_history, "_canonical_utc_now", return_value="2026-07-21T17:00:02Z"),
            mock.patch.object(live_history, "datetime", _FixedHistoryDateTime),
        ):
            # ``daily_top3`` retains its imported production observers.  Their
            # public defaults are redirected only to deterministic ID/clock seams.
            slate_defaults.update(
                observation_id_factory=live_sources.new_observation_id,
                durable_clock=durable,
            )
            final_defaults.update(
                observation_id_factory=live_sources.new_observation_id,
                durable_clock=durable,
            )
            with (
                mock.patch.object(live_sources.observe_live_slate, "__kwdefaults__", slate_defaults),
                mock.patch.object(live_sources.observe_final_game_schedule, "__kwdefaults__", final_defaults),
            ):
                result = daily_top3.run_daily_top3(
                    root, TARGET, Path("audit/v5_1_bt/model_card.json"), fetcher=fetcher,
                    utc_clock=lambda: datetime(2026, 7, 21, 16, tzinfo=timezone.utc),
                )
        return (*result, fetcher)

    def test_fixture_e2e_is_outcome_isolated_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            workers = [
                context.Process(target=_fixture_worker, args=(str(base / name), queue))
                for name in ("one", "two")
            ]
            try:
                for worker in workers:
                    worker.start()
                results = [queue.get(timeout=30) for _worker in workers]
                for worker in workers:
                    worker.join(timeout=30)
                    self.assertEqual(worker.exitcode, 0)
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                    worker.join(timeout=5)
                queue.close()
                queue.join_thread()
            self.assertTrue(all(result[0] == "ok" for result in results), results)
            first, second = (result[1] for result in results)
            self.assertEqual(first, second)
            payload = json.loads(first)
            first_json = Path(results[0][2])
            first_markdown = Path(results[0][3])
            second_json = Path(results[1][2])
            second_markdown = Path(results[1][3])

            self.assertEqual(payload["verified_top3"], [])
            self.assertEqual(len(payload["preliminary_top3"]), 3)
            self.assertEqual(len(payload["unscored_games"]), 2)
            self.assertEqual(len(payload["excluded_games"]), 1)
            self.assertEqual([row["rank"] for row in payload["preliminary_top3"]], [1, 2, 3])
            self.assertEqual(
                [row["game_pk"] for row in payload["preliminary_top3"]],
                [880003, 880002, 880004],
            )
            self.assertNotIn(
                880001, {row["game_pk"] for row in payload["preliminary_top3"]}
            )
            self.assertEqual(
                [row["pick_probability"] for row in payload["preliminary_top3"]],
                [0.5337954055378902, 0.5327084991552588, 0.531692991320826],
            )
            self.assertEqual(
                [row["starter_edge_abs"] for row in payload["preliminary_top3"]],
                [1.506376631040581, 1.3027996827358095, 1.1126482172581404],
            )
            self.assertEqual(payload["content_sha256"], GOLDEN_CONTENT_SHA256)
            self.assertEqual(
                {
                    row["game_pk"]: (row["state"], tuple(row["reason_codes"]))
                    for row in payload["unscored_games"]
                },
                {
                    880005: ("UNSCORED", ("STARTER_CONFLICT",)),
                    880006: ("UNSCORED", ("INVALID_HOME_LINEUP",)),
                },
            )
            self.assertEqual(
                {
                    row["game_pk"]: (row["state"], tuple(row["reason_codes"]))
                    for row in payload["excluded_games"]
                },
                {880007: ("EXCLUDED", ("PREGAME_STATUS_NOT_ACCEPTED",))},
            )
            without_digest = dict(payload)
            without_digest["content_sha256"] = ""
            without_digest.pop("content_sha256")
            self.assertEqual(
                payload["content_sha256"],
                hashlib.sha256(canonical_json_bytes(without_digest)).hexdigest(),
            )
            self.assertTrue(first_json.is_file())
            self.assertTrue(first_markdown.is_file())
            self.assertTrue(second_json.is_file())
            self.assertTrue(second_markdown.is_file())
            self.assertEqual(json.loads(first_json.read_bytes()), payload)
            self.assertEqual(first_json.read_bytes(), canonical_json_bytes(payload))
            self.assertEqual(second_json.read_bytes(), canonical_json_bytes(payload))
            self.assertEqual(first_markdown.read_bytes(), daily_top3.render_live_top3_markdown(payload))
            self.assertEqual(hashlib.sha256(first_markdown.read_bytes()).hexdigest(), GOLDEN_MARKDOWN_SHA256)
            self.assertEqual(first_json.name, f"20260721T160000Z-{GOLDEN_CONTENT_SHA256[:16]}.json")
            self.assertEqual(first_markdown.name, f"20260721T160000Z-{GOLDEN_CONTENT_SHA256[:16]}.md")
            self.assertEqual([result[4] for result in results], [8, 8])
            _assert_result_free(self, payload)
            for row in (*payload["unscored_games"], *payload["excluded_games"]):
                self.assertNotIn("pick_probability", row)
                self.assertNotIn("rank", row)
            for binding in payload["source_bindings"]:
                self.assertIsInstance(binding["payload_relative"], str)
                self.assertIsInstance(binding["receipt_relative"], str)
                for field in ("payload_relative", "receipt_relative"):
                    binding_path = PurePosixPath(binding[field])
                    self.assertFalse(binding_path.is_absolute())
                    self.assertNotIn("..", binding_path.parts)
                    self.assertEqual(binding[field], binding_path.as_posix())
                    self.assertEqual(str(binding_path), binding[field])
                query_identity = json.loads(binding["query_identity_json"])
                _assert_result_free(self, query_identity)
                root = first_json.parents[3]
                payload_path = root / binding["payload_relative"]
                receipt_path = root / binding["receipt_relative"]
                self.assertEqual(hashlib.sha256(payload_path.read_bytes()).hexdigest(), binding["payload_sha256"])
                self.assertEqual(payload_path.stat().st_size, binding["byte_length"])
                self.assertEqual(hashlib.sha256(receipt_path.read_bytes()).hexdigest(), binding["receipt_sha256"])
                manifests = [
                    path for path in payload_path.parent.glob("*.manifest.json")
                    if hashlib.sha256(path.read_bytes()).hexdigest() == binding["manifest_sha256"]
                ]
                self.assertEqual(len(manifests), 1)
            for result in results:
                self.assertTrue(Path(result[2]).resolve().is_relative_to(base.resolve()))
                self.assertTrue(Path(result[3]).resolve().is_relative_to(base.resolve()))
            self.assertEqual(
                hashlib.sha256((FIXTURES / "sealed_happy_slate.json").read_bytes()).hexdigest(),
                SEALED_SLATE_SHA256,
            )

    def test_result_field_normalization_rejects_mlb_and_semantic_variants(self):
        rejected = (
            "home_score", "actual_winner", "market_odds", "isWinner",
            "winning_team", "game_result", "predictedWinner",
        )
        for key in rejected:
            with self.subTest(key=key), self.assertRaisesRegex(AssertionError, "result-bearing"):
                _assert_result_free(self, {key: "forbidden"})
        _assert_result_free(self, {"predicted_winner": "permitted"})
        with self.assertRaisesRegex(AssertionError, "result-bearing"):
            _assert_result_free(self, {"query_identity_json": {"isWinner": True}})

    def test_global_integrity_failures_write_no_ranking_report(self):
        cases = (
            ("card digest", {"tamper_card": True}, ValueError, "digest", False),
            ("durable receipt at scheduled start", {"durable_at_start": True}, ValueError, "durable at or after", True),
            ("target-day Statcast poison", {"variant": "target_poison"}, ValueError, "same-day leakage", True),
            ("invalid history venue", {"variant": "invalid_venue"}, ValueError, "coordinates", True),
            ("incomplete current schedule", {"variant": "incomplete"}, ValueError, "scheduled games", True),
            ("source-host redirect", {"redirect": True}, ValueError, "authority", True),
            ("response at start cannot backdate durable receipt", {"variant": "after_start"}, ValueError, "durable time precedes", True),
            ("no scheduled games", {"variant": "empty"}, ValueError, "scheduled games", True),
        )
        for label, options, error_type, pattern, should_fetch in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "root"
                fetcher = SealedLiveFetcher(
                    variant=options.get("variant"), redirect=options.get("redirect", False)
                )
                options = {key: value for key, value in options.items() if key not in {"variant", "redirect"}}
                with self.assertRaisesRegex(error_type, pattern):
                    self.run_fixture(root, fetcher=fetcher, **options)
                self.assertEqual(bool(fetcher.calls), should_fetch)
                self.assertEqual(tuple(root.glob("reports/experimental_daily/**/*.json")), ())
                self.assertEqual(tuple(root.glob("reports/experimental_daily/**/*.md")), ())

    def test_component_weight_byte_tamper_is_stopped_at_card_digest_boundary(self):
        """The fixed card digest prevents reaching semantic weight projection.

        Re-signing this deliberately frozen card is not an E2E seam.  The real
        path must therefore reject the changed ``starter`` component byte before
        history or current observations, while focused card tests cover a
        digest-authenticated semantic mismatch.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            with self.assertRaisesRegex(ValueError, "digest"):
                self.run_fixture(root, component_weight_tamper=True)
            self.assertEqual(_report_files(root), ())

    def test_authenticated_history_raw_manifest_and_receipt_tamper_abort_before_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            for kind in ("payload_relative", "manifest", "receipt_relative"):
                with self.subTest(kind=kind):
                    root = Path(temporary) / kind
                    payload, json_path, markdown_path, _fetcher = self.run_fixture(root)
                    original_reports = {
                        path: path.read_bytes() for path in (json_path, markdown_path)
                    }
                    binding = next(
                        item for item in payload["source_bindings"]
                        if item["binding_role"] == "HISTORY_SCHEDULE"
                    )
                    if kind == "manifest":
                        source = root / binding["payload_relative"]
                        target = next(source.parent.glob("*.manifest.json"))
                    else:
                        target = root / binding[kind]
                    target.write_bytes(b"tampered authenticated fixture bytes")
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.run_fixture(root)
                    self.assertEqual(
                        {path: path.read_bytes() for path in original_reports},
                        original_reports,
                    )

    def test_changed_append_only_current_observation_rejects_without_overwriting_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            _payload, json_path, markdown_path, _fetcher = self.run_fixture(root)
            original_reports = {path: path.read_bytes() for path in (json_path, markdown_path)}
            with self.assertRaises((ValueError, RuntimeError)):
                self.run_fixture(root, variant="changed_append_only")
            self.assertEqual(
                {path: path.read_bytes() for path in original_reports}, original_reports
            )

    def test_d1_cache_reuses_authenticated_history_but_never_falls_back_to_current_slate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            _payload, json_path, markdown_path, _fetcher = self.run_fixture(root)
            original_reports = {path: path.read_bytes() for path in (json_path, markdown_path)}
            offline = OfflineCurrentFetcher()
            with self.assertRaises(URLError):
                self.run_fixture(root, fetcher=offline)
            self.assertEqual(len(offline.calls), 1)
            _source, attempted_url = offline.calls[0]
            self.assertIn("/schedule?", attempted_url)
            self.assertIn("startDate=2026-07-21", attempted_url)
            self.assertEqual(
                {path: path.read_bytes() for path in original_reports}, original_reports
            )

    def test_same_run_identity_conflicts_without_overwriting_report_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            _payload, json_path, markdown_path, _fetcher = self.run_fixture(root)
            original_reports = {path: path.read_bytes() for path in (json_path, markdown_path)}
            with self.assertRaises(FileExistsError):
                self.run_fixture(root)
            self.assertEqual(
                {path: path.read_bytes() for path in original_reports}, original_reports
            )

    def test_golden_ranking_rejects_constant_statcast_mutation(self):
        """A constant scorer/history cannot accidentally satisfy the Top 3 golden."""
        with tempfile.TemporaryDirectory() as temporary:
            baseline, _json_path, _markdown_path, _fetcher = self.run_fixture(
                Path(temporary) / "baseline"
            )
            mutated, _json_path, _markdown_path, _fetcher = self.run_fixture(
                Path(temporary) / "mutated", variant="constant_statcast"
            )
            baseline_probabilities = [row["pick_probability"] for row in baseline["preliminary_top3"]]
            self.assertGreater(len(set(baseline_probabilities)), 1)
            self.assertNotEqual(
                [row["game_pk"] for row in baseline["preliminary_top3"]],
                [row["game_pk"] for row in mutated["preliminary_top3"]],
            )

    def test_exact_career_bf_totals_replace_raw_starter_counts_once(self):
        """Recording wrapper observes the real adapter; it does not change scoring."""
        from mlb_backtest import live_top3_service

        observed: list[dict[str, object]] = []
        real_builder = live_top3_service.build_live_raw_game_features

        def recording_builder(*args, **kwargs):
            raw = real_builder(*args, **kwargs)
            observed.append(raw)
            return raw

        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            live_top3_service,
            "build_live_raw_game_features",
            side_effect=recording_builder,
        ):
            self.run_fixture(Path(temporary) / "root")
        self.assertEqual([row["game_pk"] for row in observed], [880001, 880002, 880003, 880004])
        for index, raw in enumerate(observed):
            self.assertEqual(raw["away"]["starter"]["career_bf"], 5000 + index)
            self.assertEqual(raw["home"]["starter"]["career_bf"], 7000 + index)

    def test_candidate_local_failures_preserve_other_scoreable_games(self):
        cases = (
            ("target venue absent", "target_venue_missing", "unscored_games", "MISSING_TARGET_VENUE_EVIDENCE"),
            ("empty pitcher lists", "empty_pitchers", "unscored_games", "STARTER_CONFLICT"),
            ("post-start appearance order", "poststart_pitchers", "unscored_games", "INVALID_AWAY_LINEUP"),
            ("final start conflict", "final_start_conflict", "excluded_games", "SCHEDULED_START_CHANGED"),
            ("final identity conflict", "final_identity_conflict", "excluded_games", "GAME_IDENTITY_CHANGED"),
            ("final status conflict", "final_status_conflict", "excluded_games", "PREGAME_STATUS_NOT_ACCEPTED"),
            ("missing schedule starter", "missing_starter", "unscored_games", "STARTER_CONFLICT"),
            ("unknown schedule status", "unknown_status", "excluded_games", "PREGAME_STATUS_NOT_ACCEPTED"),
        )
        for label, variant, population, reason in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                payload, json_path, markdown_path, _fetcher = self.run_fixture(
                    Path(temporary) / "root", variant=variant
                )
                self.assertTrue(json_path.is_file())
                self.assertTrue(markdown_path.is_file())
                self.assertEqual(len(payload["preliminary_top3"]), 3)
                affected = next(row for row in payload[population] if row["game_pk"] == 880001)
                self.assertIn(reason, affected["reason_codes"])
                self.assertNotIn("rank", affected)
                self.assertNotIn("pick_probability", affected)


if __name__ == "__main__":
    unittest.main()
