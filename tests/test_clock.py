import unittest
from datetime import date, datetime, timezone

from mlb_backtest.clock import assert_feature_dates, make_game_clock


class ClockTest(unittest.TestCase):
    def test_t45_and_d_minus_one(self):
        clock = make_game_clock(
            778498, "2025-05-01", "2025-05-01T23:10:00Z", 45
        )

        self.assertEqual(clock.game_pk, 778498)
        self.assertEqual(clock.official_date, date(2025, 5, 1))
        self.assertEqual(
            clock.scheduled_start_utc,
            datetime(2025, 5, 1, 23, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(
            clock.lock_at_utc,
            datetime(2025, 5, 1, 22, 25, tzinfo=timezone.utc),
        )
        self.assertEqual(clock.feature_as_of, date(2025, 4, 30))

    def test_timezone_offset_is_normalized_to_utc(self):
        clock = make_game_clock(
            778498, "2025-05-01", "2025-05-01T19:10:00-04:00", 45
        )

        self.assertEqual(
            clock.scheduled_start_utc,
            datetime(2025, 5, 1, 23, 10, tzinfo=timezone.utc),
        )
        self.assertIs(clock.scheduled_start_utc.tzinfo, timezone.utc)

    def test_invalid_game_clock_inputs_are_rejected(self):
        cases = [
            (0, "2025-05-01", "2025-05-01T23:10:00Z", 45),
            (-1, "2025-05-01", "2025-05-01T23:10:00Z", 45),
            (True, "2025-05-01", "2025-05-01T23:10:00Z", 45),
            (1, "bad-date", "2025-05-01T23:10:00Z", 45),
            (1, "2025-05-01", "bad-start", 45),
            (1, "2025-05-01", "2025-05-01T23:10:00", 45),
            (1, "2025-05-01", "2025-05-01T23:10:00Z", 0),
            (1, "2025-05-01", "2025-05-01T23:10:00Z", -1),
            (1, "2025-05-01", "2025-05-01T23:10:00Z", True),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                make_game_clock(*args)

    def test_same_day_and_future_features_are_rejected(self):
        for source_date in ("2025-05-01", "2025-05-02"):
            with self.subTest(source_date=source_date):
                with self.assertRaisesRegex(ValueError, "same-day leakage"):
                    assert_feature_dates("2025-05-01", [source_date])

    def test_empty_and_earlier_feature_dates_are_accepted(self):
        assert_feature_dates("2025-05-01", [])
        assert_feature_dates("2025-05-01", ["2025-04-01", "2025-04-30"])

    def test_malformed_feature_dates_are_rejected_as_values(self):
        with self.assertRaises(ValueError):
            assert_feature_dates("2025-05-01", ["not-a-date"])


if __name__ == "__main__":
    unittest.main()
