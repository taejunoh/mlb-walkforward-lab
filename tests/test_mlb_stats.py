import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from mlb_backtest.manifest import RawStore
from mlb_backtest.mlb_stats import (
    fetch_league_era_records,
    league_era_url,
    parse_league_era,
)


FIXTURE = Path(__file__).parent / "fixtures" / "mlb_pitching_stats.json"


class MlbStatsTest(unittest.TestCase):
    def test_league_era_url_has_canonical_query(self):
        url = league_era_url("2025-03-18", "2025-04-30")
        parsed = urlparse(url)
        self.assertEqual(parsed.path, "/api/v1/stats")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "stats": ["byDateRange"],
                "group": ["pitching"],
                "sportIds": ["1"],
                "gameType": ["R"],
                "playerPool": ["ALL"],
                "limit": ["5000"],
                "startDate": ["2025-03-18"],
                "endDate": ["2025-04-30"],
            },
        )

    def test_strict_parser_sums_earned_runs_and_outs(self):
        parsed = parse_league_era(FIXTURE.read_bytes())
        self.assertEqual(parsed, {"earned_runs": 17, "outs": 108, "value": 4.25})

    def test_parser_rejects_malformed_or_truncated_payloads(self):
        valid = json.loads(FIXTURE.read_text("utf-8"))
        mutations = [
            {},
            {"stats": []},
            {"stats": [{"group": {"displayName": "hitting"}, "totalSplits": 0, "splits": []}]},
            {"stats": [valid["stats"][0], valid["stats"][0]]},
            {"stats": [dict(valid["stats"][0], totalSplits=3)]},
            {"stats": [dict(valid["stats"][0], totalSplits=True)]},
            {"stats": [dict(valid["stats"][0], splits=[])]},
        ]
        for payload in mutations:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_league_era(json.dumps(payload).encode())

        for field, bad in (("earnedRuns", -1), ("earnedRuns", True), ("outs", -1), ("outs", True), ("outs", 1.5)):
            payload = json.loads(FIXTURE.read_text("utf-8"))
            payload["stats"][0]["splits"][0]["stat"][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                parse_league_era(json.dumps(payload).encode())

        zero_outs = json.loads(FIXTURE.read_text("utf-8"))
        for split in zero_outs["stats"][0]["splits"]:
            split["stat"]["outs"] = 0
        with self.assertRaises(ValueError):
            parse_league_era(json.dumps(zero_outs).encode())

    def test_fetch_validates_before_publish_retries_and_resumes(self):
        payload = FIXTURE.read_bytes()
        calls = []
        sleeps = []

        def flaky(url, **kwargs):
            calls.append((url, kwargs))
            if len(calls) == 1:
                raise HTTPError(url, 429, "slow", {}, None)
            if len(calls) == 2:
                raise URLError("network")
            return payload

        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            records = fetch_league_era_records(
                store,
                {2025: "2025-03-18"},
                ["2025-04-30", "2025-04-30"],
                fetcher=flaky,
                retries=(1, 2),
                sleeper=sleeps.append,
            )
            self.assertEqual(sleeps, [1, 2])
            self.assertEqual(len(calls), 3)
            record = records["2025-04-30"]
            self.assertEqual(record["as_of_date"], "2025-04-30")
            self.assertEqual(record["value"], 4.25)
            self.assertEqual(record["earned_runs"], 17)
            self.assertEqual(record["outs"], 108)
            self.assertEqual(record["source"], "mlb_stats_api")
            self.assertRegex(record["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(len(store.verify_manifests()), 1)

            def must_not_fetch(*_args, **_kwargs):
                raise AssertionError("resume must use the existing unique query")

            resumed = fetch_league_era_records(
                store,
                {2025: "2025-03-18"},
                ["2025-04-30"],
                fetcher=must_not_fetch,
            )
            self.assertEqual(resumed, records)

            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("verified raw files need safe reads"),
            ):
                safely_resumed = fetch_league_era_records(
                    store,
                    {2025: "2025-03-18"},
                    ["2025-04-30"],
                    fetcher=must_not_fetch,
                )
            self.assertEqual(safely_resumed, records)

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("published manifests need safe reads"),
        ):
            published = fetch_league_era_records(
                RawStore(Path(td)),
                {2025: "2025-03-18"},
                ["2025-04-30"],
                fetcher=lambda _url: payload,
            )
        self.assertRegex(published["2025-04-30"]["manifest_sha256"], r"^[0-9a-f]{64}$")

    def test_fetch_rejects_boundaries_and_invalid_payload_before_publication(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            with self.assertRaises(ValueError):
                fetch_league_era_records(
                    store,
                    {2025: "2025-03-18"},
                    ["2025-03-17"],
                    fetcher=lambda *_args, **_kwargs: FIXTURE.read_bytes(),
                )
            with self.assertRaises(ValueError):
                fetch_league_era_records(
                    store,
                    {2025: "2025-03-18"},
                    ["2025-04-30"],
                    fetcher=lambda *_args, **_kwargs: b"{}",
                )
            self.assertEqual(store.verify_manifests(), [])

    def test_nontransient_http_failure_is_not_retried(self):
        calls = []
        sleeps = []

        def failing(url, **_kwargs):
            calls.append(url)
            raise HTTPError(url, 400, "bad", {}, None)

        with tempfile.TemporaryDirectory() as td, self.assertRaises(HTTPError):
            fetch_league_era_records(
                RawStore(Path(td)),
                {2025: "2025-03-18"},
                ["2025-04-30"],
                fetcher=failing,
                sleeper=sleeps.append,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_default_retry_policy_is_frozen_for_every_http_status(self):
        for status in (429, 500, 502, 503, 504, 501, 505):
            calls = []
            sleeps = []

            def failing(url, status=status):
                calls.append(url)
                raise HTTPError(url, status, "failure", {}, None)

            with self.subTest(status=status), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(HTTPError):
                    fetch_league_era_records(
                        RawStore(Path(td)),
                        {2025: "2025-03-18"},
                        ["2025-04-30"],
                        fetcher=failing,
                        sleeper=sleeps.append,
                    )
            if status in {429, 500, 502, 503, 504}:
                self.assertEqual(len(calls), 6)
                self.assertEqual(sleeps, [1, 2, 4, 8, 16])
            else:
                self.assertEqual(len(calls), 1)
                self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
