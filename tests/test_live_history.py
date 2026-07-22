import json
import csv
import hashlib
import io
import multiprocessing
import queue
import shutil
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from threading import BrokenBarrierError
from urllib.parse import parse_qs, urlsplit
from unittest import mock

from mlb_backtest.live_history import (
    build_live_history,
    live_career_bf_url,
    load_live_career_bf,
)
from mlb_backtest import live_history
from mlb_backtest.live_model_card import FrozenLiveCard
from mlb_backtest.live_top3_contracts import LiveHttpObservation
from mlb_backtest.manifest import RawStore


def _career_payload() -> bytes:
    return json.dumps({
        "stats": [{
            "type": {"displayName": "byDateRange"},
            "group": {"displayName": "pitching"},
            "splits": [
                {"stat": {"battersFaced": 123}, "team": {"id": 1}, "sport": {"id": 1}},
                {"stat": {"battersFaced": 123}, "sport": {"id": 0}},
            ],
        }],
    }).encode()


def _http(url: str, body: bytes) -> LiveHttpObservation:
    return LiveHttpObservation(
        requested_url=url,
        final_url=url,
        started_at_utc="2026-07-21T12:00:00Z",
        response_completed_at_utc="2026-07-21T12:00:01Z",
        http_status=200,
        content_type="application/json",
        etag='"fixture"',
        last_modified=None,
        body=body,
    )


def _career_completion_process_worker(root, worker_index, barrier, results):
    def durable_clock():
        try:
            barrier.wait(timeout=1)
        except BrokenBarrierError:
            pass
        return f"2026-07-21T12:00:0{worker_index}Z"

    try:
        with mock.patch(
            "mlb_backtest.live_history._canonical_utc_now",
            side_effect=durable_clock,
        ):
            _, bindings = load_live_career_bf(
                Path(root),
                "2026-07-21",
                [10],
                fetcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("completion recovery must not fetch")
                ),
            )
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        binding = bindings[0]
        results.put(
            ("ok", binding.binding_sha256, binding.durable_at_utc)
        )


def _card() -> FrozenLiveCard:
    return FrozenLiveCard(
        model_version="v5.1-BT", file_sha256="a" * 64, normalization={},
        slot_weights={slot: 1 / 9 for slot in range(1, 10)},
        component_weights={}, alpha=0.0, beta=1.0,
    )


def _history_schedule(days=("2026-07-20",)) -> bytes:
    games = []
    for official_date in days:
        game_offset = (date.fromisoformat(official_date).toordinal() % 1000) * 100
        for index in range(15):
            away, home = f"Away {index}", f"Home {index}"
            games.append({
            "gamePk": 900000 + game_offset + index, "gameType": "R", "officialDate": official_date,
            "gameDate": "2026-07-20T19:00:00Z", "status": {"detailedState": "Final"},
            "teams": {
                "away": {"team": {"id": 100 + index, "name": away}, "score": 1, "isWinner": False},
                "home": {"team": {"id": 200 + index, "name": home}, "score": 2, "isWinner": True},
            },
            "linescore": {"currentInning": 1, "inningHalf": "Top", "outs": 0},
            "venue": {"id": 1, "name": "Fixture Park"},
            })
    by_day = [{"date": day, "games": [game for game in games if game["officialDate"] == day]} for day in days]
    return json.dumps({"dates": by_day}).encode()


def _history_statcast(days=("2026-07-20",), *, poison_target_day=False) -> bytes:
    fixture = Path(__file__).parent / "fixtures" / "statcast_mini.csv"
    with fixture.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        next(reader)
        template = next(reader)
        fields = reader.fieldnames
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for game_day in days:
        game_offset = (date.fromisoformat(game_day).toordinal() % 1000) * 100
        for index in range(15):
            row = dict(template)
            row.update({
                "game_date": game_day, "game_pk": str(900000 + game_offset + index),
                "home_team": f"H{index:02}", "away_team": f"A{index:02}",
                "pitcher": str(1000 + index), "batter": str(2000 + index),
            })
            writer.writerow(row)
    if poison_target_day:
        row = dict(template)
        row.update({"game_date": "2026-07-21", "game_pk": "999999", "home_team": "H00", "away_team": "A00"})
        writer.writerow(row)
    return output.getvalue().encode()


def _venue() -> bytes:
    return json.dumps({"venues": [{"id": 1, "name": "Fixture Park", "location": {
        "city": "Fixture City", "defaultCoordinates": {"latitude": 40.0, "longitude": -74.0},
        "timeZone": {"id": "America/New_York"},
    }}]}).encode()


def _history_fetcher(*, poison_target_day=False):
    schedule = _history_schedule()
    statcast = _history_statcast(poison_target_day=poison_target_day)
    era = (Path(__file__).parent / "fixtures" / "mlb_pitching_stats.json").read_bytes()

    def fetcher(source, url, *, max_bytes):
        if "/schedule?" in url:
            return _http(url, schedule)
        if "statcast_search" in url:
            return _http(url, statcast)
        if "/venues/" in url:
            return _http(url, _venue())
        if "/stats?" in url:
            return _http(url, era)
        raise AssertionError(url)

    return fetcher


def _extending_history_fetcher(calls=None):
    era = (Path(__file__).parent / "fixtures" / "mlb_pitching_stats.json").read_bytes()

    def fetcher(source, url, *, max_bytes):
        if calls is not None:
            calls.append((source, url))
        if "/schedule?" in url:
            days = ("2026-08-01",) if "endDate=2026-08-01" in url else ("2026-08-01", "2026-08-02")
            return _http(url, _history_schedule(days))
        if "statcast_search" in url:
            days = ("2026-08-01",) if "game_date_gt=2026-08-01" in url else ("2026-08-02",)
            return _http(url, _history_statcast(days))
        if "/venues/" in url:
            return _http(url, _venue())
        if "/stats?" in url:
            return _http(url, era)
        raise AssertionError(url)

    return fetcher


def _month_boundary_fetcher():
    era = (Path(__file__).parent / "fixtures" / "mlb_pitching_stats.json").read_bytes()

    def fetcher(source, url, *, max_bytes):
        if "/schedule?" in url:
            days = ("2026-08-31",) if "endDate=2026-08-31" in url else ("2026-08-31", "2026-09-01")
            return _http(url, _history_schedule(days))
        if "statcast_search" in url:
            days = ("2026-08-31",) if "game_date_gt=2026-08-31" in url else ("2026-09-01",)
            return _http(url, _history_statcast(days))
        if "/venues/" in url:
            return _http(url, _venue())
        if "/stats?" in url:
            return _http(url, era)
        raise AssertionError(url)

    return fetcher


class LiveHistoryTests(unittest.TestCase):
    @staticmethod
    def _receipt_prefix_paths(receipt):
        return (
            receipt.with_suffix(".claim.json"),
            receipt,
            receipt.with_suffix(".sha256"),
            receipt.with_suffix(".completion.json"),
            receipt.with_suffix(".completion.sha256"),
        )

    def test_public_career_loader_rejects_symlink_root_before_any_outside_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            (outside / "nested-history-root").mkdir()
            outside_before = tuple(outside.iterdir())
            symlink_root = base / "history-root"
            symlink_root.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                load_live_career_bf(
                    symlink_root / "nested-history-root",
                    "2026-07-21",
                    [10],
                    fetcher=lambda *_args, **_kwargs: self.fail(
                        "unsafe root must fail before fetch"
                    ),
                )
            self.assertEqual(tuple(outside.iterdir()), outside_before)

    def test_public_history_apis_reject_symlinked_cache_ancestor_without_external_writes(self):
        for api_name in ("career", "history"):
            with self.subTest(api_name=api_name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "root"
                outside = base / "outside"
                root.mkdir()
                outside.mkdir()
                (root / "data").symlink_to(outside, target_is_directory=True)
                outside_before = tuple(outside.iterdir())
                with self.assertRaises(ValueError):
                    if api_name == "career":
                        load_live_career_bf(
                            root,
                            "2026-07-21",
                            [10],
                            fetcher=lambda *_args, **_kwargs: self.fail(
                                "unsafe cache ancestor must fail before fetch"
                            ),
                        )
                    else:
                        build_live_history(
                            root,
                            "2026-07-21",
                            _card(),
                            fetcher=lambda *_args, **_kwargs: self.fail(
                                "unsafe cache ancestor must fail before fetch"
                            ),
                            sleeper=lambda _delay: None,
                        )
                self.assertEqual(tuple(outside.iterdir()), outside_before)

    def test_public_history_apis_reject_root_swap_before_any_external_write(self):
        for api_name in ("career", "history"):
            with self.subTest(api_name=api_name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "root"
                moved = base / "moved-root"
                outside = base / "outside"
                root.mkdir()
                outside.mkdir()
                outside_before = tuple(outside.iterdir())
                swapped = False

                def swap(stage, observed_root):
                    nonlocal swapped
                    if stage == "after_existing_root_lstat" and not swapped:
                        swapped = True
                        observed_root.rename(moved)
                        observed_root.symlink_to(outside, target_is_directory=True)

                with mock.patch(
                    "mlb_backtest.live_history._history_root_test_hook",
                    side_effect=swap,
                ):
                    with self.assertRaises(ValueError):
                        if api_name == "career":
                            load_live_career_bf(
                                root,
                                "2026-07-21",
                                [10],
                                fetcher=lambda *_args, **_kwargs: self.fail(
                                    "swapped root must fail before fetch"
                                ),
                            )
                        else:
                            build_live_history(
                                root,
                                "2026-07-21",
                                _card(),
                                fetcher=lambda *_args, **_kwargs: self.fail(
                                    "swapped root must fail before fetch"
                                ),
                                sleeper=lambda _delay: None,
                            )
                self.assertTrue(swapped)
                self.assertEqual(tuple(outside.iterdir()), outside_before)

    def test_every_valid_receipt_prefix_resumes_by_publishing_only_missing_suffix(self):
        for prefix_length in range(6):
            with self.subTest(prefix_length=prefix_length), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, bindings = load_live_career_bf(
                    root, "2026-07-21", [10],
                    fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()),
                )
                receipt = root / bindings[0].receipt_relative
                paths = self._receipt_prefix_paths(receipt)
                preserved = {path: path.read_bytes() for path in paths[:prefix_length]}
                for path in paths[prefix_length:]:
                    path.unlink()
                calls = []

                def fetcher(source, url, *, max_bytes):
                    calls.append(url)
                    return _http(url, _career_payload())

                _, resumed = load_live_career_bf(
                    root, "2026-07-21", [10], fetcher=fetcher
                )
                self.assertEqual(resumed[0].binding_role, "CAREER_BF")
                self.assertTrue(all(path.is_file() for path in paths))
                self.assertEqual(
                    {path: path.read_bytes() for path in paths[:prefix_length]},
                    preserved,
                )
                self.assertEqual(len(calls), 1 if prefix_length == 0 else 0)

    def test_every_valid_statcast_receipt_prefix_resumes(self):
        for prefix_length in range(6):
            with self.subTest(prefix_length=prefix_length), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                build_live_history(
                    root, "2026-07-21", _card(),
                    fetcher=_history_fetcher(), sleeper=lambda _delay: None,
                )
                directory = root / "audit/experimental_daily/history_receipts"
                receipt = next(
                    path for path in directory.glob("history-history_statcast-*.json")
                    if not path.name.endswith((".claim.json", ".completion.json"))
                )
                paths = self._receipt_prefix_paths(receipt)
                preserved = {path: path.read_bytes() for path in paths[:prefix_length]}
                for path in paths[prefix_length:]:
                    path.unlink()
                calls = []
                base = _history_fetcher()

                def fetcher(source, url, *, max_bytes):
                    calls.append((source, url))
                    return base(source, url, max_bytes=max_bytes)

                build_live_history(
                    root, "2026-07-21", _card(), fetcher=fetcher,
                    sleeper=lambda _delay: None,
                )
                self.assertTrue(all(path.is_file() for path in paths))
                self.assertEqual(
                    {path: path.read_bytes() for path in paths[:prefix_length]},
                    preserved,
                )
                self.assertEqual(
                    [source for source, _url in calls],
                    ["savant"] if prefix_length == 0 else [],
                )

    def test_each_receipt_prefix_artifact_byte_mismatch_fails_closed(self):
        for corrupt_index in range(5):
            with self.subTest(corrupt_index=corrupt_index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, bindings = load_live_career_bf(
                    root, "2026-07-21", [10],
                    fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()),
                )
                receipt = root / bindings[0].receipt_relative
                target = self._receipt_prefix_paths(receipt)[corrupt_index]
                content = bytearray(target.read_bytes())
                content[0] ^= 1
                target.write_bytes(bytes(content))
                with self.assertRaises(ValueError):
                    load_live_career_bf(
                        root, "2026-07-21", [10],
                        fetcher=lambda *_args, **_kwargs: self.fail("mismatch must fail before fetch"),
                    )

    def test_receipt_state_gap_fails_closed_without_republishing(self):
        for missing_index in range(4):
            with self.subTest(missing_index=missing_index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, bindings = load_live_career_bf(
                    root, "2026-07-21", [10],
                    fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()),
                )
                receipt = root / bindings[0].receipt_relative
                self._receipt_prefix_paths(receipt)[missing_index].unlink()
                with self.assertRaisesRegex(ValueError, "prefix"):
                    load_live_career_bf(
                        root, "2026-07-21", [10],
                        fetcher=lambda *_args, **_kwargs: self.fail("gap must not fetch"),
                    )

    def test_receipt_state_rejects_extra_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, bindings = load_live_career_bf(
                root, "2026-07-21", [10],
                fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()),
            )
            receipt = root / bindings[0].receipt_relative
            receipt.with_name(f"{receipt.stem}.extra").write_bytes(b"unexpected")
            with self.assertRaisesRegex(ValueError, "extra"):
                load_live_career_bf(
                    root, "2026-07-21", [10],
                    fetcher=lambda *_args, **_kwargs: self.fail("extra must not fetch"),
                )

    def test_raw_only_recovery_rejects_different_response_without_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, bindings = load_live_career_bf(
                root, "2026-07-21", [10],
                fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()),
            )
            receipt = root / bindings[0].receipt_relative
            paths = self._receipt_prefix_paths(receipt)
            for path in paths:
                path.unlink()
            different = _career_payload().replace(b"123", b"124")
            with self.assertRaisesRegex(ValueError, "raw prefix"):
                load_live_career_bf(
                    root, "2026-07-21", [10],
                    fetcher=lambda source, url, *, max_bytes: _http(url, different),
                )
            self.assertTrue(all(not path.exists() for path in paths))

    def test_raw_prefix_rejects_symlink_and_hardlink_identities(self):
        for link_kind in ("symlink", "hardlink"):
            for pattern in ("*.raw", "*.manifest.json", "*.manifest.sha256"):
                with (
                    self.subTest(link_kind=link_kind, pattern=pattern),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    load_live_career_bf(
                        root, "2026-07-21", [10],
                        fetcher=lambda source, url, *, max_bytes: _http(
                            url, _career_payload()
                        ),
                    )
                    target = next(
                        (root / "data/experimental_daily/history_cache").rglob(pattern)
                    )
                    outside = root / "raw-identity-source"
                    outside.write_bytes(target.read_bytes())
                    target.unlink()
                    if link_kind == "symlink":
                        target.symlink_to(outside)
                    else:
                        target.hardlink_to(outside)
                    with self.assertRaises((RuntimeError, ValueError)):
                        load_live_career_bf(
                            root, "2026-07-21", [10],
                            fetcher=lambda *_args, **_kwargs: self.fail(
                                "unsafe raw identity must not fetch"
                            ),
                        )

    def test_capability_manifest_validation_matches_rawstore_mutation_corpus(self):
        cases = (
            "timestamp_invalid", "timestamp_naive", "timestamp_offset",
            "timestamp_type", "row_count_string", "row_count_bool",
            "row_count_mismatch", "schema_string", "schema_bool",
            "schema_other_int", "missing_field", "extra_field", "source",
            "query", "sha256", "noncanonical_bytes", "seal", "payload",
            "manifest_path",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "history"
                load_live_career_bf(
                    root, "2026-07-21", [10],
                    fetcher=lambda source, url, *, max_bytes: _http(
                        url, _career_payload()
                    ),
                )
                cache = root / "data/experimental_daily/history_cache"
                manifest_path = next(cache.rglob("*.manifest.json"))
                manifest = json.loads(manifest_path.read_bytes())
                if case == "timestamp_invalid":
                    manifest["retrieved_at_utc"] = "not-a-time"
                elif case == "timestamp_naive":
                    manifest["retrieved_at_utc"] = "2026-07-21T12:00:00"
                elif case == "timestamp_offset":
                    manifest["retrieved_at_utc"] = "2026-07-21T08:00:00-04:00"
                elif case == "timestamp_type":
                    manifest["retrieved_at_utc"] = 1
                elif case == "row_count_string":
                    manifest["row_count"] = "1"
                elif case == "row_count_bool":
                    manifest["row_count"] = True
                elif case == "row_count_mismatch":
                    manifest["row_count"] += 1
                elif case == "schema_string":
                    manifest["schema_version"] = "1"
                elif case == "schema_bool":
                    manifest["schema_version"] = True
                elif case == "schema_other_int":
                    manifest["schema_version"] = 2
                elif case == "missing_field":
                    manifest.pop("retrieved_at_utc")
                elif case == "extra_field":
                    manifest["extra"] = 1
                elif case == "source":
                    manifest["source"] = "other"
                elif case == "query":
                    manifest["query"] = {"changed": True}
                elif case == "sha256":
                    manifest["sha256"] = "0" * 64
                canonical = json.dumps(
                    manifest, ensure_ascii=False, indent=2, sort_keys=True
                ).encode()
                content = (
                    json.dumps(manifest, separators=(",", ":")).encode()
                    if case == "noncanonical_bytes"
                    else canonical
                )
                manifest_path.write_bytes(content)
                seal_path = manifest_path.with_suffix(".sha256")
                seal_path.write_text(
                    f"{hashlib.sha256(content).hexdigest()}\n"
                )
                if case == "seal":
                    seal_path.write_text(f"{'0' * 64}\n")
                if case == "payload":
                    next(cache.rglob("*.raw")).write_bytes(b"changed")
                if case == "manifest_path":
                    renamed = manifest_path.with_name(
                        f"wrong.{manifest_path.name}"
                    )
                    manifest_path.rename(renamed)
                direct_root = base / "rawstore"
                shutil.copytree(cache, direct_root)
                try:
                    RawStore(direct_root).verify_manifests()
                except (RuntimeError, ValueError):
                    rawstore_accepts = False
                else:
                    rawstore_accepts = True
                receipts = root / "audit/experimental_daily/history_receipts"
                for artifact in receipts.iterdir():
                    artifact.unlink()
                try:
                    load_live_career_bf(
                        root, "2026-07-21", [10],
                        fetcher=lambda source, url, *, max_bytes: _http(
                            url, _career_payload()
                        ),
                    )
                except (RuntimeError, ValueError):
                    capability_accepts = False
                else:
                    capability_accepts = True
                self.assertEqual(capability_accepts, rawstore_accepts)

    def test_capability_pending_transactions_match_rawstore(self):
        cases = (
            "exact_recoverable", "conflicting_pending", "orphan_seal",
            "pending_only", "bad_pending_seal", "final_conflicting_pending",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "history"
                load_live_career_bf(
                    root, "2026-07-21", [10],
                    fetcher=lambda source, url, *, max_bytes: _http(
                        url, _career_payload()
                    ),
                )
                cache = root / "data/experimental_daily/history_cache"
                manifest_path = next(cache.rglob("*.manifest.json"))
                pending_path = manifest_path.with_name(
                    manifest_path.name.removesuffix(".json") + ".pending"
                )
                seal_path = manifest_path.with_suffix(".sha256")
                original = manifest_path.read_bytes()
                manifest = json.loads(original)
                source = manifest["source"]
                query_value = manifest["query"]
                payload = next(cache.rglob("*.raw")).read_bytes()
                conflicting = dict(manifest)
                conflicting["query"] = {"conflicting": True}
                conflicting_bytes = json.dumps(
                    conflicting, ensure_ascii=False, indent=2, sort_keys=True
                ).encode()
                if case in {
                    "exact_recoverable", "pending_only", "bad_pending_seal"
                }:
                    manifest_path.rename(pending_path)
                elif case == "conflicting_pending":
                    manifest_path.unlink()
                    pending_path.write_bytes(conflicting_bytes)
                    seal_path.write_text(
                        f"{hashlib.sha256(conflicting_bytes).hexdigest()}\n"
                    )
                elif case == "orphan_seal":
                    manifest_path.unlink()
                elif case == "final_conflicting_pending":
                    pending_path.write_bytes(conflicting_bytes)
                if case == "pending_only":
                    seal_path.unlink()
                elif case == "bad_pending_seal":
                    seal_path.write_text(f"{'0' * 64}\n")

                direct_root = base / "rawstore"
                shutil.copytree(cache, direct_root)
                try:
                    RawStore(direct_root).put_unique_query(
                        source, query_value, payload
                    )
                except (RuntimeError, ValueError):
                    rawstore_accepts = False
                else:
                    rawstore_accepts = True

                receipts = root / "audit/experimental_daily/history_receipts"
                for artifact in receipts.iterdir():
                    artifact.unlink()
                try:
                    load_live_career_bf(
                        root, "2026-07-21", [10],
                        fetcher=lambda observed_source, url, *, max_bytes: _http(
                            url, _career_payload()
                        ),
                    )
                except (RuntimeError, ValueError):
                    capability_accepts = False
                else:
                    capability_accepts = True
                self.assertEqual(capability_accepts, rawstore_accepts)
                if rawstore_accepts:
                    capability_manifest = next(
                        cache.rglob("*.manifest.json"), None
                    )
                    capability_pending = next(
                        cache.rglob("*.manifest.pending"), None
                    )
                    direct_manifest = next(
                        direct_root.rglob("*.manifest.json"), None
                    )
                    direct_pending = next(
                        direct_root.rglob("*.manifest.pending"), None
                    )
                    self.assertEqual(
                        capability_manifest is not None,
                        direct_manifest is not None,
                    )
                    self.assertEqual(
                        capability_pending is not None,
                        direct_pending is not None,
                    )
                    if case == "exact_recoverable":
                        self.assertEqual(capability_manifest.read_bytes(), original)
                        self.assertEqual(direct_manifest.read_bytes(), original)

    def test_receipt_prefix_rejects_symlink_and_hardlink_identities(self):
        for link_kind in ("symlink", "hardlink"):
            for artifact_index in range(5):
                with (
                    self.subTest(link_kind=link_kind, artifact_index=artifact_index),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    _, bindings = load_live_career_bf(
                        root, "2026-07-21", [10],
                        fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()),
                    )
                    receipt = root / bindings[0].receipt_relative
                    target = self._receipt_prefix_paths(receipt)[artifact_index]
                    outside = root / "identity-source"
                    outside.write_bytes(target.read_bytes())
                    target.unlink()
                    if link_kind == "symlink":
                        target.symlink_to(outside)
                    else:
                        target.hardlink_to(outside)
                    with self.assertRaises(ValueError):
                        load_live_career_bf(
                            root, "2026-07-21", [10],
                            fetcher=lambda *_args, **_kwargs: self.fail(
                                "unsafe identity must not fetch"
                            ),
                        )

    def test_persistent_receipt_lock_rejects_symlink_and_hardlink_identity(self):
        for link_kind in ("symlink", "hardlink"):
            with self.subTest(link_kind=link_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                load_live_career_bf(
                    root, "2026-07-21", [10],
                    fetcher=lambda source, url, *, max_bytes: _http(
                        url, _career_payload()
                    ),
                )
                lock_path = next(
                    (root / "audit/experimental_daily/history_receipt_locks").glob(
                        "*.lock"
                    )
                )
                outside = root / "lock-identity-source"
                outside.write_bytes(lock_path.read_bytes())
                lock_path.unlink()
                if link_kind == "symlink":
                    lock_path.symlink_to(outside)
                else:
                    lock_path.hardlink_to(outside)
                with self.assertRaises(ValueError):
                    load_live_career_bf(
                        root, "2026-07-21", [10],
                        fetcher=lambda *_args, **_kwargs: self.fail(
                            "unsafe lock must fail before fetch"
                        ),
                    )

    def test_injected_fetcher_body_must_respect_requested_max_bytes(self):
        url = "https://statsapi.mlb.com/api/v1/stats"
        with self.assertRaisesRegex(ValueError, "size limit"):
            live_history._http_for(
                "stats_api", url,
                lambda source, requested, *, max_bytes: _http(requested, b"12"),
                1,
            )

    def test_schedule_history_build_binds_authenticated_schedule_manifest_sha(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "mlb_backtest.live_history._build_result_free_live_schedule_history",
            wraps=live_history._build_result_free_live_schedule_history,
        ) as builder:
            bundle = build_live_history(Path(temporary), "2026-07-21", _card(), fetcher=_history_fetcher(), sleeper=lambda _delay: None)
        schedule_binding = next(binding for binding in bundle.source_bindings if binding.binding_role == "HISTORY_SCHEDULE")
        self.assertEqual(builder.call_args.args[-1], schedule_binding.manifest_sha256)

    def test_schedule_history_builder_indexes_statcast_pitches_once(self):
        class CountingPitches:
            def __init__(self, rows):
                self.rows = tuple(rows)
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                return iter(self.rows)

        day = "2026-07-20"
        schedule, pitches, team_map = [], [], {}
        for index in range(15):
            game_pk = 700000 + index
            away, home = f"Away {index}", f"Home {index}"
            schedule.append(
                {
                    "game_pk": game_pk,
                    "official_date": day,
                    "away_team": away,
                    "home_team": home,
                    "venue_id": 1,
                    "venue_name": "Fixture Park",
                    "doubleheader_marker": "N",
                }
            )
            pitches.append(
                {"game_pk": game_pk, "game_date": day, "inning": 1}
            )
            team_map[away], team_map[home] = f"A{index}", f"H{index}"
        counting = CountingPitches(pitches)
        statcast = mock.Mock(pitches=counting)
        result = live_history._build_result_free_live_schedule_history(
            schedule,
            statcast,
            team_map,
            {
                1: {
                    "venue_name": "Fixture Park",
                    "venue_city": "City",
                    "venue_latitude": 40.0,
                    "venue_longitude": -74.0,
                    "time_zone": "America/New_York",
                }
            },
            date.fromisoformat(day),
            date.fromisoformat(day),
            "a" * 64,
        )
        self.assertEqual(len(result.rows), 15)
        self.assertEqual(counting.iterations, 1)

    def test_completion_timestamp_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_live_history(root, "2026-07-21", _card(), fetcher=_history_fetcher(), sleeper=lambda _delay: None)
            completion = next((root / "audit/experimental_daily/history_receipts").glob("*.completion.json"))
            value = json.loads(completion.read_text())
            value["durable_at_utc"] = "2099-01-01T00:00:00Z"
            completion.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            with self.assertRaisesRegex(ValueError, "completion"):
                build_live_history(root, "2026-07-21", _card(), fetcher=lambda *_args, **_kwargs: self.fail("tampered cache must not fetch"), sleeper=lambda _delay: None)

    def test_extending_cutoff_uses_complete_nonoverlapping_statcast_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_live_history(root, "2026-08-02", _card(), fetcher=_extending_history_fetcher(), sleeper=lambda _delay: None)
            extended = build_live_history(root, "2026-08-03", _card(), fetcher=_extending_history_fetcher(), sleeper=lambda _delay: None)
        self.assertEqual(extended.feature_history.max_pitch_row["game_date"], "2026-08-02")

    def test_same_month_extension_fetches_only_the_missing_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []
            build_live_history(root, "2026-08-02", _card(), fetcher=_extending_history_fetcher(calls), sleeper=lambda _delay: None)
            calls.clear()
            build_live_history(root, "2026-08-03", _card(), fetcher=_extending_history_fetcher(calls), sleeper=lambda _delay: None)
        statcast_urls = [url for source, url in calls if source == "savant"]
        self.assertEqual(len(statcast_urls), 1)
        self.assertIn("game_date_gt=2026-08-02", statcast_urls[0])
        self.assertIn("game_date_lt=2026-08-02", statcast_urls[0])

    def test_cache_authority_is_stable_after_later_cutoff_and_unrelated_career_acquisition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = build_live_history(root, "2026-08-02", _card(), fetcher=_extending_history_fetcher(), sleeper=lambda _delay: None)
            build_live_history(root, "2026-08-03", _card(), fetcher=_extending_history_fetcher(), sleeper=lambda _delay: None)
            load_live_career_bf(root, "2026-08-03", [10], fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()))
            reused = build_live_history(root, "2026-08-02", _card(), fetcher=lambda *_args, **_kwargs: self.fail("old cutoff must reuse exact cache"), sleeper=lambda _delay: None)
        self.assertEqual(first.cache_authority, reused.cache_authority)

    def test_interrupted_receipt_publication_resumes_only_exact_residue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, bindings = load_live_career_bf(root, "2026-07-21", [10], fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()))
            receipt = root / bindings[0].receipt_relative
            completion = receipt.with_suffix(".completion.json")
            completion_seal = receipt.with_suffix(".completion.sha256")
            receipt_bytes = receipt.read_bytes()
            completion.unlink()
            completion_seal.unlink(missing_ok=True)
            _, resumed = load_live_career_bf(root, "2026-07-21", [10], fetcher=lambda *_args, **_kwargs: self.fail("exact residue must resume offline"))
            self.assertEqual(receipt.read_bytes(), receipt_bytes)
            self.assertEqual(resumed[0].binding_role, "CAREER_BF")

    def test_completion_present_seal_missing_resumes_without_rewriting_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, bindings = load_live_career_bf(
                root, "2026-07-21", [10],
                fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()),
            )
            receipt = root / bindings[0].receipt_relative
            completion = receipt.with_suffix(".completion.json")
            completion_seal = receipt.with_suffix(".completion.sha256")
            preserved = completion.read_bytes()
            completion_seal.unlink()
            _, resumed = load_live_career_bf(
                root, "2026-07-21", [10],
                fetcher=lambda *_args, **_kwargs: self.fail(
                    "completion residue must resume offline"
                ),
            )
            self.assertEqual(completion.read_bytes(), preserved)
            self.assertEqual(
                completion_seal.read_text().strip(),
                hashlib.sha256(preserved).hexdigest(),
            )
            self.assertEqual(
                resumed[0].durable_at_utc,
                json.loads(preserved)["durable_at_utc"],
            )

    def test_identical_threaded_public_calls_share_winner_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, bindings = load_live_career_bf(
                root, "2026-07-21", [10],
                fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()),
            )
            receipt = root / bindings[0].receipt_relative
            receipt.with_suffix(".completion.json").unlink()
            receipt.with_suffix(".completion.sha256").unlink()
            barrier = threading.Barrier(2)
            local = threading.local()
            returned, errors = [], []

            def durable_clock():
                try:
                    barrier.wait(timeout=0.25)
                except BrokenBarrierError:
                    pass
                return f"2026-07-21T12:00:0{local.worker_index}Z"

            def call_public_api(worker_index):
                local.worker_index = worker_index
                try:
                    _, concurrent_bindings = load_live_career_bf(
                        root, "2026-07-21", [10],
                        fetcher=lambda *_args, **_kwargs: self.fail(
                            "completion recovery must not fetch"
                        ),
                    )
                    returned.append(concurrent_bindings[0])
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch(
                "mlb_backtest.live_history._canonical_utc_now",
                side_effect=durable_clock,
            ):
                threads = [
                    threading.Thread(target=call_public_api, args=(index,))
                    for index in (1, 2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(returned), 2)
            self.assertEqual(returned[0], returned[1])
            completion = json.loads(
                receipt.with_suffix(".completion.json").read_bytes()
            )
            self.assertEqual(
                {binding.durable_at_utc for binding in returned},
                {completion["durable_at_utc"]},
            )

    def test_identical_spawned_process_calls_share_winner_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, bindings = load_live_career_bf(
                root, "2026-07-21", [10],
                fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()),
            )
            receipt = root / bindings[0].receipt_relative
            receipt.with_suffix(".completion.json").unlink()
            receipt.with_suffix(".completion.sha256").unlink()
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            results = context.Queue()
            processes = [
                context.Process(
                    target=_career_completion_process_worker,
                    args=(str(root), index, barrier, results),
                )
                for index in (1, 2)
            ]
            try:
                for process in processes:
                    process.start()
                outputs = [results.get(timeout=10) for _process in processes]
                for process in processes:
                    process.join(timeout=10)
                self.assertTrue(all(not process.is_alive() for process in processes))
                self.assertTrue(all(process.exitcode == 0 for process in processes))
            except queue.Empty as exc:
                self.fail(f"spawned completion worker returned no result: {exc}")
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                results.close()
                results.join_thread()
            self.assertEqual([output[0] for output in outputs], ["ok", "ok"])
            self.assertEqual(outputs[0][1:], outputs[1][1:])
            completion = json.loads(
                receipt.with_suffix(".completion.json").read_bytes()
            )
            self.assertEqual(outputs[0][2], completion["durable_at_utc"])

    def test_conflicting_concurrent_fetchers_share_one_unambiguous_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_entered = threading.Event()
            release_first = threading.Event()
            returned, errors = [], []
            fetch_calls = []

            def call_public_api(payload, label):
                def fetcher(source, url, *, max_bytes):
                    fetch_calls.append(label)
                    if label == "first":
                        first_entered.set()
                        if not release_first.wait(timeout=2):
                            raise AssertionError("first concurrent fetch was not released")
                    return _http(url, payload)

                try:
                    returned.append(
                        load_live_career_bf(
                            root, "2026-07-21", [10], fetcher=fetcher
                        )[0]
                    )
                except BaseException as exc:
                    errors.append(exc)

            payloads = (
                _career_payload(),
                _career_payload().replace(b"123", b"124"),
            )
            threads = [
                threading.Thread(
                    target=call_public_api, args=(payloads[0], "first")
                ),
                threading.Thread(
                    target=call_public_api, args=(payloads[1], "second")
                ),
            ]
            threads[0].start()
            self.assertTrue(first_entered.wait(timeout=2))
            threads[1].start()
            release_first.set()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(returned, [{10: 123}, {10: 123}])
            self.assertEqual(fetch_calls, ["first"])
            cache = root / "data/experimental_daily/history_cache"
            self.assertEqual(len(tuple(cache.rglob("*.manifest.json"))), 1)
            receipts = root / "audit/experimental_daily/history_receipts"
            self.assertEqual(
                len(
                    tuple(
                        path for path in receipts.glob("history-*.json")
                        if not path.name.endswith(
                            (".claim.json", ".completion.json")
                        )
                    )
                ),
                1,
            )

    def test_interrupted_receipt_publication_rejects_mismatched_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, bindings = load_live_career_bf(root, "2026-07-21", [10], fetcher=lambda source, url, *, max_bytes: _http(url, _career_payload()))
            receipt = root / bindings[0].receipt_relative
            receipt.with_suffix(".completion.json").unlink()
            receipt.with_suffix(".completion.sha256").unlink()
            claim = receipt.with_suffix(".claim.json")
            value = json.loads(claim.read_text())
            value["receipt"]["etag"] = '"well-formed-forgery"'
            forged_receipt = live_history.canonical_json_bytes(value["receipt"])
            value["receipt_sha256"] = hashlib.sha256(forged_receipt).hexdigest()
            claim.write_bytes(live_history.canonical_json_bytes(value))
            with self.assertRaisesRegex(ValueError, "receipt"):
                load_live_career_bf(root, "2026-07-21", [10], fetcher=lambda *_args, **_kwargs: self.fail("mismatched residue must not fetch"))

    def test_league_era_transient_retry_uses_injected_sleeper(self):
        with tempfile.TemporaryDirectory() as temporary:
            delays = []
            base = _history_fetcher()
            attempts = 0

            def fetcher(source, url, *, max_bytes):
                nonlocal attempts
                if "/stats?" in url and attempts == 0:
                    attempts += 1
                    raise ConnectionError("transient")
                return base(source, url, max_bytes=max_bytes)

            bundle = build_live_history(Path(temporary), "2026-07-21", _card(), fetcher=fetcher, sleeper=delays.append)
        self.assertEqual(delays, [1])
        era_binding = next(binding for binding in bundle.source_bindings if binding.binding_role == "HISTORY_ERA")
        self.assertIn('"as_of_date":"2026-07-20"', era_binding.query_identity_json)

    def test_cross_path_swap_of_valid_statcast_receipt_quartets_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_live_history(root, "2026-09-01", _card(), fetcher=_month_boundary_fetcher(), sleeper=lambda _delay: None)
            build_live_history(root, "2026-09-02", _card(), fetcher=_month_boundary_fetcher(), sleeper=lambda _delay: None)
            directory = root / "audit/experimental_daily/history_receipts"
            receipts = sorted(path for path in directory.glob("history-history_statcast-*.json") if not path.name.endswith((".claim.json", ".completion.json")))
            first, second = receipts[0], receipts[-1]
            suffixes = (".json", ".claim.json", ".sha256", ".completion.json", ".completion.sha256")
            first_stem = first.name.removesuffix(".json")
            second_stem = second.name.removesuffix(".json")
            for suffix in suffixes:
                left, right = directory / f"{first_stem}{suffix}", directory / f"{second_stem}{suffix}"
                left_bytes, right_bytes = left.read_bytes(), right.read_bytes()
                left.write_bytes(right_bytes)
                right.write_bytes(left_bytes)
            with self.assertRaisesRegex(ValueError, "Statcast receipt"):
                build_live_history(root, "2026-09-02", _card(), fetcher=lambda *_args, **_kwargs: self.fail("swapped cache must not fetch"), sleeper=lambda _delay: None)

    def test_month_boundary_cutoff_reuses_a_prior_statcast_partition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_live_history(root, "2026-09-01", _card(), fetcher=_month_boundary_fetcher(), sleeper=lambda _delay: None)
            extended = build_live_history(root, "2026-09-02", _card(), fetcher=_month_boundary_fetcher(), sleeper=lambda _delay: None)
        self.assertEqual(extended.feature_history.max_pitch_row["game_date"], "2026-09-01")

    def test_cache_rejects_well_formed_receipt_metadata_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_live_history(root, "2026-07-21", _card(), fetcher=_history_fetcher(), sleeper=lambda _delay: None)
            receipt_path = next(
                path
                for path in (
                    root / "audit/experimental_daily/history_receipts"
                ).glob("history-*.json")
                if not path.name.endswith((".claim.json", ".completion.json"))
            )
            receipt = json.loads(receipt_path.read_text())
            receipt["etag"] = '"forged"'
            forged = live_history.canonical_json_bytes(receipt)
            receipt_path.write_bytes(forged)
            receipt_path.with_suffix(".sha256").write_text(
                f"{hashlib.sha256(forged).hexdigest()}\n"
            )
            with self.assertRaisesRegex(ValueError, "receipt"):
                build_live_history(root, "2026-07-21", _card(), fetcher=lambda *_args, **_kwargs: self.fail("tampered cache must not fetch"), sleeper=lambda _delay: None)

    def test_target_date_uses_exact_previous_day_cutoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = build_live_history(
                Path(temporary), "2026-07-21", _card(), fetcher=_history_fetcher(), sleeper=lambda _delay: None
            )
        self.assertEqual(bundle.cutoff_date, "2026-07-20")
        self.assertLess(bundle.feature_history.max_pitch_row["game_date"], "2026-07-21")
        self.assertEqual(
            {binding.binding_role for binding in bundle.source_bindings},
            {"HISTORY_SCHEDULE", "HISTORY_STATCAST", "HISTORY_VENUE", "HISTORY_ERA"},
        )
        self.assertEqual(bundle.league_era_by_asof["2026-07-20"]["value"], 4.25)

    def test_returned_feature_history_is_deeply_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = build_live_history(
                Path(temporary), "2026-07-21", _card(),
                fetcher=_history_fetcher(), sleeper=lambda _delay: None,
            )
        history = bundle.feature_history
        with self.assertRaises(TypeError):
            history.pitches[0]["game_date"] = "2026-07-21"
        with self.assertRaises((AttributeError, TypeError)):
            history.pitches = ()
        with self.assertRaises(TypeError):
            history.schedule_history.rows[0]["official_date"] = "2026-07-21"
        with self.assertRaises((AttributeError, TypeError)):
            history.schedule_history.rows = ()
        with self.assertRaises(TypeError):
            history.__dict__["pitches"] = ()
        with self.assertRaises(TypeError):
            history.schedule_history.__dict__["rows"] = ()

    def test_receipt_completion_is_sealed_and_supplies_postwrite_durable_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = build_live_history(root, "2026-07-21", _card(), fetcher=_history_fetcher(), sleeper=lambda _delay: None)
            receipt_path = next(path for path in (root / "audit/experimental_daily/history_receipts").glob("history-*.json") if not path.name.endswith((".claim.json", ".completion.json")))
            receipt = json.loads(receipt_path.read_text())
            completion = json.loads(receipt_path.with_suffix(".completion.json").read_text())
            receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        self.assertNotIn("durable_at_utc", receipt)
        self.assertEqual(completion["receipt_sha256"], receipt_sha256)
        self.assertIn(completion["durable_at_utc"], {binding.durable_at_utc for binding in bundle.source_bindings})

    def test_venue_reference_fails_closed_for_bad_coordinates_and_invalid_iana(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            live_history._parse_venue(b'{"venues":[]}', 1)
        invalid = json.loads(_venue())
        invalid["venues"][0]["location"]["defaultCoordinates"]["latitude"] = 100
        with self.assertRaisesRegex(ValueError, "coordinates"):
            live_history._parse_venue(json.dumps(invalid).encode(), 1)
        invalid = json.loads(_venue())
        invalid["venues"][0]["location"]["timeZone"]["id"] = "Mars/Olympus"
        with self.assertRaisesRegex(ValueError, "IANA"):
            live_history._parse_venue(json.dumps(invalid).encode(), 1)

    def test_target_day_statcast_poison_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "same-day leakage"):
                build_live_history(
                    Path(temporary), "2026-07-21", _card(),
                    fetcher=_history_fetcher(poison_target_day=True), sleeper=lambda _delay: None,
                )

    def test_validated_d1_cache_can_be_reused_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = build_live_history(root, "2026-07-21", _card(), fetcher=_history_fetcher(), sleeper=lambda _delay: None)
            second = build_live_history(
                root, "2026-07-21", _card(),
                fetcher=lambda *_args, **_kwargs: self.fail("cache reuse must not fetch"), sleeper=lambda _delay: None,
            )
        self.assertEqual(first.source_bindings, second.source_bindings)

    def test_tampered_d1_raw_cache_is_a_global_integrity_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_live_history(root, "2026-07-21", _card(), fetcher=_history_fetcher(), sleeper=lambda _delay: None)
            raw = next((root / "data/experimental_daily/history_cache").rglob("*.raw"))
            raw.write_bytes(b"tampered")
            with self.assertRaises((RuntimeError, ValueError)):
                build_live_history(
                    root, "2026-07-21", _card(),
                    fetcher=lambda *_args, **_kwargs: self.fail("invalid cache must not fetch"),
                    sleeper=lambda _delay: None,
                )

    def test_live_career_bf_url_uses_exact_d1_as_of_date(self):
        target = date.fromisoformat("2026-07-21")
        url = live_career_bf_url(543037, (target.fromordinal(target.toordinal() - 1)).isoformat())
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["endDate"], ["2026-07-20"])
        self.assertEqual(query["startDate"], ["1876-01-01"])

    def test_live_career_bf_cache_reuses_authenticated_response_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []

            def fetcher(source, url, *, max_bytes):
                calls.append((source, url, max_bytes))
                return _http(url, _career_payload())

            first, first_bindings = load_live_career_bf(
                root, "2026-07-21", [10], fetcher=fetcher
            )
            second, second_bindings = load_live_career_bf(
                root,
                "2026-07-21",
                [10],
                fetcher=lambda *_args, **_kwargs: self.fail("cache reuse must not fetch"),
            )
            self.assertEqual(first, {10: 123})
            self.assertEqual(first, second)
            self.assertEqual(first_bindings, second_bindings)
            self.assertEqual(len(calls), 1)
            self.assertEqual(first_bindings[0].binding_role, "CAREER_BF")
            self.assertIn('"as_of_date":"2026-07-20"', first_bindings[0].query_identity_json)


if __name__ == "__main__":
    unittest.main()
