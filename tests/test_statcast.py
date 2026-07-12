import unittest
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from mlb_backtest.manifest import RawStore
from mlb_backtest.statcast import (
    fetch_monthly,
    identify_pitcher_roles,
    parse_statcast,
    regular_season_ranges,
    statcast_url,
)


FIXTURE = Path(__file__).parent / "fixtures" / "statcast_mini.csv"


class StatcastTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
