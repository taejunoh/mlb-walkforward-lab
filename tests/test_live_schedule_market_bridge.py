from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from mlb_backtest import daily_multibook_market_plays as daily
from mlb_backtest.live_sources import LiveHttpObservation, live_schedule_url, observe_live_slate
from mlb_backtest.live_top3_contracts import canonical_json_bytes as live_canonical_json_bytes
from mlb_backtest.market_play_contracts import canonical_json_bytes, content_digest, sidecar_bytes
from mlb_backtest.multibook_market_board import (
    load_market_board,
    load_schedule_authority,
    normalize_market_board,
    persist_market_board,
)
from mlb_backtest.multibook_market_capture import admit_raw_market_capture
from mlb_backtest.multibook_market_capture import UNAVAILABLE_SCHEMA, record_unavailable_book
from mlb_backtest.market_play_authorities import (
    MlAuthority,
    MlGameAuthority,
    ParticipantGateEvidence,
    TotalsAuthority,
    TotalsGameAuthority,
)
from mlb_backtest.multibook_ml_evaluator import evaluate_moneylines
from tests.test_multibook_market_capture import raw_capture


class LiveScheduleMarketBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.tmp.name)
        self.target_date = "2026-09-02"
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/live_top3/schedule_complete.json").read_text()
        )
        fixture["dates"][0]["date"] = self.target_date
        game = fixture["dates"][0]["games"][0]
        game.update(
            {
                "gamePk": 1,
                "officialDate": self.target_date,
                "gameDate": "2026-09-02T19:00:00Z",
                "status": {"detailedState": "Final"},
            }
        )
        game["teams"]["away"]["team"] = {"id": 110, "name": "AAA"}
        game["teams"]["home"]["team"] = {"id": 120, "name": "BBB"}
        second = json.loads(json.dumps(game))
        second.update(
            {
                "gamePk": 2,
                "gameDate": "2026-09-02T23:00:00Z",
                "status": {"detailedState": "In Progress"},
            }
        )
        second["teams"]["away"]["team"] = {"id": 130, "name": "CCC"}
        second["teams"]["home"]["team"] = {"id": 140, "name": "DDD"}
        fixture["dates"][0]["games"].append(second)
        request = live_schedule_url(self.target_date, self.target_date)

        def fetcher(_source, url, *, max_bytes):
            self.assertEqual(url, request)
            return LiveHttpObservation(
                requested_url=url,
                final_url=url,
                started_at_utc="2026-09-02T12:00:00Z",
                response_completed_at_utc="2026-09-02T12:00:01Z",
                http_status=200,
                content_type="application/json",
                etag='"fixture"',
                last_modified=None,
                body=canonical_json_bytes(fixture),
            )

        observed = observe_live_slate(
            self.root,
            self.target_date,
            fetcher=fetcher,
            observation_id_factory=lambda: "market-bridge-live-schedule",
            durable_clock=lambda: datetime(2026, 9, 2, 12, 0, 2, tzinfo=timezone.utc),
        )
        self.schedule_relative = Path(observed.source_bindings[0].receipt_relative)
        receipt = json.loads((self.root / self.schedule_relative).read_text())
        self.live_raw = Path(receipt["payload_relative"])
        capture = raw_capture("FANDUEL")
        capture["content_sha256"] = content_digest({**capture, "content_sha256": ""})
        raw = canonical_json_bytes(capture)
        capture_relative = Path("data/experimental_multibook_market/raw") / f"{capture['content_sha256']}.json"
        capture_path = self.root / capture_relative
        capture_path.parent.mkdir(parents=True)
        capture_path.write_bytes(raw)
        capture_path.with_suffix(".sha256").write_bytes(sidecar_bytes(raw))
        self.outcome = admit_raw_market_capture(self.root, capture_relative)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rewrite_json(self, relative: Path, mutate) -> None:
        path = self.root / relative
        value = json.loads(path.read_text())
        mutate(value)
        path.write_bytes(canonical_json_bytes(value))

    def test_admitted_live_schedule_receipt_loads_and_round_trips_one_book_board(self):
        schedule = load_schedule_authority(self.root, self.schedule_relative)
        self.assertEqual(schedule.target_date, self.target_date)
        self.assertEqual([game["game_pk"] for game in schedule.games], [1, 2])

        built = normalize_market_board(self.root, self.schedule_relative, (self.outcome,))
        board_path, _ = persist_market_board(self.root, built)
        loaded = load_market_board(self.root, board_path, schedule)
        self.assertEqual([quote.book_id for quote in loaded.games[0].moneylines], ["FANDUEL"])
        self.assertEqual(daily._board_authority(self.root, board_path).games, loaded.games)

    def test_live_schedule_rejects_receipt_identity_mutations(self):
        original = (self.root / self.schedule_relative).read_bytes()
        for field, value in (
            ("source", "savant"),
            ("binding_role", "FINAL_SCHEDULE"),
            ("http_status", 500),
            ("final_url", "https://statsapi.mlb.com/api/v1/schedule?wrong=1"),
            ("query_identity_json", "{}"),
        ):
            with self.subTest(field=field):
                self._rewrite_json(self.schedule_relative, lambda receipt, f=field, v=value: receipt.__setitem__(f, v))
                with self.assertRaises(ValueError):
                    load_schedule_authority(self.root, self.schedule_relative)
                (self.root / self.schedule_relative).write_bytes(original)

    def test_live_schedule_rejects_malformed_and_duplicate_game_ids(self):
        raw = json.loads((self.root / self.live_raw).read_text())
        for observation_id, mutate in (
            ("duplicate-live-schedule", lambda games: games.append(dict(games[0]))),
            ("malformed-live-schedule", lambda games: games[0].pop("gamePk")),
        ):
            with self.subTest(observation_id=observation_id):
                payload = json.loads(json.dumps(raw))
                mutate(payload["dates"][0]["games"])

                def fetcher(_source, url, *, max_bytes):
                    return LiveHttpObservation(
                        requested_url=url, final_url=url,
                        started_at_utc="2026-09-02T12:00:00Z",
                        response_completed_at_utc="2026-09-02T12:00:01Z",
                        http_status=200, content_type="application/json",
                        etag='"fixture"', last_modified=None,
                        body=canonical_json_bytes(payload),
                    )

                with self.assertRaises(ValueError):
                    observe_live_slate(
                        self.root, self.target_date, fetcher=fetcher,
                        observation_id_factory=lambda value=observation_id: value,
                        durable_clock=lambda: datetime(2026, 9, 2, 12, 0, 2, tzinfo=timezone.utc),
                    )
                with self.assertRaises(ValueError):
                    load_schedule_authority(
                        self.root,
                        Path("audit/experimental_daily/live_receipts") / f"{observation_id}.json",
                    )

    def test_live_schedule_rejects_chain_mutations(self):
        receipt = json.loads((self.root / self.schedule_relative).read_text())
        mutations = (
            Path(receipt["payload_relative"]),
            Path(receipt["manifest_relative"]),
            Path(receipt["manifest_relative"]).with_suffix(".sha256"),
            Path(receipt["claim_relative"]),
            self.schedule_relative.with_name(f"{self.schedule_relative.stem}.completion.json"),
            self.schedule_relative.with_name(f"{self.schedule_relative.stem}.completion.sha256"),
        )
        for relative in mutations:
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaises(ValueError):
                    load_schedule_authority(self.root, self.schedule_relative)
                path.write_bytes(original)

    def test_live_schedule_rejects_canonical_wrong_date_query_against_manifest(self):
        receipt_path = self.root / self.schedule_relative
        receipt = json.loads(receipt_path.read_text())
        wrong_date = "2026-09-03"
        wrong_url = live_schedule_url(wrong_date, wrong_date)
        wrong_query = {
            "observation_id": receipt["observation_id"],
            "target_date": wrong_date,
            "url": wrong_url,
        }
        receipt["query_identity_json"] = live_canonical_json_bytes(wrong_query).decode().removesuffix("\n")
        receipt["requested_url"] = wrong_url
        receipt["final_url"] = wrong_url
        query_sha = hashlib.sha256(receipt["query_identity_json"].encode()).hexdigest()
        original_manifest = self.root / receipt["manifest_relative"]
        manifest = Path("data/experimental_daily/live_observations/mlb_live_schedule") / f"{receipt['payload_sha256']}.{query_sha}.manifest.json"
        copied_manifest = self.root / manifest
        copied_manifest.parent.mkdir(parents=True, exist_ok=True)
        copied_manifest.write_bytes(original_manifest.read_bytes())
        copied_manifest.with_suffix(".sha256").write_bytes(original_manifest.with_suffix(".sha256").read_bytes())
        receipt["manifest_relative"] = manifest.as_posix()
        receipt_path.write_bytes(live_canonical_json_bytes(receipt))
        with self.assertRaisesRegex(ValueError, "raw manifest query differs"):
            load_schedule_authority(self.root, self.schedule_relative)

    def test_persist_rejects_postbuild_input_mutation(self):
        built = normalize_market_board(self.root, self.schedule_relative, (self.outcome,))
        receipt = json.loads((self.root / self.schedule_relative).read_text())
        raw = self.root / receipt["payload_relative"]
        raw.write_bytes(raw.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            persist_market_board(self.root, built)

    def test_load_rejects_stored_schedule_receipt_sha_mismatch(self):
        built = normalize_market_board(self.root, self.schedule_relative, (self.outcome,))
        body = dict(built.board)
        body["schedule_receipt_sha256"] = "0" * 64
        body["content_sha256"] = content_digest({**body, "content_sha256": ""})
        board_bytes = canonical_json_bytes(body)
        board_relative = Path("data/experimental_multibook_market/boards") / f"{body['content_sha256']}.json"
        board = self.root / board_relative
        board.parent.mkdir(parents=True, exist_ok=True)
        board.write_bytes(board_bytes)
        board.with_suffix(".sha256").write_bytes(sidecar_bytes(board_bytes))
        with self.assertRaises(ValueError):
            load_market_board(
                self.root, board_relative,
                load_schedule_authority(self.root, self.schedule_relative),
            )

    def test_load_rejects_digest_valid_live_board_game_identity_change(self):
        built = normalize_market_board(self.root, self.schedule_relative, (self.outcome,))
        body = dict(built.board)
        body["games"] = [dict(game) for game in body["games"]]
        body["games"][0]["home_team"] = "WRONG"
        body["content_sha256"] = content_digest({**body, "content_sha256": ""})
        board_bytes = canonical_json_bytes(body)
        board_relative = Path("data/experimental_multibook_market/boards") / f"{body['content_sha256']}.json"
        board = self.root / board_relative
        board.parent.mkdir(parents=True, exist_ok=True)
        board.write_bytes(board_bytes)
        board.with_suffix(".sha256").write_bytes(sidecar_bytes(board_bytes))
        with self.assertRaisesRegex(ValueError, "game differs from schedule"):
            load_market_board(
                self.root, board_relative,
                load_schedule_authority(self.root, self.schedule_relative),
            )

    def test_daily_persist_rejects_live_raw_mutated_after_rebuild(self):
        built_board = normalize_market_board(self.root, self.schedule_relative, (self.outcome,))
        board_path, _ = persist_market_board(self.root, built_board)
        ml_trace = Path("input/ml.trace.json")
        totals_trace = Path("input/totals.trace.json")
        for relative in (ml_trace, totals_trace):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")

        class MutableMlAuthority(MlAuthority):
            raw: Path
            rebinds = 0
            def rebind(self) -> None:
                type(self).rebinds += 1
                if type(self).rebinds == 2:
                    type(self).raw.write_bytes(type(self).raw.read_bytes() + b" ")

        class Totals(TotalsAuthority):
            def rebind(self) -> None:
                return None

        MutableMlAuthority.raw = self.root / self.live_raw
        ml = MutableMlAuthority(
            (MlGameAuthority(
                1, "2026-09-02T19:00:00Z", "AAA", "BBB", "home",
                Fraction("0.60"), Fraction("0.60"),
                ParticipantGateEvidence(True, True, False, ()), "SCORED", None,
            ), MlGameAuthority(
                2, "2026-09-02T23:00:00Z", "CCC", "DDD", "home",
                Fraction("0.60"), Fraction("0.60"),
                ParticipantGateEvidence(True, True, False, ()), "SCORED", None,
            )), "2026-09-02", ml_trace, self.root, (),
        )
        totals = Totals(
            (
                TotalsGameAuthority(1, "2026-09-02T19:00:00Z", "AAA", "BBB", None, (), "UNPROJECTED"),
                TotalsGameAuthority(2, "2026-09-02T23:00:00Z", "CCC", "DDD", None, (), "UNPROJECTED"),
            ),
            "2026-09-02", totals_trace, self.root, (),
        )
        with (
            patch("mlb_backtest.market_play_authorities.load_ml_authority", return_value=ml),
            patch("mlb_backtest.market_play_authorities.load_totals_authority", return_value=totals),
        ):
            built = daily.build_market_plays(
                self.root, ml_trace, totals_trace, board_path,
                evaluated_clock=lambda: "2026-09-02T18:00:00Z",
            )
            with self.assertRaisesRegex(ValueError, "regular file snapshot mismatch"):
                daily.persist_market_plays(self.root, built)

    def test_live_schedule_unavailable_book_persists_daily_zero_quote_pass(self):
        unavailable = {
            "schema": UNAVAILABLE_SCHEMA,
            "source_book": "FANATICS",
            "recorded_at_utc": "2026-09-02T18:00:00Z",
            "reason": "SOURCE_UNAVAILABLE",
            "detail": "fixture collector failed",
        }
        record = self.root / "audit/collector/fanatics-unavailable.json"
        record.parent.mkdir(parents=True)
        record.write_bytes(canonical_json_bytes(unavailable))
        outcome = record_unavailable_book(self.root, record.relative_to(self.root))
        built = normalize_market_board(self.root, self.schedule_relative, (outcome,))
        board_path, _ = persist_market_board(self.root, built)
        board = daily._board_authority(self.root, board_path)
        ml = MlAuthority(
            (MlGameAuthority(
                1, "2026-09-02T19:00:00Z", "AAA", "BBB", "home",
                Fraction("0.60"), Fraction("0.60"),
                ParticipantGateEvidence(True, True, False, ()), "SCORED", None,
            ), MlGameAuthority(
                2, "2026-09-02T23:00:00Z", "CCC", "DDD", "home",
                Fraction("0.60"), Fraction("0.60"),
                ParticipantGateEvidence(True, True, False, ()), "SCORED", None,
            )), "2026-09-02", Path("ml.trace.json"), self.root, (),
        )
        row = evaluate_moneylines(ml, board, evaluated_at="2026-09-02T18:30:00Z")[0]
        self.assertEqual(row["decision"], "PASS")
        self.assertIn("NO_QUOTE", row["reason_codes"])

    def test_daily_build_and_persist_round_trip_uses_live_schedule_board(self):
        built_board = normalize_market_board(self.root, self.schedule_relative, (self.outcome,))
        board_path, _ = persist_market_board(self.root, built_board)
        ml_trace, totals_trace = Path("input/ml.trace.json"), Path("input/totals.trace.json")
        for relative in (ml_trace, totals_trace):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")
        ml = MlAuthority(
            (
                MlGameAuthority(1, "2026-09-02T19:00:00Z", "AAA", "BBB", "home", Fraction("0.60"), Fraction("0.60"), ParticipantGateEvidence(True, True, False, ()), "SCORED", None),
                MlGameAuthority(2, "2026-09-02T23:00:00Z", "CCC", "DDD", "home", Fraction("0.60"), Fraction("0.60"), ParticipantGateEvidence(True, True, False, ()), "SCORED", None),
            ), "2026-09-02", ml_trace, self.root, (),
        )
        totals = TotalsAuthority(
            (
                TotalsGameAuthority(1, "2026-09-02T19:00:00Z", "AAA", "BBB", None, (), "UNPROJECTED"),
                TotalsGameAuthority(2, "2026-09-02T23:00:00Z", "CCC", "DDD", None, (), "UNPROJECTED"),
            ), "2026-09-02", totals_trace, self.root, (),
        )
        with (
            patch("mlb_backtest.market_play_authorities.load_ml_authority", return_value=ml),
            patch("mlb_backtest.market_play_authorities.load_totals_authority", return_value=totals),
        ):
            built = daily.build_market_plays(self.root, ml_trace, totals_trace, board_path, evaluated_clock=lambda: "2026-09-02T18:00:00Z")
            _, report, _, trace = daily.persist_market_plays(self.root, built)
        self.assertTrue((self.root / report).is_file())
        self.assertTrue((self.root / trace).is_file())


if __name__ == "__main__":
    unittest.main()
