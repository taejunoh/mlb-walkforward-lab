from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.error import URLError

from mlb_backtest.live_top3_contracts import canonical_json_bytes
from mlb_backtest.secure_fs import open_trusted_root


def _payload():
    from tests.test_live_top3_service import _build, _final_observer, _game, _slate

    game = _game(1)
    return _build(_slate((game,)), _final_observer((game,)))


def _rehash(payload):
    without_digest = dict(payload)
    without_digest.pop("content_sha256", None)
    payload["content_sha256"] = hashlib.sha256(
        canonical_json_bytes(without_digest)
    ).hexdigest()
    return payload


class DailyTop3Tests(unittest.TestCase):
    def test_parse_args_defaults_date_in_eastern_time(self):
        from mlb_backtest import daily_top3

        instant = datetime(2026, 7, 22, 2, 30, tzinfo=timezone.utc)
        with mock.patch.object(daily_top3, "utc_now", return_value=instant):
            args = daily_top3.parse_args([])

        self.assertEqual(args.date, "2026-07-21")

    def test_render_labels_research_only_and_keeps_unscored_unranked(self):
        from mlb_backtest import daily_top3
        from tests.test_live_top3_service import (
            _build,
            _final_observer,
            _game,
            _participants,
            _slate,
        )

        game = _game(1)
        game["away_team"] = "A|way"
        slate = _slate(
            (game,),
            participants={1: _participants(1, home_lineup=())},
        )
        payload = _build(slate, _final_observer((game,)))
        rendered = daily_top3.render_live_top3_markdown(payload).decode("utf-8")

        self.assertTrue(rendered.startswith("# Experimental MLB Daily Top 3\n"))
        for label in (
            "RESEARCH ONLY",
            "NON-OFFICIAL SAMPLE",
            "PROFITABILITY NOT VALIDATED",
        ):
            self.assertIn(label, rendered)
        self.assertIn("A\\|way", rendered)
        unscored = rendered.split("## Unscored games", 1)[1]
        self.assertNotIn("Probability", unscored)
        self.assertNotIn("Rank", unscored)

    def test_persist_writes_validated_json_then_markdown_in_experimental_namespace(self):
        from mlb_backtest import daily_top3

        payload = _payload()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            json_path, markdown_path = daily_top3.persist_live_top3_report(root, payload)

            self.assertTrue(json_path.is_absolute())
            self.assertTrue(markdown_path.is_absolute())
            self.assertIn(payload["content_sha256"][:16], json_path.name)
            self.assertEqual(
                json_path.parent.relative_to(root).parts[:2],
                ("reports", "experimental_daily"),
            )
            self.assertEqual(
                markdown_path.read_bytes(),
                daily_top3.render_live_top3_markdown(json.loads(json_path.read_bytes())),
            )
            original_json = json_path.read_bytes()
            original_markdown = markdown_path.read_bytes()
            with self.assertRaises(FileExistsError):
                daily_top3.persist_live_top3_report(root, payload)
            self.assertEqual(json_path.read_bytes(), original_json)
            self.assertEqual(markdown_path.read_bytes(), original_markdown)

    def test_distinct_execution_instants_preserve_two_report_pairs(self):
        from mlb_backtest import daily_top3

        first = _payload()
        second = json.loads(canonical_json_bytes(first))
        second["executed_at_eastern"] = "2025-05-01T08:00:01-04:00"
        second["executed_at_utc"] = "2025-05-01T12:00:01Z"
        _rehash(second)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            first_paths = daily_top3.persist_live_top3_report(root, first)
            second_paths = daily_top3.persist_live_top3_report(root, second)

            self.assertNotEqual(first_paths, second_paths)
            self.assertTrue(all(path.exists() for path in (*first_paths, *second_paths)))

    def test_markdown_write_failure_recovers_only_matching_json_residue(self):
        from mlb_backtest import daily_top3

        payload = _payload()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            real_write = daily_top3.write_exclusive
            calls = 0

            def fail_second_write(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected Markdown write failure")
                return real_write(*args, **kwargs)

            with mock.patch.object(
                daily_top3, "write_exclusive", side_effect=fail_second_write
            ):
                with self.assertRaises(OSError):
                    daily_top3.persist_live_top3_report(root, payload)

            report_dir = root / "reports/experimental_daily" / payload["target_date"]
            self.assertEqual(len(tuple(report_dir.glob("*.json"))), 1)
            self.assertEqual(tuple(report_dir.glob("*.md")), ())
            json_path, markdown_path = daily_top3.persist_live_top3_report(root, payload)
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())

    def test_renderer_html_and_markdown_escapes_external_cells(self):
        from mlb_backtest import daily_top3

        payload = _payload()
        payload["preliminary_top3"][0]["away_team"] = (
            '<img src=x onerror="alert(1)">|Away'
        )
        payload["warnings"] = ["<script>alert('x')</script>|warning"]
        _rehash(payload)

        rendered = daily_top3.render_live_top3_markdown(payload).decode("utf-8")

        self.assertNotIn("<img", rendered)
        self.assertNotIn("<script", rendered)
        self.assertIn("&lt;img src=x onerror=&quot;alert\\(1\\)&quot;&gt;\\|Away", rendered)
        self.assertIn(
            "&lt;script&gt;alert\\(&#x27;x&#x27;\\)&lt;/script&gt;\\|warning", rendered
        )

    def test_renderer_neutralizes_markdown_syntax_in_team_and_warning_cells(self):
        from mlb_backtest import daily_top3

        payload = _payload()
        hostile = '<b>![track](url) []()!*_~`</b>'
        payload["preliminary_top3"][0]["away_team"] = hostile
        payload["warnings"] = [hostile]
        _rehash(payload)

        rendered = daily_top3.render_live_top3_markdown(payload).decode("utf-8")

        self.assertNotIn("![track](url)", rendered)
        self.assertNotIn("<b>", rendered)
        expected = (
            "&lt;b&gt;\\!\\[track\\]\\(url\\) "
            "\\[\\]\\(\\)\\!\\*\\_\\~\\`&lt;/b&gt;"
        )
        self.assertIn(expected, rendered)

    def test_final_observer_uses_the_exact_saved_slate_game(self):
        from mlb_backtest import daily_top3

        saved_game = {"game_pk": 7, "identity": "do-not-rebuild"}
        slate = SimpleNamespace(
            games=(saved_game,),
            participants={
                7: SimpleNamespace(
                    away_pitcher_list_id=101, home_pitcher_list_id=202
                )
            },
        )
        fetcher = object()
        observed = object()

        def build_run(**kwargs):
            kwargs["final_schedule_observer"](7, "2026-07-21")
            return {"payload": "validated"}

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            card = root / "audit/v5_1_bt/model_card.json"
            card.parent.mkdir(parents=True)
            card.write_bytes(b"model card fixture")
            expected_paths = (root / "result.json", root / "result.md")
            with (
                mock.patch.object(
                    daily_top3, "load_frozen_live_card_bytes", return_value=object()
                ),
                mock.patch.object(
                    daily_top3, "build_live_history", return_value=object()
                ) as history,
                mock.patch.object(daily_top3, "observe_live_slate", return_value=slate),
                mock.patch.object(daily_top3, "load_live_career_bf", return_value=({}, ())) as career,
                mock.patch.object(daily_top3, "observe_final_game_schedule", return_value=observed) as final,
                mock.patch.object(daily_top3, "build_live_top3_run", side_effect=build_run),
                mock.patch.object(daily_top3, "validate_run_payload", side_effect=lambda value: value),
                mock.patch.object(
                    daily_top3,
                    "_persist_bound_live_top3_report",
                    return_value=expected_paths,
                ) as persist,
            ):
                result = daily_top3.run_daily_top3(
                    root,
                    "2026-07-21",
                    Path("audit/v5_1_bt/model_card.json"),
                    fetcher=fetcher,
                    utc_clock=datetime(2026, 7, 21, 16, tzinfo=timezone.utc),
                )

        self.assertEqual(result, ({"payload": "validated"}, *expected_paths))
        self.assertIsNotNone(history.call_args.kwargs["expected_root_binding"])
        self.assertEqual(career.call_args.args[2], (101, 202))
        self.assertIsNotNone(career.call_args.kwargs["expected_root_binding"])
        self.assertEqual(final.call_args.args, (root, "2026-07-21", saved_game))
        self.assertEqual(final.call_args.kwargs["fetcher"], fetcher)
        self.assertIsNotNone(final.call_args.kwargs["expected_root_binding"])
        self.assertEqual(persist.call_args.args[0], root)
        self.assertEqual(persist.call_args.args[2], {"payload": "validated"})

    def test_main_does_not_persist_when_integrity_validation_fails(self):
        from mlb_backtest import daily_top3
        from mlb_backtest.live_top3_contracts import IntegrityError

        with mock.patch.object(
            daily_top3, "run_daily_top3", side_effect=IntegrityError("bad payload")
        ):
            status = daily_top3.main([])

        self.assertEqual(status, 2)

    def test_model_card_symlink_escape_is_rejected_before_loader_read(self):
        from mlb_backtest import daily_top3

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            outside = base / "outside-model-card.json"
            outside.write_bytes(b"outside sentinel")
            (root / "card.json").symlink_to(outside)
            with mock.patch.object(daily_top3, "load_frozen_live_card_bytes") as loader:
                with self.assertRaises(ValueError):
                    daily_top3.run_daily_top3(
                        root,
                        "2026-07-21",
                        Path("card.json"),
                        utc_clock=datetime(2026, 7, 21, 16, tzinfo=timezone.utc),
                    )
            loader.assert_not_called()
            self.assertEqual(outside.read_bytes(), b"outside sentinel")

    def test_model_card_parent_and_absolute_outside_are_rejected_before_read(self):
        from mlb_backtest import daily_top3

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            outside = base / "outside.json"
            outside.write_bytes(b"outside sentinel")
            for supplied in (Path("../outside.json"), outside):
                with self.subTest(supplied=supplied), mock.patch.object(
                    daily_top3, "load_frozen_live_card_bytes"
                ) as loader:
                    with self.assertRaises(ValueError):
                        daily_top3.run_daily_top3(
                            root,
                            "2026-07-21",
                            supplied,
                            utc_clock=datetime(
                                2026, 7, 21, 16, tzinfo=timezone.utc
                            ),
                        )
                    loader.assert_not_called()
            self.assertEqual(outside.read_bytes(), b"outside sentinel")

    def test_root_swap_during_history_aborts_before_slate_observation(self):
        from mlb_backtest import daily_top3

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            root = base / "root"
            moved = base / "moved"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            card = root / "card.json"
            card.write_bytes(b"model card fixture")

            def swap_root(*_args, **_kwargs):
                root.rename(moved)
                root.symlink_to(outside, target_is_directory=True)
                return object()

            with (
                mock.patch.object(
                    daily_top3, "load_frozen_live_card_bytes", return_value=object()
                ),
                mock.patch.object(
                    daily_top3, "build_live_history", side_effect=swap_root
                ),
                mock.patch.object(daily_top3, "observe_live_slate") as slate,
            ):
                with self.assertRaises(ValueError):
                    daily_top3.run_daily_top3(
                        root,
                        "2026-07-21",
                        card,
                        utc_clock=datetime(
                            2026, 7, 21, 16, tzinfo=timezone.utc
                        ),
                    )
            slate.assert_not_called()
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_daily_binding_rejects_directory_replacement_at_history_or_career_entry(self):
        from mlb_backtest import daily_top3, live_history

        for phase in ("history", "career"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                dir=Path.cwd()
            ) as temporary:
                base = Path(temporary)
                root = base / "root"
                moved = base / "moved-root"
                replacement = base / "replacement"
                root.mkdir()
                replacement.mkdir()
                card = root / "card.json"
                card.write_bytes(b"model card fixture")
                slate_value = SimpleNamespace(
                    games=(),
                    participants={
                        1: SimpleNamespace(
                            away_pitcher_list_id=101,
                            home_pitcher_list_id=202,
                        )
                    },
                )

                def swap_root_once():
                    root.rename(moved)
                    replacement.rename(root)

                def history_entry(*args, **kwargs):
                    swap_root_once()
                    return live_history.build_live_history(*args, **kwargs)

                def career_entry(*args, **kwargs):
                    swap_root_once()
                    return live_history.load_live_career_bf(*args, **kwargs)

                history_side_effect = history_entry if phase == "history" else None
                career_side_effect = career_entry if phase == "career" else None
                with (
                    mock.patch.object(
                        daily_top3,
                        "load_frozen_live_card_bytes",
                        return_value=object(),
                    ),
                    mock.patch.object(
                        daily_top3,
                        "build_live_history",
                        side_effect=history_side_effect,
                        return_value=object(),
                    ),
                    mock.patch.object(
                        daily_top3,
                        "observe_live_slate",
                        return_value=slate_value,
                    ) as observe_slate,
                    mock.patch.object(
                        daily_top3,
                        "load_live_career_bf",
                        side_effect=career_side_effect,
                        return_value=({}, ()),
                    ),
                ):
                    with self.assertRaises(ValueError):
                        daily_top3.run_daily_top3(
                            root,
                            "2026-07-21",
                            Path("card.json"),
                            utc_clock=datetime(
                                2026, 7, 21, 16, tzinfo=timezone.utc
                            ),
                        )
                if phase == "history":
                    observe_slate.assert_not_called()
                self.assertEqual(tuple(root.iterdir()), ())

    def test_model_card_snapshot_bytes_survive_leaf_symlink_or_fifo_swap(self):
        from mlb_backtest import daily_top3
        from mlb_backtest.live_model_card import load_frozen_live_card_bytes

        raw = (
            Path(__file__).resolve().parents[1]
            / "audit/v5_1_bt/model_card.json"
        ).read_bytes()
        for replacement in ("symlink", "fifo"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory(
                dir=Path.cwd()
            ) as temporary:
                base = Path(temporary)
                root = base / "root"
                root.mkdir()
                card = root / "card.json"
                card.write_bytes(raw)
                outside = base / "outside-card.json"
                outside.write_bytes(b"OUTSIDE-SENTINEL")

                def swap_leaf(stage, relative):
                    if stage != "after_snapshot" or relative != Path("card.json"):
                        return
                    card.unlink()
                    if replacement == "symlink":
                        card.symlink_to(outside)
                    else:
                        os.mkfifo(card)

                with (
                    mock.patch.object(
                        daily_top3,
                        "_model_card_snapshot_test_hook",
                        side_effect=swap_leaf,
                    ),
                    mock.patch.object(
                        daily_top3,
                        "load_frozen_live_card_bytes",
                        wraps=load_frozen_live_card_bytes,
                    ) as bytes_loader,
                    open_trusted_root(root) as trusted,
                ):
                    with self.assertRaises(ValueError):
                        daily_top3._load_bound_model_card(trusted, "card.json")

                bytes_loader.assert_called_once_with(raw)

    def test_live_root_normalization_accepts_default_tempdir_but_rejects_nested_symlink(self):
        from mlb_backtest import daily_top3

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            normalized = daily_top3.normalize_live_root_path(root)
            self.assertEqual(normalized, Path(os.path.realpath(root)))
            json_path, markdown_path = daily_top3.persist_live_top3_report(
                root, _payload()
            )
            self.assertTrue(json_path.is_relative_to(normalized))
            self.assertTrue(markdown_path.is_relative_to(normalized))
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            nested = base / "nested"
            nested.symlink_to(outside, target_is_directory=True)
            requested = nested / "live-root"
            normalized = daily_top3.normalize_live_root_path(requested)

            self.assertEqual(normalized, requested.absolute())
            with self.assertRaises(ValueError):
                open_trusted_root(normalized)

    def test_main_exit_codes_for_invalid_date_network_and_interrupt(self):
        from mlb_backtest import daily_top3

        self.assertEqual(daily_top3.main(["--date", "not-a-date"]), 2)
        for error, expected in ((URLError("offline"), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(error=type(error).__name__), mock.patch.object(
                daily_top3, "run_daily_top3", side_effect=error
            ):
                self.assertEqual(daily_top3.main([]), expected)

    def test_main_normal_summary_and_json_only_stdout_contract(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io

        from mlb_backtest import daily_top3

        payload = _payload()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            paths = (root / "report.json", root / "report.md")
            with mock.patch.object(
                daily_top3, "run_daily_top3", return_value=(payload, *paths)
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    self.assertEqual(daily_top3.main([]), 0)
                summary = stdout.getvalue()
                self.assertIn("Verified Top 3: 0", summary)
                self.assertIn("Preliminary Top 3: 1", summary)
                self.assertIn(str(paths[0].absolute()), summary)
                self.assertIn(str(paths[1].absolute()), summary)

                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    self.assertEqual(daily_top3.main(["--json-only"]), 0)
                self.assertEqual(json.loads(stdout.getvalue()), payload)


if __name__ == "__main__":
    unittest.main()
