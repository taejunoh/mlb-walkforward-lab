import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mlb_backtest.statcast import (
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
                ("2025-03-27", "2025-09-28"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
