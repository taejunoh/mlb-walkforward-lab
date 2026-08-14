import ast
import hashlib
import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import mlb_backtest.live_features as live_features
from mlb_backtest.feature_engine import FeatureHistory, build_raw_game_features
from mlb_backtest.live_features import build_live_raw_game_features
from mlb_backtest.live_sources import PregameParticipants
from mlb_backtest.live_top3_contracts import (
    FinalGameObservation,
    LiveHistoryBundle,
    SourceBinding,
    canonical_json_bytes,
)
from mlb_backtest.statcast import StatcastData


TARGET = "2025-05-01"
CUTOFF = "2025-04-30"
GAME_PK = 900
FORBIDDEN_LIVE_IMPORT_NAMES = frozenset(
    {"outcome", "outcomes", "scoring", "ranking", "market", "odds"}
)


def _forbidden_live_imports(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    candidates: list[str] = []
    importlib_aliases = {"importlib"}
    builtins_aliases = {"builtins"}
    dynamic_import_aliases = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                candidates.append(alias.name)
                if alias.asname:
                    candidates.append(alias.asname)
                bound_name = alias.asname or alias.name.split(".", 1)[0]
                if alias.name == "importlib" or alias.name.startswith("importlib."):
                    importlib_aliases.add(bound_name)
                if alias.name == "builtins" or alias.name.startswith("builtins."):
                    builtins_aliases.add(bound_name)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            if module:
                candidates.append(module)
            for alias in node.names:
                candidates.extend((alias.name, f"{module}.{alias.name}"))
                if alias.asname:
                    candidates.append(alias.asname)
                if node.module == "importlib" and alias.name == "import_module":
                    dynamic_import_aliases.add(alias.asname or alias.name)
                if node.module == "builtins" and alias.name == "__import__":
                    dynamic_import_aliases.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dynamic_import = (
            isinstance(node.func, ast.Name)
            and node.func.id in dynamic_import_aliases
        ) or (
            isinstance(node.func, ast.Attribute)
            and (
                node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_aliases
                or node.func.attr == "__import__"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in builtins_aliases
            )
        )
        if not dynamic_import:
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(
            node.args[0].value, str
        ):
            candidates.append("<dynamic-import>")
        else:
            candidates.append(node.args[0].value)
    violations = set()
    for candidate in candidates:
        segments = {
            segment.lower()
            for segment in candidate.lstrip(".").split(".")
            if segment
        }
        if candidate == "<dynamic-import>" or segments & FORBIDDEN_LIVE_IMPORT_NAMES:
            violations.add(candidate)
    return tuple(sorted(violations))


def _binding(role: str, index: int, *, game_pks: tuple[int, ...] = ()) -> SourceBinding:
    observation_id = f"obs-{index}"
    url = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
    identity = {
        "url": url,
        "target_date": TARGET if role == "FINAL_SCHEDULE" else CUTOFF,
        "observation_id": observation_id,
    }
    if role != "FINAL_SCHEDULE":
        identity["as_of_date"] = CUTOFF
        del identity["observation_id"]
    value = {
        "source": "stats_api",
        "binding_role": role,
        "game_pks": game_pks,
        "observation_id": observation_id,
        "query_identity_json": canonical_json_bytes(identity)
        .decode("utf-8")
        .removesuffix("\n"),
        "requested_url": url,
        "final_url": url,
        "response_completed_at_utc": "2025-05-01T12:00:00Z",
        "durable_at_utc": "2025-05-01T12:00:01Z",
        "http_status": 200,
        "content_type": "application/json",
        "etag": '"abc"',
        "last_modified": None,
        "payload_sha256": "a" * 64,
        "byte_length": 12,
        "payload_relative": f"data/experimental_daily/{role.lower()}-{index}.json",
        "manifest_sha256": "b" * 64,
        "receipt_relative": f"audit/experimental_daily/{role.lower()}-{index}.json",
        "receipt_sha256": "c" * 64,
    }
    value["binding_sha256"] = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return SourceBinding(**value)


def _pa(
    game_pk: int,
    day: str,
    pitcher: int,
    batter: int,
    *,
    hand: str,
    home: str,
    away: str,
    top: str,
    at_bat: int,
) -> dict[str, object]:
    return {
        "game_date": day,
        "game_pk": game_pk,
        "pitcher_id": pitcher,
        "batter_id": batter,
        "events": "field_out",
        "description": "terminal",
        "p_throws": hand,
        "home_team": home,
        "away_team": away,
        "inning": 1,
        "inning_topbot": top,
        "release_speed": 95.0,
        "woba_value": 0.0,
        "woba_denom": 1.0,
        "estimated_woba_using_speedangle": 0.2,
        "launch_speed_angle": 2.0,
        "at_bat_number": at_bat,
        "pitch_number": 1,
        "home_score": 0,
        "away_score": 0,
        "post_home_score": 0,
        "post_away_score": 0,
        "outs_on_play": 1,
        "game_type": "R",
        "event_id": f"{game_pk}:{at_bat}",
    }


def _history(*, extra_rows: tuple[dict[str, object], ...] = ()) -> FeatureHistory:
    rows = (
        _pa(1, CUTOFF, 501, 101, hand="L", home="HOM", away="AWY", top="Top", at_bat=1),
        _pa(1, CUTOFF, 601, 201, hand="R", home="HOM", away="AWY", top="Bot", at_bat=2),
        *extra_rows,
    )
    ordered = tuple(
        sorted(rows, key=lambda row: (row["game_date"], row["game_pk"], row["at_bat_number"]))
    )
    return FeatureHistory(
        StatcastData(ordered, ordered),
        slot_weights={slot: 1 / 9 for slot in range(1, 10)},
        schedule_coverage_start=CUTOFF,
        schedule_coverage_end=CUTOFF,
        schedule_coverage_complete=True,
    )


def _travel_history() -> FeatureHistory:
    base = _history()
    return FeatureHistory(
        StatcastData(base.pitches, base.plate_appearances),
        slot_weights={slot: 1 / 9 for slot in range(1, 10)},
        schedule_history=(
            {
                "game_pk": 700,
                "official_date": CUTOFF,
                "team_codes": ("AWY", "T00"),
                "home_team_code": "T00",
                "venue_name": "West Park",
                "innings": 9,
            },
        ),
        schedule_coverage_start="2025-04-28",
        schedule_coverage_end=CUTOFF,
        schedule_coverage_complete=True,
        venue_by_name={
            "Park": {
                "city": "New York",
                "latitude": 40.8296,
                "longitude": -73.9262,
                "time_zone": "America/New_York",
            },
            "West Park": {
                "city": "Los Angeles",
                "latitude": 34.0522,
                "longitude": -118.2437,
                "time_zone": "America/Los_Angeles",
            },
        },
    )


def _team_map() -> dict[str, str]:
    value = {"Away": "AWY", "Home": "HOM"}
    value.update({f"Team {index}": f"T{index:02d}" for index in range(28)})
    return value


def _era() -> dict[str, dict[str, object]]:
    return {
        CUTOFF: {
            "as_of_date": CUTOFF,
            "value": 4.0,
            "earned_runs": 4,
            "outs": 27,
            "source": "mlb_stats_api",
            "manifest_sha256": "d" * 64,
        }
    }


def _bundle(**overrides: object) -> LiveHistoryBundle:
    values = {
        "target_date": TARGET,
        "cutoff_date": CUTOFF,
        "feature_history": _history(),
        "league_era_by_asof": _era(),
        "team_code_by_name": _team_map(),
        "venue_references": {
            33: {
                "venue_name": "Park",
                "venue_city": "New York",
                "venue_latitude": 40.8296,
                "venue_longitude": -73.9262,
                "time_zone": "America/New_York",
            }
        },
        "cache_authority": {
            "namespace": "experimental_daily/history_cache",
            "cutoff_date": CUTOFF,
            "catalog_sha256": "e" * 64,
        },
        "source_bindings": tuple(
            _binding(role, index)
            for index, role in enumerate(
                (
                    "HISTORY_SCHEDULE",
                    "HISTORY_STATCAST",
                    "HISTORY_ERA",
                    "HISTORY_VENUE",
                ),
                1,
            )
        ),
    }
    values.update(overrides)
    return LiveHistoryBundle(**values)


def _projected_game(**changes: object) -> dict[str, object]:
    value = {
        "game_pk": GAME_PK,
        "official_date": TARGET,
        "game_type": "R",
        "scheduled_start_utc": "2025-05-01T23:00:00Z",
        "status": "Pre-Game",
        "away_team": "Away",
        "away_team_id": 110,
        "home_team": "Home",
        "home_team_id": 120,
        "away_probable_pitcher_id": 501,
        "away_probable_pitcher_name": "Away Pitcher",
        "home_probable_pitcher_id": 601,
        "home_probable_pitcher_name": "Home Pitcher",
        "venue_id": 33,
        "venue_name": "Park",
        "doubleheader_marker": None,
        "away_lineup": [],
        "home_lineup": [],
    }
    value.update(changes)
    return value


def _final(**changes: object) -> FinalGameObservation:
    projected = _projected_game(**changes)
    game_pk = int(projected["game_pk"])
    target = str(projected["official_date"])
    return FinalGameObservation(
        game_pk,
        target,
        projected,
        _binding("FINAL_SCHEDULE", 10, game_pks=(game_pk,)),
    )


def _participants(**changes: object) -> PregameParticipants:
    values = {
        "game_pk": GAME_PK,
        "away_team_id": 110,
        "home_team_id": 120,
        "away_pitcher_list_id": 501,
        "home_pitcher_list_id": 601,
        "away_lineup": tuple(range(201, 210)),
        "home_lineup": tuple(range(101, 110)),
        "complete": True,
        "reason_codes": ("PROBABLE_CONSISTENT",),
    }
    values.update(changes)
    return PregameParticipants(**values)


class LiveFeatureTests(unittest.TestCase):
    def test_prepared_authority_matches_legacy_builder_output(self):
        bundle = _bundle()
        prepared = live_features.prepare_live_feature_history(
            bundle, target_game_pks=(GAME_PK,)
        )

        legacy = build_live_raw_game_features(
            _participants(), _final(), bundle, {501: 1_234, 601: 2_345}
        )
        reused = build_live_raw_game_features(
            _participants(),
            _final(),
            bundle,
            {501: 1_234, 601: 2_345},
            prepared_history=prepared,
        )

        self.assertIsInstance(
            prepared, live_features.PreparedLiveFeatureHistory
        )
        self.assertEqual(reused, legacy)

    def test_prepared_authority_is_deeply_read_only_between_candidates(self):
        bundle = _bundle()
        prepared = live_features.prepare_live_feature_history(
            bundle, target_game_pks=(GAME_PK, 901)
        )
        before = build_live_raw_game_features(
            _participants(),
            _final(),
            bundle,
            {501: 1, 601: 2},
            prepared_history=prepared,
        )

        with self.assertRaises(TypeError):
            prepared.feature_history.pitches[0]["events"] = "home_run"
        with self.assertRaises(AttributeError):
            prepared.feature_history.pitches = ()
        with self.assertRaises(AttributeError):
            prepared.feature_history.schedule_history.rows = ()
        with self.assertRaises(TypeError):
            prepared.feature_history.pa_by_batter[101] = ()

        after = build_live_raw_game_features(
            _participants(),
            _final(),
            bundle,
            {501: 1, 601: 2},
            prepared_history=prepared,
        )
        self.assertEqual(after, before)

    def test_prepared_history_namespaces_reject_direct_dict_mutation(self):
        prepared = live_features.prepare_live_feature_history(
            _bundle(), target_game_pks=(GAME_PK,)
        )
        feature_namespace = prepared.feature_history.__dict__
        schedule_namespace = prepared.feature_history.schedule_history.__dict__
        seal = prepared._seal

        for label, namespace in (
            ("feature", feature_namespace),
            ("schedule", schedule_namespace),
        ):
            with self.subTest(namespace=label), self.assertRaises(TypeError):
                namespace["forged"] = object()
            for method, arguments in (
                ("update", ({"forged": object()},)),
                ("pop", ("_sealed",)),
                ("setdefault", ("forged", object())),
            ):
                with self.subTest(namespace=label, method=method), self.assertRaises(
                    (AttributeError, TypeError)
                ):
                    getattr(namespace, method)(*arguments)

        with self.assertRaises(TypeError):
            prepared.feature_history.pitches[0]["events"] = "home_run"
        self.assertIs(prepared._seal, seal)

    def test_prepare_rejects_any_target_game_pk_in_raw_history(self):
        target_row = _pa(
            GAME_PK,
            CUTOFF,
            501,
            101,
            hand="L",
            home="HOM",
            away="AWY",
            top="Top",
            at_bat=9,
        )
        pitch_history = _history()
        pitch_history.pitches = (*pitch_history.pitches, target_row)
        pa_history = _history()
        pa_history.plate_appearances = (
            *pa_history.plate_appearances,
            target_row,
        )
        schedule_history = _history()
        schedule_history.schedule_history.rows = (
            *schedule_history.schedule_history.rows,
            {
                "game_pk": GAME_PK,
                "official_date": CUTOFF,
                "team_codes": ("AWY", "HOM"),
                "home_team_code": "HOM",
                "venue_name": "Park",
                "innings": 9,
            },
        )

        for history in (pitch_history, pa_history, schedule_history):
            with self.subTest(history=history), self.assertRaisesRegex(
                ValueError, "AS_OF_VIOLATION"
            ):
                live_features.prepare_live_feature_history(
                    _bundle(feature_history=history),
                    target_game_pks=(GAME_PK,),
                )

    def test_prepared_authority_rejects_forgery_other_bundle_and_unsealed_game(self):
        bundle = _bundle()
        prepared = live_features.prepare_live_feature_history(
            bundle, target_game_pks=(GAME_PK,)
        )
        forged = live_features.PreparedLiveFeatureHistory(
            bundle,
            prepared.target_game_pks,
            _history(),
            prepared.venue_by_id,
            prepared.venue_by_name,
        )
        cases = (
            (bundle, _participants(), _final(), forged),
            (_bundle(), _participants(), _final(), prepared),
            (
                bundle,
                _participants(game_pk=901),
                _final(game_pk=901),
                prepared,
            ),
        )

        for candidate_bundle, participants, final, authority in cases:
            with self.subTest(final_game_pk=final.game_pk), self.assertRaisesRegex(
                ValueError, "INVALID_PREPARED_HISTORY"
            ):
                build_live_raw_game_features(
                    participants,
                    final,
                    candidate_bundle,
                    {501: 1, 601: 2},
                    prepared_history=authority,
                )

    def test_fifty_thousand_rows_are_detached_and_indexed_only_once(self):
        rows = tuple(
            _pa(
                10_000 + index,
                CUTOFF,
                501 if index % 2 == 0 else 601,
                101 if index % 2 == 0 else 201,
                hand="L" if index % 2 == 0 else "R",
                home="HOM",
                away="AWY",
                top="Top" if index % 2 == 0 else "Bot",
                at_bat=1,
            )
            for index in range(50_000)
        )
        history = FeatureHistory(
            StatcastData(rows, rows),
            slot_weights={slot: 1 / 9 for slot in range(1, 10)},
            schedule_coverage_start=CUTOFF,
            schedule_coverage_end=CUTOFF,
            schedule_coverage_complete=True,
        )
        bundle = _bundle(feature_history=history)

        with mock.patch.object(
            live_features,
            "_detach_history_rows",
            wraps=live_features._detach_history_rows,
        ) as detach_rows, mock.patch.object(
            live_features,
            "_SealedScheduleHistory",
            wraps=live_features._SealedScheduleHistory,
        ) as schedule_constructor, mock.patch.object(
            live_features,
            "_SealedFeatureHistory",
            wraps=live_features._SealedFeatureHistory,
        ) as feature_constructor:
            prepared = live_features.prepare_live_feature_history(
                bundle, target_game_pks=tuple(range(901, 916))
            )

        self.assertIsInstance(
            prepared.feature_history, FeatureHistory
        )
        self.assertEqual(detach_rows.call_count, 3)
        self.assertEqual(schedule_constructor.call_count, 1)
        self.assertEqual(feature_constructor.call_count, 1)

    def test_consistent_probable_participants_build_result_free_raw_row(self):
        raw = build_live_raw_game_features(
            _participants(), _final(), _bundle(), {501: 1_234, 601: 2_345}
        )

        self.assertTrue(raw["eligible"])
        self.assertEqual(raw["reasons"], [])
        self.assertEqual(raw["starter_provenance"], "PROBABLE_CONSISTENT")
        self.assertEqual(raw["lineup_provenance"], "OBSERVED_PREGAME_LINEUP")
        self.assertEqual(raw["away"]["starter"]["career_bf"], 1_234)
        self.assertEqual(raw["home"]["starter"]["career_bf"], 2_345)
        self.assertEqual(
            [row["player_id"] for row in raw["home"]["offense"]["players"]],
            list(range(101, 110)),
        )
        encoded = json.dumps(raw, sort_keys=True)
        self.assertNotIn("RECONSTRUCTED_ACTUAL_STARTER", encoded)
        self.assertNotIn("RECONSTRUCTED_FINAL_LINEUP", encoded)
        self.assertNotIn("ANNOUNCED", encoded)

    def test_shared_math_matches_historical_output_except_live_provenance_and_career_bf(self):
        bundle = _bundle()
        live = build_live_raw_game_features(
            _participants(), _final(), bundle, {501: 1, 601: 2}
        )
        historical_game = {
            **_projected_game(status="Final"),
            "away_actual_starter_id": 501,
            "home_actual_starter_id": 601,
            "starter_provenance": "RECONSTRUCTED_ACTUAL_STARTER",
            "away_lineup": [{"player_id": value} for value in range(201, 210)],
            "home_lineup": [{"player_id": value} for value in range(101, 110)],
        }
        historical = build_raw_game_features(
            historical_game,
            bundle.feature_history,
            bundle.team_code_by_name,
            bundle.league_era_by_asof,
        )
        historical["away"]["starter"]["career_bf"] = 1
        historical["home"]["starter"]["career_bf"] = 2
        historical["starter_provenance"] = "PROBABLE_CONSISTENT"
        historical["lineup_provenance"] = "OBSERVED_PREGAME_LINEUP"

        self.assertEqual(live, historical)

    def test_detached_rebuild_preserves_authenticated_prior_venue_travel_parity(self):
        history = _travel_history()
        venue_references = {
            **dict(_bundle().venue_references),
            44: {
                "venue_name": "West Park",
                "venue_city": "Los Angeles",
                "venue_latitude": 34.0522,
                "venue_longitude": -118.2437,
                "time_zone": "America/Los_Angeles",
            },
        }
        bundle = _bundle(
            feature_history=history,
            venue_references=venue_references,
        )
        live = build_live_raw_game_features(
            _participants(), _final(), bundle, {501: 1, 601: 2}
        )
        historical_game = {
            **_projected_game(status="Final"),
            "away_actual_starter_id": 501,
            "home_actual_starter_id": 601,
            "starter_provenance": "RECONSTRUCTED_ACTUAL_STARTER",
            "away_lineup": [{"player_id": value} for value in range(201, 210)],
            "home_lineup": [{"player_id": value} for value in range(101, 110)],
        }
        historical = build_raw_game_features(
            historical_game,
            history,
            bundle.team_code_by_name,
            bundle.league_era_by_asof,
        )
        historical["away"]["starter"]["career_bf"] = 1
        historical["home"]["starter"]["career_bf"] = 2
        historical["starter_provenance"] = "PROBABLE_CONSISTENT"
        historical["lineup_provenance"] = "OBSERVED_PREGAME_LINEUP"

        self.assertEqual(historical["away"]["schedule"]["value"], 34.0)
        self.assertEqual(historical["away"]["schedule"]["quality"], 5 / 6)
        self.assertEqual(live, historical)

    def test_starter_conflict_and_partial_lineup_fail_closed(self):
        cases = (
            (
                _participants(complete=False, reason_codes=("STARTER_CONFLICT",)),
                "STARTER_CONFLICT",
            ),
            (
                _participants(complete=False, home_lineup=()),
                "INVALID_HOME_LINEUP",
            ),
            (
                _participants(
                    home_lineup=(101, 101, 102, 103, 104, 105, 106, 107, 108)
                ),
                "INVALID_HOME_LINEUP",
            ),
            (
                _participants(
                    complete=False, reason_codes=("starter_conflict",)
                ),
                "INVALID_PARTICIPANT_PROVENANCE",
            ),
        )
        for participants, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                build_live_raw_game_features(
                    participants, _final(), _bundle(), {501: 1, 601: 2}
                )

    def test_final_projection_rejects_target_results_or_participant_fields(self):
        for final, reason in (
            (_final(away_score=9), "INVALID_FINAL_GAME_PROJECTION"),
            (_final(home_lineup=[101]), "TARGET_PARTICIPANT_LEAKAGE"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                build_live_raw_game_features(
                    _participants(), final, _bundle(), {501: 1, 601: 2}
                )

    def test_exact_game_team_starter_and_date_identities_are_required(self):
        cases = (
            (_participants(game_pk=901), _final(), _bundle(), "GAME_IDENTITY_MISMATCH"),
            (_participants(away_team_id=999), _final(), _bundle(), "TEAM_IDENTITY_MISMATCH"),
            (_participants(away_pitcher_list_id=999), _final(), _bundle(), "STARTER_CONFLICT"),
            (_participants(), _final(away_probable_pitcher_id=999), _bundle(), "STARTER_CONFLICT"),
            (
                _participants(),
                _final(official_date="2025-05-02"),
                _bundle(),
                "TARGET_DATE_MISMATCH",
            ),
        )
        for participants, final, bundle, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                build_live_raw_game_features(
                    participants, final, bundle, {501: 1, 601: 2}
                )

    def test_status_start_venue_team_map_and_era_fail_closed(self):
        bad_map = _team_map()
        bad_map.pop("Team 0")
        bad_era = _era()
        bad_era[CUTOFF] = {**bad_era[CUTOFF], "value": 4.1}
        cases = (
            (_final(status="Final"), _bundle(), "PREGAME_STATUS_NOT_ACCEPTED"),
            (_final(scheduled_start_utc="not-utc"), _bundle(), "INVALID_SCHEDULED_START_UTC"),
            (_final(venue_name="Other Park"), _bundle(), "VENUE_IDENTITY_MISMATCH"),
            (_final(), _bundle(team_code_by_name=bad_map), "INVALID_TEAM_MAP"),
            (_final(), _bundle(league_era_by_asof=bad_era), "INVALID_LEAGUE_ERA_PROVENANCE"),
        )
        for final, bundle, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                build_live_raw_game_features(
                    _participants(), final, bundle, {501: 1, 601: 2}
                )

    def test_venue_reference_requires_coordinates_and_valid_iana_zone(self):
        base = dict(_bundle().venue_references[33])
        invalid_refs = (
            {33: {**base, "venue_latitude": 91.0}},
            {33: {**base, "time_zone": "Eastern Standard Time"}},
            {33: {**base, "time_zone": "\x00"}},
            {},
        )
        for venue_references in invalid_refs:
            with self.subTest(venue_references=venue_references), self.assertRaisesRegex(
                ValueError, "INVALID_VENUE_REFERENCE"
            ):
                build_live_raw_game_features(
                    _participants(),
                    _final(),
                    _bundle(venue_references=venue_references),
                    {501: 1, 601: 2},
                )

    def test_target_or_future_history_is_rejected_instead_of_silently_bounded(self):
        for day in (TARGET, "2025-05-02"):
            poisoned = _pa(
                99,
                day,
                501,
                101,
                hand="L",
                home="HOM",
                away="AWY",
                top="Top",
                at_bat=1,
            )
            with self.subTest(day=day), self.assertRaisesRegex(
                ValueError, "AS_OF_VIOLATION"
            ):
                build_live_raw_game_features(
                    _participants(),
                    _final(),
                    _bundle(feature_history=_history(extra_rows=(poisoned,))),
                    {501: 1, 601: 2},
                )

        target_identity_poison = _pa(
            GAME_PK,
            CUTOFF,
            501,
            101,
            hand="L",
            home="HOM",
            away="AWY",
            top="Top",
            at_bat=3,
        )
        with self.assertRaisesRegex(ValueError, "AS_OF_VIOLATION"):
            build_live_raw_game_features(
                _participants(),
                _final(),
                _bundle(
                    feature_history=_history(extra_rows=(target_identity_poison,))
                ),
                {501: 1, 601: 2},
            )

    def test_post_construction_rows_cannot_hide_behind_stale_history_metadata(self):
        statcast_history = _history()
        target_row = _pa(
            GAME_PK,
            TARGET,
            501,
            101,
            hand="L",
            home="HOM",
            away="AWY",
            top="Top",
            at_bat=9,
        )
        statcast_history.pitches = (*statcast_history.pitches, target_row)
        statcast_history.plate_appearances = (
            *statcast_history.plate_appearances,
            target_row,
        )
        self.assertNotEqual(statcast_history.max_pitch_row["game_pk"], GAME_PK)
        self.assertNotIn(GAME_PK, statcast_history.pitch_game_pks)

        schedule_history = _history()
        schedule_history.schedule_history.rows = (
            *schedule_history.schedule_history.rows,
            {
                "game_pk": GAME_PK,
                "official_date": TARGET,
                "team_codes": ("AWY", "HOM"),
                "home_team_code": "HOM",
                "venue_name": "Park",
                "innings": 9,
            },
        )
        self.assertIsNone(schedule_history.schedule_history.input_max_date)
        self.assertNotIn(GAME_PK, schedule_history.schedule_history.game_dates)

        result_history = _history()
        result_history.schedule_history.rows = (
            {
                "game_pk": 777,
                "official_date": CUTOFF,
                "team_codes": ("AWY", "HOM"),
                "home_team_code": "HOM",
                "venue_name": "Park",
                "innings": 9,
                "away_score": 3,
            },
        )

        for history in (statcast_history, schedule_history, result_history):
            with self.subTest(history=history), self.assertRaisesRegex(
                ValueError, "AS_OF_VIOLATION"
            ):
                build_live_raw_game_features(
                    _participants(),
                    _final(),
                    _bundle(feature_history=history),
                    {501: 1, 601: 2},
                )

    def test_index_only_poison_cannot_change_detached_feature_math(self):
        clean = build_live_raw_game_features(
            _participants(), _final(), _bundle(), {501: 1, 601: 2}
        )
        target_row = _pa(
            GAME_PK,
            TARGET,
            501,
            101,
            hand="R",
            home="HOM",
            away="AWY",
            top="Top",
            at_bat=9,
        )

        pa_history = _history()
        pa_history.pa_by_batter = {
            **pa_history.pa_by_batter,
            101: (target_row, *pa_history.pa_by_batter[101]),
        }

        pitch_history = _history()
        pitch_history.pitches_by_pitcher = {
            **pitch_history.pitches_by_pitcher,
            501: (target_row, *pitch_history.pitches_by_pitcher[501]),
        }

        schedule_history = _history()
        indexed_schedule_row = {
            "game_pk": GAME_PK,
            "official_date": CUTOFF,
            "team_codes": ("AWY", "HOM"),
            "home_team_code": "HOM",
            "venue_name": "Park",
            "innings": 12,
        }
        schedule_history.schedule_history.by_team = {
            **schedule_history.schedule_history.by_team,
            "HOM": (indexed_schedule_row,),
        }
        schedule_history.schedule_history.by_team_date = {
            **schedule_history.schedule_history.by_team_date,
            ("HOM", date.fromisoformat(CUTOFF)): (indexed_schedule_row,),
        }

        for history in (pa_history, pitch_history, schedule_history):
            with self.subTest(history=history):
                bundle = _bundle(feature_history=history)
                prepared = live_features.prepare_live_feature_history(
                    bundle, target_game_pks=(GAME_PK,)
                )
                poisoned = build_live_raw_game_features(
                    _participants(),
                    _final(),
                    bundle,
                    {501: 1, 601: 2},
                    prepared_history=prepared,
                )
                self.assertEqual(poisoned, clean)

    def test_row_kind_date_authority_rejects_conflicting_extraneous_dates(self):
        statcast_row = _pa(
            99,
            TARGET,
            501,
            101,
            hand="L",
            home="HOM",
            away="AWY",
            top="Top",
            at_bat=7,
        )
        statcast_row["official_date"] = CUTOFF
        statcast_history = _history(extra_rows=(statcast_row,))

        schedule_history = _history()
        schedule_history.schedule_history.rows = (
            {
                "game_pk": 777,
                "official_date": CUTOFF,
                "game_date": TARGET,
                "team_codes": ("AWY", "HOM"),
                "home_team_code": "HOM",
                "venue_name": "Park",
                "innings": 9,
            },
        )

        for history in (statcast_history, schedule_history):
            with self.subTest(history=history), self.assertRaisesRegex(
                ValueError, "AS_OF_VIOLATION"
            ):
                build_live_raw_game_features(
                    _participants(),
                    _final(),
                    _bundle(feature_history=history),
                    {501: 1, 601: 2},
                )

    def test_career_bf_is_exact_nonnegative_integer_and_replaces_loaded_count(self):
        raw = build_live_raw_game_features(
            _participants(), _final(), _bundle(), {501: 0, 601: 0}
        )
        self.assertEqual(raw["away"]["starter"]["career_bf"], 0)
        self.assertEqual(raw["home"]["starter"]["career_bf"], 0)
        for values in ({501: 1}, {501: 1, 601: -1}, {501: 1, 601: True}):
            with self.subTest(values=values), self.assertRaisesRegex(
                ValueError, "INVALID_CAREER_BF"
            ):
                build_live_raw_game_features(
                    _participants(), _final(), _bundle(), values
                )

    def test_final_binding_query_identity_fails_closed_when_not_a_mapping(self):
        final = _final()
        object.__setattr__(final.source_binding, "query_identity_json", "[]")

        with self.assertRaisesRegex(ValueError, "FINAL_BINDING_IDENTITY_MISMATCH"):
            build_live_raw_game_features(
                _participants(), final, _bundle(), {501: 1, 601: 2}
            )

    def test_live_feature_module_has_no_forbidden_static_or_dynamic_imports(self):
        source = Path(live_features.__file__).read_text(encoding="utf-8")
        self.assertEqual(_forbidden_live_imports(source), ())

    def test_forbidden_import_guard_rejects_synthetic_variants(self):
        forbidden_sources = (
            "from . import scoring",
            "from .. import outcome",
            "from .outcome import winner",
            "from .outcomes import winner",
            "import mlb_backtest.ranking",
            "import safe_module as outcome",
            "from feeds import market",
            "import odds",
            "__import__('mlb_backtest.scoring')",
            "import importlib\nimportlib.import_module('mlb_backtest.market')",
            "import importlib as loader\nloader.import_module('.odds', __package__)",
            "from importlib import import_module as load\nload(module_name)",
            "import builtins as runtime\nruntime.__import__('ranking')",
            "from builtins import __import__ as load\nload('market')",
            "import importlib\nimportlib.import_module('.outcome', __package__)",
        )
        for source in forbidden_sources:
            with self.subTest(source=source):
                self.assertTrue(_forbidden_live_imports(source))

    def test_forbidden_import_guard_allows_nonmatching_path_segments_and_symbols(self):
        allowed_sources = (
            "import statistics.odds_ratio",
            "from statistics import odds_ratio",
            "import marketplace_config",
            "import safe_module as marketplace_config",
            "from marketplace_config import market_value",
            "import importlib\nimportlib.import_module('statistics.odds_ratio')",
        )
        for source in allowed_sources:
            with self.subTest(source=source):
                self.assertEqual(_forbidden_live_imports(source), ())


if __name__ == "__main__":
    unittest.main()
