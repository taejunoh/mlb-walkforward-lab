from __future__ import annotations

import csv
import io
import unittest
from pathlib import Path

from mlb_backtest.statcast import REQUIRED_FIELDS, STATCAST_ROW_CAP
from mlb_backtest.v52_evidence import LogicalQuery


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/v5_2_qd/sources/statcast_mini.csv"


def _query(start: str = "2024-04-01", end: str = "2024-04-30") -> LogicalQuery:
    return LogicalQuery(
        source="baseball_savant_statcast",
        grain="pitch",
        endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
        parameters={"all": "true", "type": "details", "player_type": "batter", "game_date_gt": start, "game_date_lt": end, "hfGT": "R|", "group_by": "name-year", "sort_col": "pitches", "player_event_sort": "api_p_release_speed", "sort_order": "desc", "min_pitches": "0"},
        requested_range={"year": 2024, "start_date": start, "end_date": end},
        expected_media_type="text/csv",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


def _rows(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    stream = io.StringIO(raw.decode())
    reader = csv.DictReader(stream)
    return list(reader.fieldnames or []), list(reader)


def _csv(fields: list[str], rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=fields, lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


class V52StatcastSourceTest(unittest.TestCase):
    def test_statcast_sidecar_reuses_strict_parser_and_proves_semantics(self) -> None:
        from mlb_backtest.v52_sources.statcast import recompute_statcast_sidecar

        sidecar = recompute_statcast_sidecar(FIXTURE.read_bytes(), _query())
        semantics = sidecar["semantics"]
        self.assertEqual(semantics["required_fields"], sorted(REQUIRED_FIELDS))
        self.assertEqual(semantics["unique_pitch_count"], 3)
        self.assertEqual(semantics["game_pks"], [9001])
        self.assertEqual(semantics["pitcher_ids"], [101, 201])
        self.assertEqual(semantics["batter_ids"], [1001, 2001])
        self.assertEqual(semantics["terminal_plate_appearance_count"], 2)
        self.assertRegex(semantics["games"][0]["terminal_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(semantics["split_disposition"], "READY")

    def test_statcast_missing_field_and_conflicting_pitch_are_rejected(self) -> None:
        from mlb_backtest.v52_sources.statcast import recompute_statcast_sidecar

        fields, rows = _rows(FIXTURE.read_bytes())
        missing = [field for field in fields if field != "release_speed"]
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            recompute_statcast_sidecar(_csv(missing, rows), _query())
        conflict = dict(rows[0])
        conflict["release_speed"] = "90.0"
        with self.assertRaisesRegex(ValueError, "conflicting duplicate pitch"):
            recompute_statcast_sidecar(_csv(fields, [*rows, conflict]), _query())
        wrong_media = _query().__class__(
            **{**_query().__dict__, "expected_media_type": "application/json"}
        )
        with self.assertRaisesRegex(ValueError, "media type"):
            recompute_statcast_sidecar(FIXTURE.read_bytes(), wrong_media)
        wrong_range = _query().__class__(
            **{**_query().__dict__, "parameters": {**_query().parameters, "game_date_lt": "2024-04-29"}}
        )
        with self.assertRaisesRegex(ValueError, "parameters"):
            recompute_statcast_sidecar(FIXTURE.read_bytes(), wrong_range)

    def test_statcast_row_cap_splits_multiday_but_blocks_terminal_day(self) -> None:
        from mlb_backtest.v52_sources.statcast import recompute_statcast_sidecar

        fields, rows = _rows(FIXTURE.read_bytes())
        capped = []
        for number in range(1, STATCAST_ROW_CAP + 1):
            row = dict(rows[0])
            row["at_bat_number"] = str(number)
            row["pitch_number"] = "1"
            capped.append(row)
        sidecar = recompute_statcast_sidecar(_csv(fields, capped), _query())
        self.assertEqual(sidecar["semantics"]["split_disposition"], "SPLIT_REQUIRED")
        with self.assertRaisesRegex(ValueError, "single-day.*cap"):
            recompute_statcast_sidecar(
                _csv(fields, capped), _query("2024-04-02", "2024-04-02")
            )

    def test_statcast_child_coverage_is_ordered_gap_free_and_nonoverlapping(self) -> None:
        from mlb_backtest.v52_sources.statcast import validate_statcast_child_coverage

        parent = {"inclusive_start": "2024-04-01", "inclusive_end": "2024-04-30"}
        children = [
            {"inclusive_start": "2024-04-01", "inclusive_end": "2024-04-15"},
            {"inclusive_start": "2024-04-16", "inclusive_end": "2024-04-30"},
        ]
        self.assertEqual(validate_statcast_child_coverage(parent, children), children)
        for bad in ([children[1], children[0]], [children[0], {**children[1], "inclusive_start": "2024-04-15"}], [children[0], {**children[1], "inclusive_start": "2024-04-17"}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_statcast_child_coverage(parent, bad)

    def test_timeout_and_size_require_split_until_terminal_single_day(self) -> None:
        from mlb_backtest.v52_sources.statcast import statcast_split_action

        for reason in ("TIMEOUT", "SIZE_LIMIT"):
            with self.subTest(reason=reason):
                self.assertEqual(statcast_split_action(_query(), reason), "SPLIT_REQUIRED")
                with self.assertRaisesRegex(ValueError, "single-day"):
                    statcast_split_action(_query("2024-04-02", "2024-04-02"), reason)


if __name__ == "__main__":
    unittest.main()
