import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError

from mlb_backtest.career_bf import (
    apply_prior_career_bf,
    fetch_prior_career_bf_records,
    load_prior_career_bf_records,
    parse_prior_career_bf,
    prior_career_bf_url,
)
from mlb_backtest.manifest import RawStore
from mlb_backtest.scoring import uncertainty


EMPTY_SPLITS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "mlb_career_bf_empty_splits.json"
)


def payload(pitcher_id=10, aggregate_bf=1000):
    return json.dumps(
        {
            "stats": [
                {
                    "type": {"displayName": "byDateRange"},
                    "group": {"displayName": "pitching"},
                    "splits": [
                        {
                            "stat": {"battersFaced": 400},
                            "team": {"id": 1},
                            "sport": {"id": 1},
                        },
                        {
                            "stat": {"battersFaced": 600},
                            "team": {"id": 2},
                            "sport": {"id": 1},
                        },
                        {
                            "stat": {"battersFaced": aggregate_bf},
                            "sport": {"id": 0},
                        },
                    ],
                }
            ]
        },
        sort_keys=True,
    ).encode()


class PriorCareerBfTest(unittest.TestCase):
    def test_url_is_frozen_to_mlb_regular_seasons_through_2021(self):
        url = prior_career_bf_url(543037)
        parsed = urlsplit(url)
        self.assertEqual(parsed.path, "/api/v1/people/543037/stats")
        query = parse_qs(parsed.query)
        self.assertEqual(query["stats"], ["byDateRange"])
        self.assertEqual(query["group"], ["pitching"])
        self.assertEqual(query["gameType"], ["R"])
        self.assertEqual(query["sportIds"], ["1"])
        self.assertEqual(query["startDate"], ["1876-01-01"])
        self.assertEqual(query["endDate"], ["2021-12-31"])

    def test_parser_uses_unique_all_sport_aggregate_without_double_counting(self):
        self.assertEqual(parse_prior_career_bf(payload(), 10), 1000)
        self.assertEqual(
            parse_prior_career_bf(json.dumps({"stats": []}).encode(), 11), 0
        )
        self.assertEqual(
            parse_prior_career_bf(EMPTY_SPLITS_FIXTURE, 669022), 0
        )
        malformed = json.loads(payload())
        malformed["stats"][0]["splits"].append(
            {"stat": {"battersFaced": 1000}, "sport": {"id": 0}}
        )
        with self.assertRaisesRegex(ValueError, "aggregate"):
            parse_prior_career_bf(json.dumps(malformed).encode(), 10)
        mismatch = json.loads(payload())
        mismatch["stats"][0]["splits"][-1]["stat"]["battersFaced"] = 999
        with self.assertRaisesRegex(ValueError, "does not match"):
            parse_prior_career_bf(json.dumps(mismatch).encode(), 10)

    def test_fetch_is_immutable_resumable_and_loader_binds_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = RawStore(root)
            calls = []

            def fetcher(url):
                calls.append(url)
                return payload()

            first = fetch_prior_career_bf_records(
                store, [10], fetcher=fetcher, sleeper=lambda _delay: None
            )
            second = fetch_prior_career_bf_records(
                store,
                [10],
                fetcher=lambda _url: self.fail("resume must not fetch"),
                sleeper=lambda _delay: None,
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(first, second)
            loaded, hashes = load_prior_career_bf_records(root, [10])
            self.assertEqual(loaded[10]["prior_bf"], 1000)
            self.assertEqual(loaded[10]["cutoff_date"], "2021-12-31")
            self.assertEqual(hashes, [loaded[10]["manifest_sha256"]])

    def test_fetch_retries_every_5xx_status(self):
        with tempfile.TemporaryDirectory() as td:
            attempts = []
            delays = []

            def fetcher(url):
                attempts.append(url)
                if len(attempts) == 1:
                    raise HTTPError(url, 501, "transient", {}, None)
                return payload()

            records = fetch_prior_career_bf_records(
                RawStore(Path(td)),
                [10],
                fetcher=fetcher,
                sleeper=delays.append,
            )
            self.assertEqual(records[10]["prior_bf"], 1000)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(delays, [1])

    def test_returning_veteran_adds_pre2022_bf_without_post_d1_rows(self):
        raw = [{
            "eligible": True,
            "official_date": "2025-05-01",
            "away": {"starter": {"pitcher_id": 10, "career_bf": 40}},
            "home": {"starter": {"pitcher_id": 20, "career_bf": 80}},
        }]
        records = {
            10: {"pitcher_id": 10, "prior_bf": 500, "cutoff_date": "2021-12-31", "source": "mlb_stats_api", "manifest_sha256": "a" * 64},
            20: {"pitcher_id": 20, "prior_bf": 0, "cutoff_date": "2021-12-31", "source": "mlb_stats_api", "manifest_sha256": "b" * 64},
        }
        apply_prior_career_bf(raw, records)
        self.assertEqual(raw[0]["away"]["starter"]["career_bf"], 540)
        self.assertEqual(raw[0]["home"]["starter"]["career_bf"], 80)
        self.assertEqual(uncertainty(0.95, False, 540, False), "Low")
        self.assertEqual(uncertainty(0.95, False, 80, False), "Medium")
        self.assertNotIn("2025", json.dumps(records, sort_keys=True))
        bad = {key: dict(value) for key, value in records.items()}
        bad[10]["cutoff_date"] = "2025-04-30"
        with self.assertRaisesRegex(ValueError, "invalid"):
            apply_prior_career_bf(raw, bad)


if __name__ == "__main__":
    unittest.main()
