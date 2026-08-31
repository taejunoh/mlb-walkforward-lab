import ast
import hashlib
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest import mock

import mlb_backtest.live_top3_service as service
from mlb_backtest.feature_engine import FeatureHistory
from mlb_backtest.live_features import (
    build_live_raw_game_features as real_build_live_raw_game_features,
)
from mlb_backtest.live_model_card import load_frozen_live_card
from mlb_backtest.live_sources import (
    FinalGameScheduleDisposition,
    LiveSlateObservation,
    PregameParticipants,
)
from mlb_backtest.live_top3_contracts import (
    ExecutionTimes,
    FinalGameObservation,
    IntegrityError,
    SourceBinding,
    canonical_json_bytes,
    validate_run_payload,
)
from mlb_backtest.live_top3_service import build_live_top3_run
from mlb_backtest.statcast import StatcastData
from tests.test_live_features import _bundle as _feature_bundle


TARGET = "2025-05-01"
CUTOFF = "2025-04-30"
MODEL_CARD = Path(__file__).parents[1] / "audit/v5_1_bt/model_card.json"
FORBIDDEN_SERVICE_IMPORT_NAMES = frozenset(
    {"outcome", "outcomes", "market", "odds", "pipeline", "ranking"}
)


def _service_feature_history() -> FeatureHistory:
    source = _feature_bundle().feature_history

    def detached(rows):
        return tuple(
            {
                **row,
                "game_pk": 700,
                "event_id": f"700:{row['at_bat_number']}",
            }
            for row in rows
        )

    schedule = source.schedule_history
    return FeatureHistory(
        StatcastData(
            detached(source.pitches),
            detached(source.plate_appearances),
        ),
        slot_weights=dict(source.slot_weights),
        schedule_history=tuple(dict(row) for row in schedule.rows),
        schedule_coverage_start=schedule.coverage_start.isoformat(),
        schedule_coverage_end=schedule.coverage_end.isoformat(),
        schedule_coverage_complete=schedule.coverage_complete,
        venue_by_name=dict(source.venue_by_name),
        statcast_coverage_complete=source.statcast_coverage_complete,
    )


def _bundle(**overrides):
    overrides.setdefault("feature_history", _service_feature_history())
    return _feature_bundle(**overrides)


def _forbidden_service_imports(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    candidates: list[str] = []
    importlib_aliases = {"importlib"}
    builtins_aliases = {"builtins"}
    dynamic_aliases = {"__import__"}
    getattr_aliases = {"getattr"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                candidates.append(alias.name)
                bound = alias.asname or alias.name.split(".", 1)[0]
                if alias.name == "importlib" or alias.name.startswith("importlib."):
                    importlib_aliases.add(bound)
                if alias.name == "builtins" or alias.name.startswith("builtins."):
                    builtins_aliases.add(bound)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            candidates.append(module)
            for alias in node.names:
                candidates.extend((alias.name, f"{module}.{alias.name}"))
                if node.module == "importlib" and alias.name == "import_module":
                    dynamic_aliases.add(alias.asname or alias.name)
                if node.module == "builtins" and alias.name == "__import__":
                    dynamic_aliases.add(alias.asname or alias.name)
                if node.module == "builtins" and alias.name == "getattr":
                    getattr_aliases.add(alias.asname or alias.name)

    def is_getattr_callable(expression):
        return (
            isinstance(expression, ast.Name)
            and expression.id in getattr_aliases
        ) or (
            isinstance(expression, ast.Attribute)
            and expression.attr == "getattr"
            and isinstance(expression.value, ast.Name)
            and expression.value.id in builtins_aliases
        )

    def getattr_arguments(expression):
        if (
            not isinstance(expression, ast.Call)
            or len(expression.args) < 2
            or not is_getattr_callable(expression.func)
        ):
            return None
        return expression.args[0], expression.args[1]

    def dynamic_loader(expression):
        if isinstance(expression, ast.Name):
            return expression.id in dynamic_aliases, False
        if isinstance(expression, ast.Attribute) and isinstance(
            expression.value, ast.Name
        ):
            if (
                expression.value.id in importlib_aliases
                and expression.attr == "import_module"
            ) or (
                expression.value.id in builtins_aliases
                and expression.attr == "__import__"
            ):
                return True, False
        arguments = getattr_arguments(expression)
        if arguments is None:
            return False, False
        owner, attribute = arguments
        if not isinstance(owner, ast.Name):
            return False, False
        expected_attribute = None
        if owner.id in importlib_aliases:
            expected_attribute = "import_module"
        elif owner.id in builtins_aliases:
            expected_attribute = "__import__"
        if expected_attribute is None:
            return False, False
        if not isinstance(attribute, ast.Constant) or not isinstance(
            attribute.value, str
        ):
            return True, True
        return attribute.value == expected_attribute, False

    assignments = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assignments.extend(
                (target, node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assignments.append((node.target, node.value))

    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            if isinstance(value, ast.Name) and value.id in importlib_aliases:
                if target.id not in importlib_aliases:
                    importlib_aliases.add(target.id)
                    changed = True
                continue
            if isinstance(value, ast.Name) and value.id in builtins_aliases:
                if target.id not in builtins_aliases:
                    builtins_aliases.add(target.id)
                    changed = True
                continue
            if is_getattr_callable(value):
                if target.id not in getattr_aliases:
                    getattr_aliases.add(target.id)
                    changed = True
                continue
            dynamic, uncertain = dynamic_loader(value)
            if dynamic and target.id not in dynamic_aliases:
                dynamic_aliases.add(target.id)
                changed = True
            if uncertain:
                candidates.append("<dynamic-import>")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dynamic, uncertain = dynamic_loader(node.func)
        if not dynamic:
            continue
        if uncertain or (
            not node.args
            or not isinstance(node.args[0], ast.Constant)
            or not isinstance(node.args[0].value, str)
        ):
            candidates.append("<dynamic-import>")
        else:
            candidates.append(node.args[0].value)
    violations = {
        candidate
        for candidate in candidates
        if candidate == "<dynamic-import>"
        or set(candidate.lstrip(".").lower().split("."))
        & FORBIDDEN_SERVICE_IMPORT_NAMES
    }
    return tuple(sorted(violations))


def _binding(
    role: str,
    index: int,
    *,
    game_pks: tuple[int, ...] = (),
    durable: str = "2025-05-01T12:00:01Z",
    pitcher_id: int | None = None,
    identity_target_date: str = TARGET,
    identity_game_pk: int | None = None,
) -> SourceBinding:
    observation_id = f"service-obs-{index}"
    url = (
        f"https://statsapi.mlb.com/api/v1/game/{game_pks[0]}/boxscore"
        if role == "LIVE_PARTICIPANTS" and game_pks
        else "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
    )
    identity: dict[str, object] = {"url": url}
    if role in {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS", "FINAL_SCHEDULE"}:
        identity.update(
            {
                "target_date": identity_target_date,
                "observation_id": observation_id,
            }
        )
        if role != "LIVE_SCHEDULE":
            identity["game_pk"] = (
                identity_game_pk
                if identity_game_pk is not None
                else game_pks[0]
            )
    else:
        identity["as_of_date"] = CUTOFF
    if pitcher_id is not None:
        identity["pitcher_id"] = pitcher_id
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
        "durable_at_utc": durable,
        "http_status": 200,
        "content_type": "application/json",
        "etag": '"service"',
        "last_modified": None,
        "payload_sha256": f"{index % 16:x}" * 64,
        "byte_length": 12,
        "payload_relative": f"data/experimental_daily/service-{index}.json",
        "manifest_sha256": f"{(index + 1) % 16:x}" * 64,
        "receipt_relative": f"audit/experimental_daily/service-{index}.json",
        "receipt_sha256": f"{(index + 2) % 16:x}" * 64,
    }
    value["binding_sha256"] = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return SourceBinding(**value)


def _game(game_pk: int, *, start: str | None = None, status: str = "Pre-Game"):
    return {
        "game_pk": game_pk,
        "official_date": TARGET,
        "game_type": "R",
        "scheduled_start_utc": start or f"2025-05-01T{18 + game_pk:02d}:00:00Z",
        "status": status,
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


def _participants(
    game_pk: int,
    *,
    complete: bool = True,
    reasons: tuple[str, ...] = ("PROBABLE_CONSISTENT",),
    home_lineup: tuple[int, ...] = tuple(range(101, 110)),
) -> PregameParticipants:
    return PregameParticipants(
        game_pk=game_pk,
        away_team_id=110,
        home_team_id=120,
        away_pitcher_list_id=501,
        home_pitcher_list_id=601,
        away_lineup=tuple(range(201, 210)),
        home_lineup=home_lineup,
        complete=complete,
        reason_codes=reasons,
    )


def _slate(
    games: tuple[dict[str, object], ...],
    *,
    participants: dict[int, PregameParticipants] | None = None,
    participant_durable: str = "2025-05-01T12:00:01Z",
) -> LiveSlateObservation:
    pks = tuple(sorted(int(game["game_pk"]) for game in games))
    observed = participants or {game_pk: _participants(game_pk) for game_pk in pks}
    bindings = [_binding("LIVE_SCHEDULE", 100, game_pks=pks)]
    bindings.extend(
        _binding(
            "LIVE_PARTICIPANTS",
            110 + game_pk,
            game_pks=(game_pk,),
            durable=participant_durable,
        )
        for game_pk in sorted(observed)
    )
    return LiveSlateObservation(TARGET, games, observed, tuple(bindings))


def _final(game: dict[str, object], index: int = 0, **changes: object):
    projected = {**game, **changes}
    game_pk = int(projected["game_pk"])
    return FinalGameObservation(
        game_pk,
        str(projected["official_date"]),
        projected,
        _binding("FINAL_SCHEDULE", 200 + index + game_pk, game_pks=(game_pk,)),
    )


def _final_observer(games: tuple[dict[str, object], ...]):
    by_pk = {int(game["game_pk"]): game for game in games}
    calls: list[tuple[int, str]] = []

    def observe(game_pk: int, target_date: str):
        calls.append((game_pk, target_date))
        return _final(by_pk[game_pk], game_pk)

    observe.calls = calls
    return observe


def _career_inputs():
    values = {501: 500, 601: 600, 999: 700, 1000: 800}
    bindings = tuple(
        _binding("CAREER_BF", 300 + index, pitcher_id=pitcher_id)
        for index, pitcher_id in enumerate(values)
    )
    return values, bindings


def _card():
    return load_frozen_live_card(MODEL_CARD)


def _times():
    return ExecutionTimes(
        executed_at_eastern="2025-05-01T08:00:00-04:00",
        executed_at_utc="2025-05-01T12:00:00Z",
    )


def _command_inputs():
    return {"date": TARGET, "model_card": "audit/v5_1_bt/model_card.json"}


def _mutable(value):
    if isinstance(value, Mapping):
        return {key: _mutable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable(item) for item in value]
    return value


def _raw(participants, final, _history, career_bf, *, prepared_history=None):
    return {
        "game_pk": final.game_pk,
        "official_date": TARGET,
        "eligible": True,
        "reasons": [],
        "starter_provenance": "PROBABLE_CONSISTENT",
        "lineup_provenance": "OBSERVED_PREGAME_LINEUP",
        "away": {"starter": {"career_bf": career_bf[participants.away_starter_id]}},
        "home": {"starter": {"career_bf": career_bf[participants.home_starter_id]}},
    }


def _scorer(edges, completeness=None, starter_edges=None):
    completeness = completeness or {}
    starter_edges = starter_edges or {}

    def score(raw, _normalization):
        game_pk = raw["game_pk"]
        quality = completeness.get(game_pk, 0.9)
        starter_edge = starter_edges.get(game_pk, float(game_pk) / 10)
        names = (
            "starter",
            "offense",
            "bullpen",
            "environment",
            "defense",
            "schedule",
        )
        away_scores = {name: 50.0 for name in names}
        home_scores = dict(away_scores)
        home_scores["starter"] += starter_edge
        return {
            "edge": edges[game_pk],
            "qualities": {name: quality for name in names},
            "away_scores": away_scores,
            "home_scores": home_scores,
        }

    return score


def _build(
    slate,
    observer,
    *,
    feature_builder=_raw,
    scorer=None,
    card=None,
    history=None,
):
    career, career_bindings = _career_inputs()
    with mock.patch.object(
        service, "build_live_raw_game_features", side_effect=feature_builder
    ), mock.patch.object(
        service,
        "score_raw_game",
        side_effect=scorer or _scorer({int(game["game_pk"]): game["game_pk"] for game in slate.games}),
    ):
        return build_live_top3_run(
            slate=slate,
            final_schedule_observer=observer,
            history=history or _bundle(),
            card=card or _card(),
            career_bf_by_pitcher=career,
            career_bf_bindings=career_bindings,
            command_inputs=_command_inputs(),
            execution_times=_times(),
        )


class LiveTop3ServiceTests(unittest.TestCase):
    def test_missing_participant_pitchers_are_unscored_with_missing_evidence(self):
        game = _game(1)
        participant = _participants(
            1,
            complete=False,
            reasons=("MISSING_AWAY_PITCHER_LIST", "MISSING_HOME_PITCHER_LIST"),
        )
        participant = replace(
            participant,
            away_pitcher_list_id=None,
            home_pitcher_list_id=None,
        )
        scorer = mock.Mock(side_effect=_scorer({1: 1.0}))

        result = _build(
            _slate((game,), participants={1: participant}),
            _final_observer((game,)),
            scorer=scorer,
        )

        self.assertEqual(result["preliminary_top3"], [])
        self.assertEqual(
            result["unscored_games"][0]["reason_codes"],
            ["MISSING_AWAY_PITCHER_LIST", "MISSING_HOME_PITCHER_LIST"],
        )
        self.assertEqual(scorer.call_count, 0)

    def test_missing_lineup_evidence_is_unscored_without_invalid_fallback(self):
        game = _game(1)
        participant = _participants(
            1, complete=False, reasons=("MISSING_HOME_LINEUP",), home_lineup=()
        )
        scorer = mock.Mock(side_effect=_scorer({1: 1.0}))

        result = _build(
            _slate((game,), participants={1: participant}),
            _final_observer((game,)),
            scorer=scorer,
        )

        self.assertEqual(result["preliminary_top3"], [])
        self.assertEqual(
            result["unscored_games"][0]["reason_codes"], ["MISSING_HOME_LINEUP"]
        )
        self.assertEqual(scorer.call_count, 0)

    def test_ranked_output_preserves_side_specific_raw_schedule_quality_flags(self):
        game = _game(1)

        def feature(*args, **kwargs):
            raw = _raw(*args, **kwargs)
            raw["away"]["schedule"] = {"flags": ["away_flag", "away_flag"]}
            raw["home"]["schedule"] = {"flags": ["home_flag"]}
            return raw

        result = _build(
            _slate((game,)), _final_observer((game,)), feature_builder=feature
        )

        self.assertEqual(
            result["preliminary_top3"][0]["schedule_quality_flags"],
            {"away": ["away_flag"], "home": ["home_flag"]},
        )
        self.assertEqual(validate_run_payload(result), result)

    def test_prepares_history_once_and_reuses_authority_for_fifteen_games(self):
        game_pks = tuple(range(901, 916))
        games = tuple(
            _game(game_pk, start="2025-05-01T23:00:00Z")
            for game_pk in game_pks
        )
        seen_prepared = []

        def feature(
            participants, final, history, career_bf, *, prepared_history
        ):
            seen_prepared.append(prepared_history)
            return _raw(
                participants,
                final,
                history,
                career_bf,
                prepared_history=prepared_history,
            )

        with mock.patch.object(
            service,
            "prepare_live_feature_history",
            wraps=service.prepare_live_feature_history,
        ) as prepare:
            _build(
                _slate(games),
                _final_observer(games),
                feature_builder=feature,
            )

        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(
            prepare.call_args.kwargs["target_game_pks"], game_pks
        )
        self.assertEqual(len(seen_prepared), 15)
        self.assertEqual(len({id(value) for value in seen_prepared}), 1)

    def test_target_game_pk_in_history_aborts_before_feature_or_scoring(self):
        game = _game(901, start="2025-05-01T23:00:00Z")
        history = _bundle()
        target_row = {
            **history.feature_history.pitches[0],
            "game_pk": 901,
            "event_id": "901:1",
        }
        history.feature_history.pitches = (
            *history.feature_history.pitches,
            target_row,
        )
        feature = mock.Mock(side_effect=_raw)
        scorer = mock.Mock(side_effect=_scorer({901: 1.0}))

        with self.assertRaisesRegex(ValueError, "AS_OF_VIOLATION"):
            _build(
                _slate((game,)),
                _final_observer((game,)),
                feature_builder=feature,
                scorer=scorer,
                history=history,
            )

        self.assertEqual(feature.call_count, 0)
        self.assertEqual(scorer.call_count, 0)

    def test_four_preliminary_candidates_select_exact_top_three(self):
        games = tuple(_game(game_pk) for game_pk in (1, 2, 3, 4))
        slate = _slate(games)
        observer = _final_observer(games)

        result = _build(
            slate,
            observer,
            scorer=_scorer({1: 0.5, 2: 2.0, 3: 1.0, 4: 3.0}),
        )

        self.assertEqual(result["verified_top3"], [])
        self.assertEqual(
            [row["game_pk"] for row in result["preliminary_top3"]], [4, 2, 3]
        )
        self.assertEqual(
            [row["rank"] for row in result["preliminary_top3"]], [1, 2, 3]
        )
        self.assertEqual(observer.calls, [(1, TARGET), (2, TARGET), (3, TARGET), (4, TARGET)])

    def test_unrounded_tie_breaks_use_completeness_starter_start_and_game_pk(self):
        games = (
            _game(1, start="2025-05-01T21:00:00Z"),
            _game(2, start="2025-05-01T20:00:00Z"),
            _game(3, start="2025-05-01T20:00:00Z"),
            _game(4, start="2025-05-01T20:00:00Z"),
        )
        slate = _slate(games)
        result = _build(
            slate,
            _final_observer(games),
            scorer=_scorer(
                {game_pk: 1.0 for game_pk in range(1, 5)},
                completeness={1: 0.95, 2: 0.95, 3: 0.95, 4: 0.9},
                starter_edges={1: 0.5, 2: 0.6, 3: 0.6, 4: 9.0},
            ),
        )
        self.assertEqual(
            [row["game_pk"] for row in result["preliminary_top3"]], [2, 3, 1]
        )

    def test_starter_conflict_is_unscored_and_verified_is_reserved(self):
        games = (_game(1),)
        participant = _participants(
            1,
            complete=False,
            reasons=("STARTER_CONFLICT",),
        )
        feature = mock.Mock(side_effect=_raw)
        result = _build(
            _slate(games, participants={1: participant}),
            _final_observer(games),
            feature_builder=feature,
        )
        row = result["unscored_games"][0]
        self.assertEqual(feature.call_count, 0)
        self.assertEqual(result["verified_top3"], [])
        self.assertEqual(row["reason_codes"], ["STARTER_CONFLICT"])
        self.assertNotIn("pick_probability", row)
        self.assertNotIn("rank", row)

    def test_missing_lineup_is_unscored_without_probability(self):
        games = (_game(1),)
        slate = _slate(
            games,
            participants={
                1: _participants(
                    1,
                    complete=False,
                    reasons=("MISSING_HOME_LINEUP",),
                    home_lineup=(),
                )
            },
        )

        feature = mock.Mock(side_effect=_raw)
        row = _build(
            slate,
            _final_observer(games),
            feature_builder=feature,
        )["unscored_games"][0]
        self.assertEqual(feature.call_count, 0)
        self.assertEqual(row["reason_codes"], ["MISSING_HOME_LINEUP"])
        self.assertNotIn("pick_probability", row)
        self.assertNotIn("rank", row)

    def test_final_disposition_and_poststart_boundary_are_excluded_before_features(self):
        games = (_game(1), _game(2, start="2025-05-01T12:00:01Z"))
        slate = _slate(games)
        calls = []

        def observe(game_pk, target_date):
            calls.append((game_pk, target_date))
            if game_pk == 1:
                return FinalGameScheduleDisposition(
                    1,
                    TARGET,
                    "EXCLUDED",
                    ("SCHEDULED_START_CHANGED",),
                    _binding("FINAL_SCHEDULE", 211, game_pks=(1,)),
                )
            return _final(games[1], 2)

        feature = mock.Mock(side_effect=_raw)
        result = _build(slate, observe, feature_builder=feature)
        self.assertEqual(feature.call_count, 0)
        self.assertEqual(calls, [(1, TARGET), (2, TARGET)])
        reasons_by_game = {
            row["game_pk"]: row["reason_codes"] for row in result["excluded_games"]
        }
        self.assertEqual(reasons_by_game[1], ["SCHEDULED_START_CHANGED"])
        self.assertEqual(reasons_by_game[2], ["OBSERVATION_AT_OR_AFTER_START"])

    def test_final_schedule_revision_is_excluded_before_features(self):
        games = (_game(1),)
        feature = mock.Mock(side_effect=_raw)

        def revised(_game_pk, _target_date):
            return _final(games[0], venue_name="Different Park")

        result = _build(_slate(games), revised, feature_builder=feature)

        self.assertEqual(feature.call_count, 0)
        self.assertEqual(result["preliminary_top3"], [])
        self.assertEqual(
            result["excluded_games"][0]["reason_codes"], ["VENUE_CHANGED"]
        )

    def test_exact_neutral_score_is_unscored(self):
        games = (_game(1),)
        slate = _slate(games)
        with mock.patch.object(service, "home_probability", return_value=0.5):
            result = _build(slate, _final_observer(games))
        self.assertEqual(result["preliminary_top3"], [])
        self.assertEqual(
            result["unscored_games"][0]["reason_codes"], ["EXACT_NEUTRAL_SCORE"]
        )

    def test_payload_binds_exact_digests_roles_two_career_refs_and_boundary(self):
        games = (_game(1),)
        result = _build(_slate(games), _final_observer(games))
        self.assertEqual(len(result), 16)
        self.assertEqual(validate_run_payload(result), result)
        row = result["preliminary_top3"][0]
        bindings = {
            item["binding_sha256"]: item for item in result["source_bindings"]
        }
        selected = [bindings[digest] for digest in row["source_binding_sha256s"]]
        self.assertEqual(
            sum(item["binding_role"] == "CAREER_BF" for item in selected), 2
        )
        self.assertEqual(
            row["observation_boundary_utc"],
            max(item["durable_at_utc"] for item in selected),
        )
        expected_raw = _raw(
            _participants(1), _final(games[0], 1), _bundle(), _career_inputs()[0]
        )
        self.assertEqual(
            row["feature_sha256"],
            hashlib.sha256(canonical_json_bytes(expected_raw)).hexdigest(),
        )
        without = dict(result)
        without.pop("content_sha256")
        self.assertEqual(
            result["content_sha256"],
            hashlib.sha256(canonical_json_bytes(without)).hexdigest(),
        )

    def test_global_asof_and_card_failures_propagate(self):
        games = (_game(1),)

        def asof(*_args, **_kwargs):
            raise ValueError("AS_OF_VIOLATION")

        with self.assertRaisesRegex(ValueError, "AS_OF_VIOLATION"):
            _build(_slate(games), _final_observer(games), feature_builder=asof)
        with self.assertRaises(ValueError):
            _build(
                _slate(games),
                _final_observer(games),
                card=replace(_card(), component_weights={"starter": 1.0}),
            )

    def test_model_card_semantic_tampering_is_rejected_before_observation(self):
        games = (_game(1),)
        slate = _slate(games)
        card = _card()
        normalization = _mutable(card.normalization)
        normalization["starter"]["fip"]["mean"] += 0.01
        slot_weights = dict(card.slot_weights)
        slot_weights[1] += 0.01
        slot_weights[2] -= 0.01
        cases = (
            replace(card, alpha=card.alpha + 0.01),
            replace(card, beta=card.beta + 0.01),
            replace(card, normalization=normalization),
            replace(card, slot_weights=slot_weights),
        )
        for tampered in cases:
            with self.subTest(card=tampered):
                observer = mock.Mock(return_value=_final(games[0]))

                with self.assertRaisesRegex(ValueError, "frozen live card differs"):
                    _build(slate, observer, card=tampered)

                self.assertEqual(observer.call_count, 0)

    def test_global_feature_integrity_errors_abort_two_game_slate(self):
        games = (_game(1), _game(2))
        slate = _slate(games)
        observer = _final_observer(games)

        for reason in (
            "TARGET_PARTICIPANT_LEAKAGE",
            "INVALID_FINAL_GAME_PROJECTION",
            "AS_OF_VIOLATION",
        ):
            with self.subTest(reason=reason):
                def poisoned_feature(
                    participant,
                    final,
                    history,
                    career_bf,
                    *,
                    prepared_history,
                ):
                    if final.game_pk == 2:
                        raise IntegrityError(reason)
                    return _raw(
                        participant,
                        final,
                        history,
                        career_bf,
                        prepared_history=prepared_history,
                    )

                feature = mock.Mock(side_effect=poisoned_feature)
                scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))

                with self.assertRaisesRegex(IntegrityError, reason):
                    _build(
                        slate,
                        observer,
                        feature_builder=feature,
                        scorer=scorer,
                    )

                self.assertEqual(scorer.call_count, 0)

    def test_poisoned_second_final_projection_aborts_before_feature_or_scoring(self):
        games = (_game(1), _game(2))
        slate = _slate(games)
        poisons = (
            ({"home_lineup": [101]}, "TARGET_PARTICIPANT_LEAKAGE"),
            ({"away_score": 9}, "INVALID_FINAL_GAME_PROJECTION"),
        )
        for changes, reason in poisons:
            with self.subTest(reason=reason):
                def observe(game_pk, _target_date):
                    game = games[game_pk - 1]
                    return _final(
                        game,
                        game_pk,
                        **(changes if game_pk == 2 else {}),
                    )

                feature = mock.Mock(side_effect=_raw)
                scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))

                with self.assertRaisesRegex(IntegrityError, reason):
                    _build(
                        slate,
                        observe,
                        feature_builder=feature,
                        scorer=scorer,
                    )

                self.assertEqual(feature.call_count, 0)
                self.assertEqual(scorer.call_count, 0)

    def test_stale_schedule_binding_is_global_before_scoring(self):
        games = (_game(1),)
        valid = _slate(games)
        stale = _binding(
            "LIVE_SCHEDULE",
            100,
            game_pks=(1,),
            identity_target_date=CUTOFF,
        )
        slate = LiveSlateObservation(
            TARGET,
            valid.games,
            valid.participants,
            (stale,) + valid.source_bindings[1:],
        )
        scorer = mock.Mock(side_effect=_scorer({1: 1.0}))

        with self.assertRaises(IntegrityError):
            _build(slate, _final_observer(games), scorer=scorer)

        self.assertEqual(scorer.call_count, 0)

    def test_poisoned_initial_schedule_aborts_before_observer_or_scoring(self):
        clean_game = _game(1)
        poisons = (
            ({"away_score": 99}, "INVALID_INITIAL_GAME_PROJECTION"),
            ({"away_actual_starter_id": 501}, "INVALID_INITIAL_GAME_PROJECTION"),
            ({"home_lineup": [101]}, "TARGET_PARTICIPANT_LEAKAGE"),
        )
        for contamination, reason in poisons:
            with self.subTest(reason=reason, contamination=contamination):
                poisoned_game = {**clean_game, **contamination}
                slate = _slate((poisoned_game,))
                observer = mock.Mock(return_value=_final(clean_game))
                feature = mock.Mock(side_effect=_raw)
                scorer = mock.Mock(side_effect=_scorer({1: 1.0}))

                with self.assertRaisesRegex(IntegrityError, reason):
                    _build(
                        slate,
                        observer,
                        feature_builder=feature,
                        scorer=scorer,
                    )

                self.assertEqual(observer.call_count, 0)
                self.assertEqual(feature.call_count, 0)
                self.assertEqual(scorer.call_count, 0)

    def test_stale_and_cross_game_participant_bindings_are_global(self):
        games = (_game(1), _game(2))
        valid = _slate(games)
        cases = (
            _binding(
                "LIVE_PARTICIPANTS",
                111,
                game_pks=(1,),
                identity_target_date=CUTOFF,
            ),
            _binding(
                "LIVE_PARTICIPANTS",
                111,
                game_pks=(1,),
                identity_game_pk=2,
            ),
        )
        for invalid in cases:
            with self.subTest(identity=invalid.query_identity_json):
                slate = LiveSlateObservation(
                    TARGET,
                    valid.games,
                    valid.participants,
                    (valid.source_bindings[0], invalid)
                    + valid.source_bindings[2:],
                )
                scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))

                with self.assertRaises(IntegrityError):
                    _build(slate, _final_observer(games), scorer=scorer)

                self.assertEqual(scorer.call_count, 0)

    def test_multiple_participant_bindings_for_one_game_are_global(self):
        games = (_game(1),)
        valid = _slate(games)
        duplicate_context = _binding(
            "LIVE_PARTICIPANTS",
            999,
            game_pks=(1,),
        )
        slate = LiveSlateObservation(
            TARGET,
            valid.games,
            valid.participants,
            valid.source_bindings + (duplicate_context,),
        )
        observer = mock.Mock(return_value=_final(games[0]))
        scorer = mock.Mock(side_effect=_scorer({1: 1.0}))

        with self.assertRaisesRegex(IntegrityError, "participant binding"):
            _build(slate, observer, scorer=scorer)

        self.assertEqual(observer.call_count, 0)
        self.assertEqual(scorer.call_count, 0)

    def test_local_invalid_target_venue_unscored_without_aborting_clean_game(self):
        first = _game(1)
        second = _game(2)
        second["venue_id"] = 44
        second["venue_name"] = "Broken Park"
        games = (first, second)
        bundle = _bundle()
        history = replace(
            bundle,
            venue_references={
                **dict(bundle.venue_references),
                44: {"venue_name": "Broken Park"},
            },
        )
        feature = mock.Mock(side_effect=_raw)
        scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))

        result = _build(
            _slate(games),
            _final_observer(games),
            feature_builder=feature,
            scorer=scorer,
            history=history,
        )

        self.assertEqual(
            [row["game_pk"] for row in result["preliminary_top3"]], [1]
        )
        self.assertEqual(
            result["unscored_games"][0]["reason_codes"],
            ["INVALID_VENUE_REFERENCE"],
        )
        self.assertEqual(feature.call_count, 1)
        self.assertEqual(scorer.call_count, 1)

    def test_unexpected_venue_validator_value_errors_abort_whole_slate(self):
        games = (_game(1), _game(2))
        failures = (
            ValueError("AS_OF_VIOLATION"),
            ValueError("UNKNOWN_VENUE_FAILURE"),
            ValueError("INVALID_VENUE_REFERENCE", "extra"),
        )
        for failure in failures:
            with self.subTest(arguments=failure.args):
                feature = mock.Mock(side_effect=_raw)
                scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))

                with mock.patch.object(
                    service, "_validate_venue_references", side_effect=failure
                ), self.assertRaises(ValueError) as raised:
                    _build(
                        _slate(games),
                        _final_observer(games),
                        feature_builder=feature,
                        scorer=scorer,
                    )

                self.assertEqual(raised.exception.args, failure.args)
                self.assertEqual(feature.call_count, 0)
                self.assertEqual(scorer.call_count, 0)

    def test_typed_candidate_feature_failure_only_unscored_failed_game(self):
        games = (_game(1), _game(2))

        def feature(
            participant, final, history, career_bf, *, prepared_history
        ):
            if final.game_pk == 2:
                raise service.CandidateFeatureFailure("STARTER_CONFLICT")
            return _raw(
                participant,
                final,
                history,
                career_bf,
                prepared_history=prepared_history,
            )

        scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))
        result = _build(
            _slate(games),
            _final_observer(games),
            feature_builder=feature,
            scorer=scorer,
        )

        self.assertEqual(
            [row["game_pk"] for row in result["preliminary_top3"]], [1]
        )
        self.assertEqual(
            result["unscored_games"][0]["reason_codes"],
            ["STARTER_CONFLICT"],
        )
        self.assertEqual(scorer.call_count, 1)

    def test_real_builder_starter_conflict_only_unscored_conflicted_game(self):
        games = (
            _game(901, start="2025-05-01T19:00:00Z"),
            _game(902, start="2025-05-01T20:00:00Z"),
        )
        by_pk = {int(game["game_pk"]): game for game in games}

        def observe(game_pk, _target_date):
            game = by_pk[game_pk]
            changes = {"away_probable_pitcher_id": 999} if game_pk == 902 else {}
            return _final(game, game_pk, **changes)

        scorer = mock.Mock(side_effect=_scorer({901: 1.0, 902: 2.0}))
        result = _build(
            _slate(games),
            observe,
            feature_builder=real_build_live_raw_game_features,
            scorer=scorer,
        )

        self.assertEqual(
            [row["game_pk"] for row in result["preliminary_top3"]], [901]
        )
        self.assertEqual(
            result["unscored_games"][0]["reason_codes"],
            ["PROBABLE_PITCHER_CHANGED"],
        )
        self.assertEqual(scorer.call_count, 1)

    def test_real_builder_unscored_on_probable_pitcher_name_change_before_scoring(self):
        games = (
            _game(901, start="2025-05-01T19:00:00Z"),
            _game(902, start="2025-05-01T20:00:00Z"),
        )
        by_pk = {int(game["game_pk"]): game for game in games}

        def observe(game_pk, _target_date):
            changes = (
                {"away_probable_pitcher_name": "Replacement Pitcher"}
                if game_pk == 902
                else {}
            )
            return _final(by_pk[game_pk], game_pk, **changes)

        scorer = mock.Mock(side_effect=_scorer({901: 1.0, 902: 2.0}))
        result = _build(
            _slate(games),
            observe,
            feature_builder=real_build_live_raw_game_features,
            scorer=scorer,
        )

        self.assertEqual(
            [row["game_pk"] for row in result["preliminary_top3"]], [901]
        )
        self.assertEqual(
            result["unscored_games"][0]["reason_codes"],
            ["PROBABLE_PITCHER_CHANGED"],
        )
        self.assertEqual(scorer.call_count, 1)

    def test_untrusted_plain_feature_value_errors_abort_whole_slate(self):
        games = (_game(1), _game(2))
        failures = (
            ValueError("AS_OF_VIOLATION"),
            ValueError("UNKNOWN_FEATURE_FAILURE"),
            ValueError("STARTER_CONFLICT", "extra"),
        )
        for failure in failures:
            with self.subTest(arguments=failure.args):
                def poisoned(
                    participant,
                    final,
                    history,
                    career_bf,
                    *,
                    prepared_history,
                ):
                    if final.game_pk == 2:
                        raise failure
                    return _raw(
                        participant,
                        final,
                        history,
                        career_bf,
                        prepared_history=prepared_history,
                    )

                scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))

                with self.assertRaises(ValueError) as raised:
                    _build(
                        _slate(games),
                        _final_observer(games),
                        feature_builder=poisoned,
                        scorer=scorer,
                    )

                self.assertEqual(raised.exception.args, failure.args)
                self.assertEqual(scorer.call_count, 0)

    def test_global_codes_cannot_be_downgraded_by_candidate_failure_type(self):
        games = (_game(1), _game(2))
        for reason in ("AS_OF_VIOLATION", "TARGET_PARTICIPANT_LEAKAGE"):
            with self.subTest(reason=reason):
                def feature(
                    participant,
                    final,
                    history,
                    career_bf,
                    *,
                    prepared_history,
                ):
                    if final.game_pk == 2:
                        raise service.CandidateFeatureFailure(reason)
                    return _raw(
                        participant,
                        final,
                        history,
                        career_bf,
                        prepared_history=prepared_history,
                    )

                scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))

                with self.assertRaisesRegex(IntegrityError, reason):
                    _build(
                        _slate(games),
                        _final_observer(games),
                        feature_builder=feature,
                        scorer=scorer,
                    )

                self.assertEqual(scorer.call_count, 0)

    def test_global_participant_reason_cannot_be_downgraded(self):
        games = (_game(1), _game(2))
        participants = {
            1: _participants(1),
            2: _participants(
                2,
                complete=False,
                reasons=("AS_OF_VIOLATION",),
            ),
        }
        scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))

        with self.assertRaisesRegex(IntegrityError, "AS_OF_VIOLATION"):
            _build(
                _slate(games, participants=participants),
                _final_observer(games),
                scorer=scorer,
            )

        self.assertEqual(scorer.call_count, 0)

    def test_stale_and_cross_game_final_bindings_are_global_before_any_scoring(self):
        games = (_game(1), _game(2))
        slate = _slate(games)
        invalid_bindings = (
            _binding(
                "FINAL_SCHEDULE",
                250,
                game_pks=(2,),
                identity_target_date=CUTOFF,
            ),
            _binding(
                "FINAL_SCHEDULE",
                250,
                game_pks=(2,),
                identity_game_pk=1,
            ),
        )
        for invalid in invalid_bindings:
            with self.subTest(identity=invalid.query_identity_json):
                def observe(game_pk, _target_date):
                    if game_pk == 1:
                        return _final(games[0], 1)
                    return FinalGameObservation(2, TARGET, games[1], invalid)

                scorer = mock.Mock(side_effect=_scorer({1: 1.0, 2: 2.0}))

                with self.assertRaises(IntegrityError):
                    _build(slate, observe, scorer=scorer)

                self.assertEqual(scorer.call_count, 0)

    def test_confidence_uncertainty_and_tier_are_internal_parity_calls(self):
        games = (_game(1),)
        slate = _slate(games)
        expected = _build(slate, _final_observer(games))

        with mock.patch.object(
            service,
            "confidence_score",
            return_value=0.0,
        ) as confidence, mock.patch.object(
            service,
            "uncertainty",
            return_value="High",
        ) as uncertainty, mock.patch.object(
            service,
            "tier",
            return_value="Toss-up",
        ) as tier:
            observed = _build(slate, _final_observer(games))

        self.assertEqual(observed, expected)
        confidence.assert_called_once_with(0.9, 0.9, 0.9, 0.9, 1 / 3, 0.9, 0.9)
        uncertainty.assert_called_once_with(0.9, False, 500, True)
        tier.assert_called_once_with(
            observed["preliminary_top3"][0]["pick_probability"],
            0.0,
            "High",
        )
        row = observed["preliminary_top3"][0]
        self.assertNotIn("confidence", row)
        self.assertNotIn("uncertainty", row)
        self.assertNotIn("tier", row)

    def test_final_observer_integrity_exception_propagates(self):
        games = (_game(1),)

        def invalid_receipt(_game_pk, _target_date):
            raise ValueError("final schedule receipt mismatch")

        with self.assertRaisesRegex(ValueError, "receipt mismatch"):
            _build(_slate(games), invalid_receipt)

    def test_duplicate_source_binding_digest_is_global_failure(self):
        games = (_game(1),)
        slate = _slate(games)
        duplicate = LiveSlateObservation(
            slate.target_date,
            slate.games,
            slate.participants,
            slate.source_bindings + (slate.source_bindings[0],),
        )

        with self.assertRaisesRegex(ValueError, "duplicate source binding"):
            _build(duplicate, _final_observer(games))

    def test_exactly_two_relevant_career_bindings_are_required(self):
        game = _game(1)
        game["home_probable_pitcher_id"] = 501
        game["home_probable_pitcher_name"] = "Away Pitcher"
        games = (game,)
        same_pitcher = replace(_participants(1), home_pitcher_list_id=501)
        slate = _slate(games, participants={1: same_pitcher})

        result = _build(slate, _final_observer(games))

        self.assertEqual(result["preliminary_top3"], [])
        self.assertEqual(
            result["unscored_games"][0]["reason_codes"],
            ["MISSING_SOURCE_BINDING"],
        )

    def test_service_imports_no_outcome_market_odds_pipeline_or_historical_ranking(self):
        source = Path(service.__file__).read_text(encoding="utf-8")
        self.assertEqual(_forbidden_service_imports(source), ())

    def test_forbidden_import_guard_rejects_dynamic_variants(self):
        variants = (
            "__import__('mlb_backtest.pipeline')",
            "import importlib as il\nil.import_module('mlb_backtest.ranking')",
            "from importlib import import_module as load\nload(name)",
            "import builtins as bi\nbi.__import__('market')",
            "import importlib\nload = importlib.import_module\n"
            "load('mlb_backtest.pipeline')",
            "load = __import__\nload('odds')",
            "import importlib\n"
            "getattr(importlib, 'import_module')('market')",
            "import builtins\n"
            "getattr(builtins, '__import__')('outcome')",
            "import importlib\nload = getattr(importlib, name)\n"
            "load('otherwise_safe')",
            "import importlib\nga = getattr\n"
            "load = ga(importlib, 'import_module')\nload('pipeline')",
            "import importlib\nga = getattr\n"
            "load = ga(importlib, name)\nload('otherwise_safe')",
            "import builtins\nga = builtins.getattr\n"
            "load = ga(builtins, '__import__')\nload('ranking')",
            "from builtins import getattr as ga\nimport importlib\n"
            "load = ga(importlib, 'import_module')\nload('pipeline')",
            "from builtins import getattr\nimport builtins\n"
            "load = getattr(builtins, '__import__')\nload('ranking')",
            "from builtins import getattr as ga\nimport importlib\n"
            "load = ga(importlib, name)\nload('otherwise_safe')",
        )
        for source in variants:
            with self.subTest(source=source):
                self.assertTrue(_forbidden_service_imports(source))

    def test_observer_annotation_includes_final_disposition(self):
        annotation = service.build_live_top3_run.__annotations__[
            "final_schedule_observer"
        ]
        self.assertIn("FinalGameObservation", annotation)
        self.assertIn("FinalGameScheduleDisposition", annotation)


if __name__ == "__main__":
    unittest.main()
