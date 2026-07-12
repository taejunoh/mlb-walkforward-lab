import json
import unittest
import tempfile
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import mlb_backtest.statcast as statcast_module
from mlb_backtest.manifest import RawStore
from mlb_backtest.statcast import (
    MAX_STATCAST_BYTES,
    StatcastData,
    derive_outs_recorded,
    fetch_monthly,
    fetch_statcast_stage,
    identify_pitcher_roles,
    parse_statcast,
    regular_season_ranges,
    statcast_url,
)


FIXTURE = Path(__file__).parent / "fixtures" / "statcast_mini.csv"


class StatcastTest(unittest.TestCase):
    @staticmethod
    def _payload_with_rows(day, row_count):
        lines = FIXTURE.read_bytes().splitlines()
        row = lines[1].replace(b"2024-04-01", day.encode("ascii"))
        return b"\n".join([lines[0], *([row] * row_count)]) + b"\n"

    def test_silent_row_capped_range_is_split_before_publication(self):
        calls = []

        def fetcher(url, **_kwargs):
            query = parse_qs(urlsplit(url).query)
            start = query["game_date_gt"][0]
            end = query["game_date_lt"][0]
            calls.append((start, end))
            if start != end:
                return self._payload_with_rows(start, 2)
            return self._payload_with_rows(start, 1)

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            statcast_module, "STATCAST_ROW_CAP", 2, create=True
        ):
            artifacts = fetch_monthly(
                RawStore(Path(td)),
                "2024-04-01",
                "2024-04-02",
                fetcher=fetcher,
            )
            queries = [
                row["manifest"]["query"]
                for row in RawStore(Path(td)).verify_manifests()
            ]

        self.assertEqual(
            calls,
            [
                ("2024-04-01", "2024-04-02"),
                ("2024-04-01", "2024-04-01"),
                ("2024-04-02", "2024-04-02"),
            ],
        )
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(
            sorted((row["start"], row["end"]) for row in queries),
            [("2024-04-01", "2024-04-01"), ("2024-04-02", "2024-04-02")],
        )

    def test_resume_splits_sealed_capped_exact_manifest_without_refetching_parent(self):
        parent_query = {
            "start": "2024-04-01",
            "end": "2024-04-02",
            "game_type": "R",
            "url": statcast_url("2024-04-01", "2024-04-02"),
        }
        calls = []

        def fetcher(url, **_kwargs):
            query = parse_qs(urlsplit(url).query)
            start = query["game_date_gt"][0]
            end = query["game_date_lt"][0]
            calls.append((start, end))
            self.assertEqual(start, end)
            return self._payload_with_rows(start, 1)

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            statcast_module, "STATCAST_ROW_CAP", 2, create=True
        ):
            store = RawStore(Path(td))
            sealed_parent = store.put_unique_query(
                "baseball_savant_statcast",
                parent_query,
                self._payload_with_rows("2024-04-01", 2),
            )
            known = {
                json.dumps(parent_query, separators=(",", ":"), sort_keys=True):
                    sealed_parent.sha256
            }
            artifacts = fetch_monthly(
                store,
                "2024-04-01",
                "2024-04-02",
                known_queries=known,
                fetcher=fetcher,
            )
            catalog = store.verify_manifests()

        self.assertEqual(
            calls,
            [("2024-04-01", "2024-04-01"), ("2024-04-02", "2024-04-02")],
        )
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(len(catalog), 3)
        self.assertIn(sealed_parent.sha256, [row["manifest"]["sha256"] for row in catalog])

    def test_oversized_month_is_split_deterministically_until_chunks_fit(self):
        payload = FIXTURE.read_bytes()
        calls = []

        def fetcher(url, *, max_bytes):
            query = parse_qs(urlsplit(url).query)
            start = query["game_date_gt"][0]
            end = query["game_date_lt"][0]
            calls.append((start, end, max_bytes))
            if (start, end) == ("2024-04-01", "2024-04-30"):
                raise ValueError(f"response exceeds {max_bytes} bytes")
            return payload.replace(b"2024-04-01", start.encode("ascii")).replace(
                b"2024-04-02", start.encode("ascii")
            )

        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            artifacts = fetch_monthly(
                store,
                "2024-04-01",
                "2024-04-30",
                fetcher=fetcher,
            )
            queries = sorted(
                (
                    row["manifest"]["query"]["start"],
                    row["manifest"]["query"]["end"],
                )
                for row in RawStore(Path(td)).verify_manifests()
            )
            known = {
                json.dumps(row["manifest"]["query"], separators=(",", ":"), sort_keys=True): row["manifest"]["sha256"]
                for row in RawStore(Path(td)).verify_manifests()
            }
            resumed = fetch_monthly(
                store,
                "2024-04-01",
                "2024-04-30",
                known_queries=known,
                fetcher=lambda *_args, **_kwargs: self.fail("split resume should not fetch"),
            )

        self.assertEqual(len(artifacts), 2)
        self.assertEqual(len(resumed), 2)
        self.assertEqual(
            queries,
            [("2024-04-01", "2024-04-15"), ("2024-04-16", "2024-04-30")],
        )
        self.assertEqual(calls[0], ("2024-04-01", "2024-04-30", MAX_STATCAST_BYTES))
    def test_url_uses_official_regular_season_detail_inclusive_dates(self):
        url = statcast_url("2025-05-01", "2025-05-31")
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "https://baseballsavant.mlb.com/statcast_search/csv",
        )
        self.assertEqual(query["game_date_gt"], ["2025-05-01"])
        self.assertEqual(query["game_date_lt"], ["2025-05-31"])
        self.assertEqual(query["type"], ["details"])
        self.assertEqual(query["hfGT"], ["R|"])

    def test_parser_keeps_pitches_and_deduplicates_plate_appearances(self):
        parsed = parse_statcast(FIXTURE)
        self.assertEqual(len(parsed.pitches), 7)
        self.assertEqual(len(parsed.plate_appearances), 6)
        pa = next(row for row in parsed.plate_appearances if row["event_id"] == "1001:1")
        self.assertEqual(pa["events"], "strikeout")
        self.assertEqual(pa["pitch_number"], 2)
        self.assertEqual(pa["outs_when_up"], 0)
        self.assertEqual(pa["home_score"], 0)
        self.assertEqual(pa["away_score"], 0)

    def test_path_csv_is_streamed_without_calling_read_bytes(self):
        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("Path CSV must be streamed"),
        ):
            parsed = parse_statcast(FIXTURE)

        self.assertEqual(len(parsed.pitches), 7)

    def test_disk_unique_iterator_peak_memory_does_not_scale_with_row_count(self):
        disk_unique = getattr(
            statcast_module, "iter_unique_statcast_pitches_on_disk", None
        )
        self.assertIsNotNone(disk_unique)

        def peak_bytes(row_count):
            def rows():
                for pitch_number in range(1, row_count + 1):
                    yield {
                        "game_pk": 1,
                        "at_bat_number": pitch_number,
                        "pitch_number": 1,
                        "events": f"event-{pitch_number}",
                    }

            tracemalloc.start()
            try:
                for _row in disk_unique(rows()):
                    pass
                return tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()

        small_peak = peak_bytes(10_000)
        large_peak = peak_bytes(100_000)

        self.assertLess(large_peak - small_peak, 2 * 1024 * 1024)
        self.assertLess(large_peak, max(4 * small_peak, 512 * 1024))

    def test_truncated_pa_records_a_pitch_event_but_not_a_completed_pa(self):
        text = FIXTURE.read_text("utf-8-sig").replace(
            "strikeout,swinging_strike", "truncated_pa,swinging_strike", 1
        )
        malformed = FIXTURE.parent / "statcast_truncated.tmp.csv"
        try:
            malformed.write_text(text, encoding="utf-8")
            parsed = parse_statcast(malformed)
            self.assertTrue(any(row["events"] == "truncated_pa" for row in parsed.pitches))
            self.assertFalse(
                any(row["events"] == "truncated_pa" for row in parsed.plate_appearances)
            )
        finally:
            malformed.unlink(missing_ok=True)

    def test_blank_event_hit_by_pitch_is_still_a_completed_pa(self):
        text = FIXTURE.read_text("utf-8-sig").replace(
            "hit_by_pitch,hit_by_pitch", ",hit_by_pitch", 1
        )
        malformed = FIXTURE.parent / "statcast_blank_hbp.tmp.csv"
        try:
            malformed.write_text(text, encoding="utf-8")
            parsed = parse_statcast(malformed)
            pa = next(
                row
                for row in parsed.plate_appearances
                if row["game_pk"] == 1002 and row["at_bat_number"] == 2
            )
            self.assertEqual(pa["events"], "hit_by_pitch")
            self.assertEqual(pa["description"], "hit_by_pitch")
            self.assertEqual(len(parsed.plate_appearances), 6)
        finally:
            malformed.unlink(missing_ok=True)

    def test_roles_use_first_pitcher_for_each_fielding_side(self):
        parsed = parse_statcast(FIXTURE)
        roles = identify_pitcher_roles(parsed.pitches)
        self.assertTrue(roles[(1001, 11)]["is_starter"])
        self.assertTrue(roles[(1001, 21)]["is_starter"])
        self.assertTrue(roles[(1001, 12)]["is_reliever"])
        self.assertEqual(roles[(1001, 11)]["team"], "HOM")
        self.assertEqual(roles[(1001, 21)]["team"], "AWY")

    def test_parser_is_deterministic_under_input_permutation(self):
        lines = FIXTURE.read_text("utf-8-sig").splitlines()
        shuffled = FIXTURE.parent / "statcast_shuffled.tmp.csv"
        try:
            body = lines[:1] + list(reversed(lines[1:]))
            shuffled.write_text("\n".join(body) + "\n", encoding="utf-8")
            self.assertEqual(parse_statcast(FIXTURE), parse_statcast(shuffled))
        finally:
            shuffled.unlink(missing_ok=True)

    def test_pitch_dedup_collapses_exact_and_rejects_conflicts(self):
        lines = FIXTURE.read_text("utf-8-sig").splitlines()
        duplicate = FIXTURE.parent / "statcast_duplicate.tmp.csv"
        conflict = FIXTURE.parent / "statcast_conflict.tmp.csv"
        try:
            duplicate.write_text(
                "\n".join(lines + [lines[1]]) + "\n", encoding="utf-8"
            )
            self.assertEqual(parse_statcast(FIXTURE), parse_statcast(duplicate))
            changed = lines[1].replace("95.0", "94.9")
            conflict.write_text(
                "\n".join(lines + [changed]) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "conflicting duplicate pitch"):
                parse_statcast(conflict)
        finally:
            duplicate.unlink(missing_ok=True)
            conflict.unlink(missing_ok=True)

    def test_missing_required_field_is_rejected(self):
        text = FIXTURE.read_text("utf-8-sig").replace("pitch_type,", "", 1)
        malformed = FIXTURE.parent / "statcast_bad.tmp.csv"
        try:
            malformed.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing required fields.*pitch_type"):
                parse_statcast(malformed)
        finally:
            malformed.unlink(missing_ok=True)

    def test_outs_when_up_is_a_required_pre_pitch_state(self):
        text = FIXTURE.read_text("utf-8-sig")
        malformed = FIXTURE.parent / "statcast_bad.tmp.csv"
        try:
            malformed.write_text(
                text.replace("outs_when_up", "outs_when_up_x", 1), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValueError, "missing required fields.*outs_when_up"
            ):
                parse_statcast(malformed)
            malformed.write_text(text.replace(",0,R\n", ",-1,R\n", 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outs_when_up must be nonnegative"):
                parse_statcast(malformed)
            malformed.write_text(text.replace(",0,R\n", ",3,R\n", 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outs_when_up must be at most 2"):
                parse_statcast(malformed)
        finally:
            malformed.unlink(missing_ok=True)

    def test_outs_are_derived_from_every_pitch_state_not_event_labels(self):
        def pitch(
            number,
            *,
            at_bat,
            pitcher,
            outs,
            event="",
            inning=1,
            half="Top",
        ):
            return {
                "game_date": "2025-05-01",
                "game_pk": 778098,
                "pitcher_id": pitcher,
                "batter_id": 100 + at_bat,
                "events": event,
                "inning": inning,
                "inning_topbot": half,
                "at_bat_number": at_bat,
                "pitch_number": number,
                "outs_when_up": outs,
                "event_id": f"778098:{at_bat}",
            }

        rows = (
            pitch(1, at_bat=1, pitcher=10, outs=0, event="single"),
            # A caught stealing between pitches is credited to pitcher 20.
            pitch(1, at_bat=2, pitcher=20, outs=0),
            pitch(2, at_bat=2, pitcher=20, outs=1, event="single"),
            # A hit can also record a runner out; labels are not authoritative.
            pitch(1, at_bat=3, pitcher=20, outs=2, event="single"),
            pitch(1, at_bat=4, pitcher=20, outs=0, event="field_out", half="Bot"),
        )
        statcast = StatcastData(rows, tuple(row for row in rows if row["events"]))

        derived = derive_outs_recorded(statcast, {778098: 1})

        self.assertEqual(
            [row["outs_recorded"] for row in derived.pitches], [0, 1, 1, 1, 1]
        )
        self.assertEqual(
            sum(
                row["outs_recorded"]
                for row in derived.pitches
                if row["pitcher_id"] == 20
            ),
            4,
        )

    def test_final_pitch_uses_linescore_outs_for_walkoff_and_requires_it(self):
        row = {
            "game_date": "2025-05-01",
            "game_pk": 778108,
            "pitcher_id": 10,
            "batter_id": 20,
            "events": "single",
            "inning": 10,
            "inning_topbot": "Bot",
            "at_bat_number": 86,
            "pitch_number": 1,
            "outs_when_up": 1,
            "event_id": "778108:86",
        }
        statcast = StatcastData((row,), (row,))
        self.assertEqual(
            derive_outs_recorded(statcast, {778108: 1}).pitches[0][
                "outs_recorded"
            ],
            0,
        )
        with self.assertRaisesRegex(ValueError, "final linescore outs"):
            derive_outs_recorded(statcast, {})

    def test_out_state_regression_and_illegal_half_transition_are_rejected(self):
        base = {
            "game_date": "2025-05-01",
            "game_pk": 1,
            "pitcher_id": 10,
            "batter_id": 20,
            "events": "",
            "inning": 1,
            "inning_topbot": "Top",
            "at_bat_number": 1,
            "pitch_number": 1,
            "outs_when_up": 2,
            "event_id": "1:1",
        }
        regressed = dict(base, pitch_number=2, outs_when_up=1)
        with self.assertRaisesRegex(ValueError, "regressed"):
            derive_outs_recorded(StatcastData((base, regressed), ()), {1: 3})
        skipped = dict(
            base,
            inning=2,
            inning_topbot="Top",
            at_bat_number=2,
            pitch_number=1,
            outs_when_up=0,
        )
        with self.assertRaisesRegex(ValueError, "illegal half-inning"):
            derive_outs_recorded(StatcastData((base, skipped), ()), {1: 3})

    def test_pre_pa_scores_are_required_nonnegative_integers(self):
        text = FIXTURE.read_text("utf-8-sig")
        malformed = FIXTURE.parent / "statcast_bad.tmp.csv"
        try:
            for field in ("home_score", "away_score"):
                malformed.write_text(text.replace(field, f"{field}_x", 1), encoding="utf-8")
                with self.subTest(field=field), self.assertRaisesRegex(
                    ValueError, f"missing required fields.*{field}"
                ):
                    parse_statcast(malformed)
        finally:
            malformed.unlink(missing_ok=True)

    def test_fetch_ranges_cover_only_requested_regular_seasons(self):
        self.assertEqual(
            regular_season_ranges("2025-09-28"),
            [
                ("2022-04-07", "2022-10-05"),
                ("2023-03-30", "2023-10-01"),
                ("2024-03-20", "2024-09-30"),
                ("2025-03-18", "2025-09-28"),
            ],
        )

    def test_fetch_validates_before_publication_and_reuses_identical_query(self):
        payload = FIXTURE.read_bytes()
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            calls = []

            def fetcher(url, *, max_bytes):
                calls.append((url, max_bytes))
                return payload

            first = fetch_monthly(
                store, "2024-04-01", "2024-04-02", fetcher=fetcher
            )
            second = fetch_monthly(
                store, "2024-04-01", "2024-04-02", fetcher=fetcher
            )
            self.assertEqual(first[0].sha256, second[0].sha256)
            self.assertEqual(
                len(list((Path(td) / "baseball_savant_statcast").glob("*.manifest.json"))),
                1,
            )

    def test_fetch_resumes_verified_exact_query_without_network(self):
        payload = FIXTURE.read_bytes()
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            first = fetch_monthly(
                store,
                "2024-04-01",
                "2024-04-02",
                fetcher=lambda _url, **_kwargs: payload,
            )[0]
            query = {
                "start": "2024-04-01", "end": "2024-04-02", "game_type": "R",
                "url": statcast_url("2024-04-01", "2024-04-02"),
            }
            known = {json.dumps(query, separators=(",", ":"), sort_keys=True): first.sha256}
            resumed = fetch_monthly(
                store,
                "2024-04-01",
                "2024-04-02",
                known_queries=known,
                fetcher=lambda *_args, **_kwargs: self.fail("network should not run"),
            )
            self.assertEqual([item.sha256 for item in resumed], [first.sha256])

    def test_fetch_rejects_bad_payload_without_raw_publication(self):
        bad_payloads = (
            b"<html>upstream error</html>",
            FIXTURE.read_bytes().replace(b",game_type\n", b"\n", 1),
            FIXTURE.read_bytes().replace(b",R\n", b",P\n", 1),
            FIXTURE.read_bytes().replace(b"2024-04-01", b"2024-03-31", 1),
            FIXTURE.read_bytes().splitlines()[0] + b"\n",
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload[:30]), tempfile.TemporaryDirectory() as td:
                store = RawStore(Path(td))
                with self.assertRaises(ValueError):
                    fetch_monthly(
                        store,
                        "2024-04-01",
                        "2024-04-02",
                        fetcher=lambda _url, **_kwargs: payload,
                    )
                self.assertFalse(
                    (Path(td) / "baseball_savant_statcast").exists()
                )

    def test_fetch_rejects_ambiguous_payload_for_existing_query(self):
        original = FIXTURE.read_bytes()
        changed = original.replace(b"95.0", b"94.9", 1)
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            fetch_monthly(
                store,
                "2024-04-01",
                "2024-04-02",
                fetcher=lambda _url, **_kwargs: original,
            )
            with self.assertRaisesRegex(RuntimeError, "ambiguous raw payload"):
                fetch_monthly(
                    store,
                    "2024-04-01",
                    "2024-04-02",
                    fetcher=lambda _url, **_kwargs: changed,
                )

    def test_concurrent_query_publication_is_atomic_for_different_payloads(self):
        original = FIXTURE.read_bytes()
        changed = original.replace(b"95.0", b"94.9", 1)
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            barrier = threading.Barrier(2)

            def publish(payload):
                barrier.wait(timeout=5)
                try:
                    artifact = fetch_monthly(
                        store,
                        "2024-04-01",
                        "2024-04-02",
                        fetcher=lambda _url, **_kwargs: payload,
                    )[0]
                    return ("ok", artifact.sha256)
                except RuntimeError as exc:
                    return ("error", str(exc))

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(publish, (original, changed)))
            self.assertEqual([item[0] for item in results].count("ok"), 1)
            self.assertEqual([item[0] for item in results].count("error"), 1)
            self.assertIn("ambiguous raw payload", next(item[1] for item in results if item[0] == "error"))
            self.assertEqual(
                len(list((Path(td) / "baseball_savant_statcast").glob("*.manifest.json"))),
                1,
            )

    def test_concurrent_identical_query_publication_reuses_one_manifest(self):
        payload = FIXTURE.read_bytes()
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            barrier = threading.Barrier(2)

            def publish(_index):
                barrier.wait(timeout=5)
                return fetch_monthly(
                    store,
                    "2024-04-01",
                    "2024-04-02",
                    fetcher=lambda _url, **_kwargs: payload,
                )[0]

            with ThreadPoolExecutor(max_workers=2) as executor:
                artifacts = list(executor.map(publish, range(2)))
            self.assertEqual(artifacts[0].sha256, artifacts[1].sha256)
            self.assertEqual(
                len(list((Path(td) / "baseball_savant_statcast").glob("*.manifest.json"))),
                1,
            )

    def test_retry_policy_covers_transient_errors_and_exhaustion(self):
        for error in (
            TimeoutError("timeout"),
            URLError("url"),
            ConnectionError("connection"),
        ):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as td:
                attempts = []
                delays = []

                def failing(_url, **_kwargs):
                    attempts.append(1)
                    raise error

                with self.assertRaises(type(error)):
                    fetch_monthly(
                        RawStore(Path(td)),
                        "2024-04-01",
                        "2024-04-02",
                        fetcher=failing,
                        sleeper=delays.append,
                    )
                self.assertEqual(len(attempts), 6)
                self.assertEqual(delays, [1, 2, 4, 8, 16])

    def test_stage_audits_once_and_does_not_reread_29_prior_chunk_payloads(self):
        base = FIXTURE.read_bytes()
        ranges = [
            (f"2024-04-{day:02d}", f"2024-04-{day:02d}")
            for day in range(1, 30)
        ]
        verify_calls = []
        prior_payload_reads = []
        original_verify = RawStore.verify_manifests

        def tracking_verify(store):
            verify_calls.append(1)
            return original_verify(store)

        import mlb_backtest.manifest as manifest_module

        original_safe_read = manifest_module._safe_leaf_bytes

        def tracking_safe_read(*args, **kwargs):
            result = original_safe_read(*args, **kwargs)
            if len(args) >= 3 and args[2] == "raw" and result is not None:
                prior_payload_reads.append(args[0])
            return result

        def fetcher(url, **_kwargs):
            requested = parse_qs(urlsplit(url).query)["game_date_gt"][0]
            encoded = requested.encode("ascii")
            return base.replace(b"2024-04-01", encoded).replace(
                b"2024-04-02", encoded
            )

        with tempfile.TemporaryDirectory() as td, mock.patch(
            "mlb_backtest.statcast.regular_season_ranges", return_value=ranges
        ), mock.patch(
            "mlb_backtest.statcast.fetch_bytes", side_effect=fetcher
        ), mock.patch.object(
            RawStore, "verify_manifests", tracking_verify
        ), mock.patch(
            "mlb_backtest.manifest._safe_leaf_bytes", side_effect=tracking_safe_read
        ):
            artifacts = fetch_statcast_stage(
                Path(__file__).parents[1] / "config" / "v5_1_bt.json", td
            )
        self.assertEqual(len(artifacts), 29)
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(prior_payload_reads, [])


if __name__ == "__main__":
    unittest.main()
