from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mlb_backtest.market_play_contracts import canonical_json_bytes, content_digest, sidecar_bytes
from mlb_backtest.multibook_market_capture import (
    RAW_PREFIX,
    RAW_SCHEMA,
    SOURCE_HOSTS,
    UNAVAILABLE_SCHEMA,
    admit_raw_market_capture,
    load_capture_outcome,
    record_unavailable_book,
    validate_capture_outcome,
    validate_raw_market_capture,
)
from mlb_backtest.secure_fs import open_trusted_root


def raw_capture(book: str = "FANDUEL") -> dict[str, object]:
    host = next(iter(SOURCE_HOSTS[book]))
    body: dict[str, object] = {
        "schema": RAW_SCHEMA,
        "content_sha256": "",
        "source_book": book,
        "captured_at_utc": "2026-09-02T13:00:00Z",
        "source_url": f"https://{host}/mlb/today",
        "transport_metadata": {"collector": "fixture"},
        "events": [
            {
                "book_event_id": f"{book}-1",
                "away_team": "AAA",
                "home_team": "BBB",
                "scheduled_start_utc": "2026-09-02T19:00:00Z",
                "moneyline": {"away_american_odds": -110, "home_american_odds": 100},
                "totals": [{"line_half_runs": 85, "over_american_odds": -105, "under_american_odds": -115}],
            }
        ],
    }
    body["content_sha256"] = content_digest(body)
    return body


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        # macOS `/var` and `/tmp` are symlinked; secure_fs intentionally
        # rejects symlinked root components, so keep the fixture below the
        # real repository path.
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_raw(self, book: str = "FANDUEL") -> Path:
        payload = raw_capture(book)
        raw = canonical_json_bytes(payload)
        relative = Path(RAW_PREFIX / f"{payload['content_sha256']}.json")
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.with_suffix(".sha256").write_bytes(sidecar_bytes(raw))
        return relative

    def test_each_supported_book_admits_one_content_addressed_capture(self):
        for book in SOURCE_HOSTS:
            relative = self.write_raw(book)
            outcome = admit_raw_market_capture(self.root, relative)
            self.assertTrue(outcome.as_posix().startswith("audit/experimental_multibook_market/capture_outcomes/"))
            with open_trusted_root(self.root) as trusted:
                loaded = load_capture_outcome(trusted, outcome)
            self.assertEqual(loaded.payload["status"], "AVAILABLE")

    def test_unavailable_outcome_has_no_raw_path(self):
        body: dict[str, object] = {
            "schema": "PROVISIONAL_MULTIBOOK_MARKET_CAPTURE_OUTCOME_V1",
            "content_sha256": "",
            "status": "UNAVAILABLE",
            "source_book": "BETMGM",
            "recorded_at_utc": "2026-09-02T13:00:00Z",
            "reason": "SOURCE_UNAVAILABLE",
            "detail": "fixture",
        }
        body["content_sha256"] = content_digest(body)
        result = validate_capture_outcome(body)
        self.assertNotIn("raw_capture_relative", result)

    def test_unavailable_record_is_admitted(self):
        record = {
            "schema": UNAVAILABLE_SCHEMA,
            "source_book": "CAESARS",
            "recorded_at_utc": "2026-09-02T13:00:00Z",
            "reason": "LOGIN_REQUIRED",
            "detail": "fixture",
        }
        relative = Path("data/experimental_multibook_market/unavailable/caesars.json")
        path = self.root / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(canonical_json_bytes(record))
        outcome = record_unavailable_book(self.root, relative)
        with open_trusted_root(self.root) as trusted:
            loaded = load_capture_outcome(trusted, outcome)
        self.assertEqual(loaded.status, "UNAVAILABLE")

    def test_capture_rejects_bad_sidecar_and_bad_path(self):
        relative = self.write_raw()
        (self.root / relative).with_suffix(".sha256").write_bytes(b"x\n")
        with self.assertRaises(ValueError):
            admit_raw_market_capture(self.root, relative)
        with self.assertRaises(ValueError):
            admit_raw_market_capture(self.root, Path("../escape.json"))

    def test_content_hash_rejects_mutation(self):
        payload = raw_capture()
        payload["events"] = []
        with self.assertRaises(ValueError):
            validate_raw_market_capture(payload)


if __name__ == "__main__":
    unittest.main()
