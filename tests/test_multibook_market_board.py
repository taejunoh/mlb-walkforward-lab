from __future__ import annotations

import tempfile
import unittest
import json
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from mlb_backtest.market_play_contracts import canonical_json_bytes, content_digest, sidecar_bytes
from mlb_backtest.multibook_market_board import (
    BOARD_SCHEMA_V1,
    BOARD_SCHEMA_V2,
    SCHEDULE_SCHEMA,
    load_market_board,
    load_schedule_authority,
    normalize_market_board,
    persist_market_board,
)
from mlb_backtest.multibook_market_capture import (
    SOURCE_HOSTS,
    UNAVAILABLE_SCHEMA,
    admit_raw_market_capture,
    record_unavailable_book,
)
from mlb_backtest import daily_multibook_market_plays as daily
from mlb_backtest.market_play_authorities import (
    MlAuthority, MlGameAuthority, ParticipantGateEvidence, TotalsAuthority,
    TotalsGameAuthority,
)
from mlb_backtest.multibook_ml_evaluator import evaluate_moneylines
from tests.test_multibook_market_capture import raw_capture


class BoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.tmp.name)
        schedule: dict[str, object] = {
            "schema": SCHEDULE_SCHEMA,
            "content_sha256": "",
            "target_date": "2026-09-02",
            "games": [
                {"game_pk": 1, "away_team": "AAA", "home_team": "BBB", "scheduled_start_utc": "2026-09-02T19:00:00Z"},
                {"game_pk": 2, "away_team": "CCC", "home_team": "DDD", "scheduled_start_utc": "2026-09-02T23:00:00Z"},
            ],
        }
        schedule["content_sha256"] = ""
        schedule["content_sha256"] = content_digest(schedule)
        rel = Path("audit/experimental_multibook_market/schedule/2026-09-02.json")
        path = self.root / rel
        path.parent.mkdir(parents=True)
        raw = canonical_json_bytes(schedule)
        path.write_bytes(raw)
        path.with_suffix(".sha256").write_bytes(sidecar_bytes(raw))
        self.schedule_relative = rel
        self.outcomes: list[Path] = []
        for book in SOURCE_HOSTS:
            payload = raw_capture(book)
            payload["content_sha256"] = ""
            payload["content_sha256"] = content_digest(payload)
            raw_bytes = canonical_json_bytes(payload)
            rel_raw = Path("data/experimental_multibook_market/raw") / f"{payload['content_sha256']}.json"
            raw_path = self.root / rel_raw
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw_bytes)
            raw_path.with_suffix(".sha256").write_bytes(sidecar_bytes(raw_bytes))
            self.outcomes.append(admit_raw_market_capture(self.root, rel_raw))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _persist_body(self, body: dict[str, object]) -> Path:
        body["content_sha256"] = content_digest({**body, "content_sha256": ""})
        board_bytes = canonical_json_bytes(body)
        board_path = Path("data/experimental_multibook_market/boards") / f"{body['content_sha256']}.json"
        board_file = self.root / board_path
        board_file.parent.mkdir(parents=True, exist_ok=True)
        board_file.write_bytes(board_bytes)
        board_file.with_suffix(".sha256").write_bytes(sidecar_bytes(board_bytes))
        return board_path

    def _unavailable_outcome(self) -> Path:
        record = {
            "schema": UNAVAILABLE_SCHEMA,
            "source_book": "FANATICS",
            "recorded_at_utc": "2026-09-02T18:00:00Z",
            "reason": "SOURCE_UNAVAILABLE",
            "detail": "attempted collector failed",
        }
        record_path = self.root / "audit/collector/fanatics-unavailable.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_bytes(canonical_json_bytes(record))
        return record_unavailable_book(self.root, record_path.relative_to(self.root))

    def _ml_authority(self, loaded):
        return MlAuthority(
            tuple(
                MlGameAuthority(
                    game.game_pk,
                    game.scheduled_start_utc,
                    game.away_team,
                    game.home_team,
                    "home",
                    Fraction("0.60"),
                    Fraction("0.60"),
                    ParticipantGateEvidence(True, True, False, ()),
                    "SCORED",
                    None,
                )
                for game in loaded.games
            ),
            loaded.target_date,
            Path("ml.trace.json"),
            self.root,
            (),
        )

    def test_board_maps_five_book_events_and_keeps_zero_quote_games(self):
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes)
        self.assertEqual([g["game_pk"] for g in built.board["games"]], [1, 2])
        self.assertEqual(len(built.board["capture_outcomes"]), 5)
        self.assertEqual(built.board["games"][-1]["moneylines"], [])
        persist_market_board(self.root, built)
        schedule = load_schedule_authority(self.root, self.schedule_relative)
        board_path = Path("data/experimental_multibook_market/boards") / f"{built.board['content_sha256']}.json"
        loaded = load_market_board(self.root, board_path, schedule)
        self.assertEqual(len(loaded.games), 2)
        self.assertEqual(daily._board_authority(self.root, board_path).games, loaded.games)

    def test_default_board_is_v3_and_records_authenticated_match_evidence(self):
        """Catches reverting default intake to V2 or dropping V3 evidence."""
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1])
        self.assertEqual(built.board["schema"], "PROVISIONAL_MULTIBOOK_MARKET_BOARD_V3")
        self.assertEqual(len(built.board["capture_outcome_bindings"]), 1)
        self.assertEqual(built.board["event_match_audit"][0]["rule"], "EXACT")

    def test_v3_loader_rejects_digest_valid_projection_mutations(self):
        """Catches accepting an edited audit, embedded outcome, binding, or quote."""
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1])
        mutations = (
            lambda body: body["event_match_audit"][0].__setitem__("delta_nanoseconds", 1),
            lambda body: body["capture_outcomes"][0].__setitem__("event_count", 2),
            lambda body: body["capture_outcome_bindings"][0].__setitem__("capture_outcome_sha256", "0" * 64),
            lambda body: body["games"][0]["moneylines"][0].__setitem__("away_american_odds", -105),
        )
        schedule = load_schedule_authority(self.root, self.schedule_relative)
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                body = json.loads(json.dumps(built.board))
                mutate(body)
                board_path = self._persist_body(body)
                with self.assertRaises(ValueError):
                    load_market_board(self.root, board_path, schedule)

    def test_v3_loader_rejects_resealed_empty_evidence_collections(self):
        """Catches treating a digest-valid empty V3 board as a valid projection."""
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1])
        body = json.loads(json.dumps(built.board))
        body["capture_outcomes"] = []
        body["capture_outcome_bindings"] = []
        body["event_match_audit"] = []
        for game in body["games"]:
            game["moneylines"] = []
            game["totals"] = []
        board_path = self._persist_body(body)
        with self.assertRaises(ValueError):
            load_market_board(self.root, board_path, load_schedule_authority(self.root, self.schedule_relative))

    def test_v3_loader_reopens_bound_outcome_and_raw_sidecars(self):
        """Catches treating board-embedded source evidence as authoritative."""
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1])
        board_path, _ = persist_market_board(self.root, built)
        outcome_relative = Path(built.board["capture_outcome_bindings"][0]["capture_outcome_relative"])
        raw_relative = Path(built.board["capture_outcomes"][0]["raw_capture_relative"])
        schedule = load_schedule_authority(self.root, self.schedule_relative)
        for relative in (outcome_relative.with_suffix(".sha256"), raw_relative.with_suffix(".sha256")):
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(b"0" * 64 + b"\n")
                with self.assertRaises(ValueError):
                    load_market_board(self.root, board_path, schedule)
                path.write_bytes(original)
        raw_path = self.root / raw_relative
        original_raw = raw_path.read_bytes()
        raw_path.write_bytes(original_raw + b" ")
        with self.assertRaises(ValueError):
            load_market_board(self.root, board_path, schedule)

    def test_v3_loaded_authority_rebinds_authenticated_raw_capture(self):
        """Catches dropping raw snapshots from the authority persistence chain."""
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1])
        board_path, _ = persist_market_board(self.root, built)
        loaded = load_market_board(self.root, board_path, load_schedule_authority(self.root, self.schedule_relative))
        raw_relative = Path(built.board["capture_outcomes"][0]["raw_capture_relative"])
        raw_path = self.root / raw_relative
        raw_path.write_bytes(raw_path.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            loaded.rebind()

    def test_v3_loaded_authority_rebinds_resealed_schedule_outcome_and_raw_evidence(self):
        """Catches retaining only board bytes after the V3 authority is loaded."""
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1])
        board_path, _ = persist_market_board(self.root, built)
        loaded = load_market_board(self.root, board_path, load_schedule_authority(self.root, self.schedule_relative))
        evidence = (
            self.schedule_relative,
            Path(built.board["capture_outcome_bindings"][0]["capture_outcome_relative"]),
            Path(built.board["capture_outcomes"][0]["raw_capture_relative"]),
        )
        for relative in evidence:
            with self.subTest(relative=relative):
                path = self.root / relative
                sidecar = path.with_suffix(".sha256")
                original, original_sidecar = path.read_bytes(), sidecar.read_bytes()
                resealed = original + b" "
                path.write_bytes(resealed)
                sidecar.write_bytes(sidecar_bytes(resealed))
                with self.assertRaises(ValueError):
                    loaded.rebind()
                if relative == self.schedule_relative:
                    fresh_schedule = load_schedule_authority(self.root, self.schedule_relative)
                    with self.assertRaises(ValueError):
                        load_market_board(self.root, board_path, fresh_schedule)
                path.write_bytes(original)
                sidecar.write_bytes(original_sidecar)
                sidecar.write_bytes(original_sidecar + b" ")
                with self.assertRaises(ValueError):
                    loaded.rebind()
                sidecar.write_bytes(original_sidecar)

    def test_v3_loader_rejects_resealed_outcome_and_raw_byte_changes(self):
        """Catches byte-level source changes even when each changed sidecar is resealed."""
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1])
        board_path, _ = persist_market_board(self.root, built)
        evidence = (
            Path(built.board["capture_outcome_bindings"][0]["capture_outcome_relative"]),
            Path(built.board["capture_outcomes"][0]["raw_capture_relative"]),
        )
        for relative in evidence:
            with self.subTest(relative=relative):
                path = self.root / relative
                sidecar = path.with_suffix(".sha256")
                original, original_sidecar = path.read_bytes(), sidecar.read_bytes()
                resealed = original + b" "
                path.write_bytes(resealed)
                sidecar.write_bytes(sidecar_bytes(resealed))
                with self.assertRaises(ValueError):
                    load_market_board(self.root, board_path, load_schedule_authority(self.root, self.schedule_relative))
                path.write_bytes(original)
                sidecar.write_bytes(original_sidecar)

    def test_v3_daily_bundle_round_trips_and_final_rebind_rejects_raw_mutation(self):
        """Catches dropping V3 raw evidence from daily final-persistence rebinding."""
        built_board = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1])
        board_path, _ = persist_market_board(self.root, built_board)
        ml_trace, totals_trace = Path("input/ml.trace.json"), Path("input/totals.trace.json")
        for relative in (ml_trace, totals_trace):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}")

        class MutableMl(MlAuthority):
            mutate = False
            raw_path: Path

            def rebind(self) -> None:
                if type(self).mutate:
                    type(self).raw_path.write_bytes(type(self).raw_path.read_bytes() + b" ")

        ml = MutableMl(
            tuple(
                MlGameAuthority(
                    game_pk, start, away, home, "home", Fraction("0.60"), Fraction("0.60"),
                    ParticipantGateEvidence(True, True, False, ()), "SCORED", None,
                )
                for game_pk, away, home, start in (
                    (1, "AAA", "BBB", "2026-09-02T19:00:00Z"),
                    (2, "CCC", "DDD", "2026-09-02T23:00:00Z"),
                )
            ),
            "2026-09-02", ml_trace, self.root, (),
        )
        totals = TotalsAuthority(
            tuple(
                TotalsGameAuthority(game_pk, start, away, home, None, (), "UNPROJECTED")
                for game_pk, away, home, start in (
                    (1, "AAA", "BBB", "2026-09-02T19:00:00Z"),
                    (2, "CCC", "DDD", "2026-09-02T23:00:00Z"),
                )
            ),
            "2026-09-02", totals_trace, self.root, (),
        )
        MutableMl.raw_path = self.root / Path(built_board.board["capture_outcomes"][0]["raw_capture_relative"])
        with (
            patch("mlb_backtest.market_play_authorities.load_ml_authority", return_value=ml),
            patch("mlb_backtest.market_play_authorities.load_totals_authority", return_value=totals),
        ):
            built = daily.build_market_plays(
                self.root, ml_trace, totals_trace, board_path,
                evaluated_clock=lambda: "2026-09-02T18:00:00Z",
            )
            _, report, _, trace = daily.persist_market_plays(self.root, built)
            self.assertTrue((self.root / report).is_file())
            self.assertTrue((self.root / trace).is_file())
            rebuilt = daily.build_market_plays(
                self.root, ml_trace, totals_trace, board_path,
                evaluated_clock=lambda: "2026-09-02T18:00:00Z",
            )
            MutableMl.mutate = True
            with self.assertRaises(ValueError):
                daily.persist_market_plays(self.root, rebuilt)

    def test_v1_board_rejects_missing_book_outcomes(self):
        with self.assertRaises(ValueError):
            normalize_market_board(self.root, self.schedule_relative, self.outcomes[:4], board_schema=BOARD_SCHEMA_V1)

    def test_v2_board_rejects_duplicate_book_outcomes(self):
        with self.assertRaises(ValueError):
            normalize_market_board(self.root, self.schedule_relative, (*self.outcomes[:4], self.outcomes[0]))

    def test_v2_board_orders_only_supplied_books_deterministically(self):
        shuffled = (self.outcomes[2], self.outcomes[0], self.outcomes[4])
        built = normalize_market_board(self.root, self.schedule_relative, shuffled, board_schema=BOARD_SCHEMA_V2)
        self.assertEqual(
            [item["source_book"] for item in built.board["capture_outcomes"]],
            ["FANDUEL", "BETMGM", "FANATICS"],
        )

    def test_v2_board_rejects_empty_outcomes(self):
        with self.assertRaises(ValueError):
            normalize_market_board(self.root, self.schedule_relative, ())

    def test_v1_board_round_trips_complete_legacy_schema(self):
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes, board_schema=BOARD_SCHEMA_V1)
        persist_market_board(self.root, built)
        schedule = load_schedule_authority(self.root, self.schedule_relative)
        board_path = Path("data/experimental_multibook_market/boards") / f"{built.board['content_sha256']}.json"
        loaded = load_market_board(self.root, board_path, schedule)
        self.assertEqual(loaded.board_snapshot.contents, built.board_bytes)

    def test_v1_board_rejects_incomplete_stored_body(self):
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes, board_schema=BOARD_SCHEMA_V1)
        body = dict(built.board)
        body["capture_outcomes"] = body["capture_outcomes"][:1]
        body["content_sha256"] = content_digest({**body, "content_sha256": ""})
        board_bytes = canonical_json_bytes(body)
        board_path = Path("data/experimental_multibook_market/boards") / f"{body['content_sha256']}.json"
        board_file = self.root / board_path
        board_file.parent.mkdir(parents=True, exist_ok=True)
        board_file.write_bytes(board_bytes)
        board_file.with_suffix(".sha256").write_bytes(sidecar_bytes(board_bytes))
        schedule = load_schedule_authority(self.root, self.schedule_relative)
        with self.assertRaises(ValueError):
            load_market_board(self.root, board_path, schedule)

    def test_v2_board_accepts_one_actual_book_outcome(self):
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1], board_schema=BOARD_SCHEMA_V2)
        self.assertEqual(len(built.board["capture_outcomes"]), 1)

    def test_v2_board_persists_and_daily_consumer_loads_one_outcome(self):
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1], board_schema=BOARD_SCHEMA_V2)
        board_path, _ = persist_market_board(self.root, built)
        loaded = daily._board_authority(self.root, board_path)
        self.assertEqual(loaded.board_snapshot.contents, built.board_bytes)
        self.assertEqual([quote.book_id for quote in loaded.games[0].moneylines], ["FANDUEL"])
        self.assertEqual([quote.book_id for quote in loaded.games[0].totals], ["FANDUEL"])
        self.assertEqual(loaded.games[1].moneylines, ())
        rows = evaluate_moneylines(
            self._ml_authority(loaded), loaded, evaluated_at="2026-09-02T13:00:59Z"
        )
        self.assertEqual([row["decision"] for row in rows], ["PLAY", "PASS"])

    def test_v2_board_unavailable_outcome_has_zero_quotes(self):
        unavailable = self._unavailable_outcome()
        built = normalize_market_board(self.root, self.schedule_relative, (unavailable,), board_schema=BOARD_SCHEMA_V2)
        board_path, _ = persist_market_board(self.root, built)
        loaded = daily._board_authority(self.root, board_path)
        self.assertTrue(all(not game.moneylines and not game.totals for game in loaded.games))
        rows = evaluate_moneylines(self._ml_authority(loaded), loaded, evaluated_at="2026-09-02T18:59:59Z")
        self.assertEqual([row["decision"] for row in rows], ["PASS", "PASS"])
        self.assertTrue(all("NO_QUOTE" in row["reason_codes"] for row in rows))

    def test_v3_unavailable_outcome_keeps_every_game_with_zero_quotes(self):
        unavailable = self._unavailable_outcome()
        built = normalize_market_board(self.root, self.schedule_relative, (unavailable,))
        self.assertEqual(built.board["event_match_audit"], [])
        self.assertEqual(len(built.board["capture_outcome_bindings"]), 1)
        board_path, _ = persist_market_board(self.root, built)
        loaded = daily._board_authority(self.root, board_path)
        self.assertTrue(all(not game.moneylines and not game.totals for game in loaded.games))
        rows = evaluate_moneylines(self._ml_authority(loaded), loaded, evaluated_at="2026-09-02T18:59:59Z")
        self.assertEqual([row["decision"] for row in rows], ["PASS", "PASS"])
        self.assertTrue(all("NO_QUOTE" in row["reason_codes"] for row in rows))

    def test_each_supplied_count_has_canonical_order_independent_bytes(self):
        for count in range(2, 6):
            with self.subTest(count=count):
                forward = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:count])
                reverse = normalize_market_board(self.root, self.schedule_relative, tuple(reversed(self.outcomes[:count])))
                self.assertEqual(forward.board_bytes, reverse.board_bytes)

    def test_board_schema_json_has_v1_v2_v3_count_and_evidence_conditions(self):
        schema = json.loads((Path(__file__).parents[1] / "schemas/experimental_multibook_market_board.schema.json").read_text())
        validator = Draft202012Validator(schema)
        v1 = normalize_market_board(self.root, self.schedule_relative, self.outcomes, board_schema=BOARD_SCHEMA_V1).board
        v2 = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1], board_schema=BOARD_SCHEMA_V2).board
        v3 = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1]).board
        self.assertEqual(list(validator.iter_errors(v1)), [])
        self.assertEqual(list(validator.iter_errors(v2)), [])
        self.assertEqual(list(validator.iter_errors(v3)), [])
        v1_subset = dict(v1)
        v1_subset["capture_outcomes"] = v1_subset["capture_outcomes"][:1]
        self.assertTrue(list(validator.iter_errors(v1_subset)))
        v2_empty = dict(v2)
        v2_empty["capture_outcomes"] = []
        self.assertTrue(list(validator.iter_errors(v2_empty)))
        v2_six = dict(v2)
        v2_six["capture_outcomes"] = v2_six["capture_outcomes"] * 6
        self.assertTrue(list(validator.iter_errors(v2_six)))
        v3_missing_audit = dict(v3)
        v3_missing_audit.pop("event_match_audit")
        self.assertTrue(list(validator.iter_errors(v3_missing_audit)))
        v1_with_v3_evidence = dict(v1)
        v1_with_v3_evidence["event_match_audit"] = []
        self.assertTrue(list(validator.iter_errors(v1_with_v3_evidence)))

    def test_load_rejects_unknown_board_schema_even_with_recomputed_digest(self):
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes[:1])
        body = dict(built.board)
        body["schema"] = "PROVISIONAL_MULTIBOOK_MARKET_BOARD_V9"
        board_path = self._persist_body(body)
        schedule = load_schedule_authority(self.root, self.schedule_relative)
        with self.assertRaises(ValueError):
            load_market_board(self.root, board_path, schedule)


if __name__ == "__main__":
    unittest.main()
