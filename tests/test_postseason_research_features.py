import unittest
import hashlib
import json
from dataclasses import asdict, replace
from mlb_backtest.competition_policy import policy_identity
from mlb_backtest.live_history import ResearchLiveHistoryBundle
from mlb_backtest.feature_engine import FeatureHistory
from mlb_backtest.statcast import StatcastData
from mlb_backtest.live_top3_contracts import SourceBinding, canonical_json_bytes
from mlb_backtest.live_features import prepare_live_feature_history, build_live_raw_game_features
from tests.test_live_features import _bundle, _final, _participants, _pa, CUTOFF
from tests.test_postseason_research_sources import policy


def bound(binding, fields):
    values = asdict(binding)
    identity = json.loads(values['query_identity_json'])
    identity.update(fields)
    values['query_identity_json'] = canonical_json_bytes(identity).decode().rstrip('\n')
    values.pop('binding_sha256')
    values['binding_sha256'] = hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    return SourceBinding(**values)


def research_bundle(r_value=.2, f_value=.8):
    base = _bundle()
    rows = []
    identities = []
    for pk, day, kind, xwoba in ((1, '2025-04-29', 'R', r_value), (2, CUTOFF, 'F', f_value)):
        for at_bat, pitcher, batter, half in ((1,501,101,'Top'), (2,601,201,'Bot')):
            row = _pa(pk, day, pitcher, batter, hand='L', home='HOM', away='AWY', top=half, at_bat=at_bat)
            row.update(game_type=kind, estimated_woba_using_speedangle=xwoba)
            rows.append(row)
        identities.append({'game_pk':pk, 'official_date':day, 'status':'Final', 'game_type':kind, 'away_team_id':110, 'home_team_id':120, 'doubleheader_marker':None})
    coverage = {'start_date':'2025-04-29', 'cutoff_date':CUTOFF, 'game_identities':identities, 'played_game_pks':[1,2]}
    digest = hashlib.sha256(canonical_json_bytes(coverage)).hexdigest()
    fields = {**policy_identity(policy()), 'coverage':coverage, 'coverage_sha256':digest, 'coverage_game_pks':[1,2], 'schedule_coverage_sha256':digest}
    bindings = tuple(bound(binding, fields if binding.binding_role in {'HISTORY_SCHEDULE','HISTORY_STATCAST'} else policy_identity(policy())) for binding in base.source_bindings)
    schedule = tuple({'game_pk':identity['game_pk'], 'official_date':identity['official_date'], 'game_type':identity['game_type'], 'away_team_id':110, 'home_team_id':120, 'away_team_code':'AWY', 'team_codes':['AWY','HOM'], 'home_team_code':'HOM', 'venue_name':'Park', 'innings':9} for identity in identities)
    history = FeatureHistory(StatcastData(tuple(rows), tuple(rows)), slot_weights=base.feature_history.slot_weights, schedule_history=schedule, schedule_coverage_start='2025-04-29', schedule_coverage_end=CUTOFF, schedule_coverage_complete=True)
    return ResearchLiveHistoryBundle(base.target_date, CUTOFF, history, base.league_era_by_asof, base.team_code_by_name, base.venue_references, {**policy_identity(policy()), 'namespace':'experimental_daily/history_cache_postseason_research_v1', 'cutoff_date':CUTOFF, 'catalog_sha256':'e'*64, 'coverage':coverage, 'coverage_sha256':digest}, bindings)


def research_final(kind):
    final = _final(game_type=kind)
    return replace(final, source_binding=bound(final.source_binding, {**policy_identity(policy()), 'game_pk':900, 'game_type':kind, 'away_team_id':110, 'home_team_id':120}))


class ResearchFeatureTests(unittest.TestCase):
    def test_missing_terminal_plate_appearances_cannot_erase_recent_components(self):
        bundle = research_bundle()
        history = bundle.feature_history
        damaged = FeatureHistory(StatcastData(history.pitches, ()), slot_weights=history.slot_weights, schedule_history=history.schedule_history.rows, schedule_coverage_start='2025-04-29', schedule_coverage_end=CUTOFF, schedule_coverage_complete=True)
        with self.assertRaisesRegex(ValueError,'COVERAGE'):
            prepare_live_feature_history(replace(bundle,feature_history=damaged),target_game_pks=(900,),competition_policy=policy())
    def test_research_final_query_type_cannot_disagree_with_official_projection(self):
        final = research_final('F')
        bad = replace(final, source_binding=bound(final.source_binding, {'game_type':'D'}))
        with self.assertRaisesRegex(ValueError, 'BINDING'):
            build_live_raw_game_features(_participants(), bad, research_bundle(), {501:1000,601:1000}, competition_policy=policy())
    def test_regular_and_postseason_rows_both_change_frozen_raw_components(self):
        for kind in ('F','D','L','W'):
            bundle = research_bundle()
            final = research_final(kind)
            raw = build_live_raw_game_features(_participants(), final, bundle, {501:1000,601:1000}, competition_policy=policy())
            self.assertEqual(final.projected_game['game_type'], kind)
            self.assertAlmostEqual(raw['away']['starter']['season_xwoba']['value'], .5)
        for r_value, f_value, expected in ((.4,.8,.6), (.2,.4,.3)):
            raw = build_live_raw_game_features(_participants(), research_final('F'), research_bundle(r_value, f_value), {501:1000,601:1000}, competition_policy=policy())
            self.assertAlmostEqual(raw['away']['starter']['season_xwoba']['value'], expected)

    def test_schedule_row_gap_invalidates_prepared_research_authority(self):
        bundle = research_bundle()
        history = bundle.feature_history
        damaged = FeatureHistory(StatcastData(history.pitches, history.plate_appearances), slot_weights=history.slot_weights, schedule_history=history.schedule_history.rows[:1], schedule_coverage_start='2025-04-29', schedule_coverage_end=CUTOFF, schedule_coverage_complete=True)
        with self.assertRaisesRegex(ValueError, 'COVERAGE'):
            prepare_live_feature_history(replace(bundle, feature_history=damaged), target_game_pks=(900,), competition_policy=policy())

    def test_prior_season_and_target_day_never_enter_raw_history(self):
        for day in ('2024-04-30','2025-05-01'):
            bundle = research_bundle()
            history = bundle.feature_history
            rows = [dict(row) for row in history.pitches]
            rows[0]['game_date'] = day
            bad = FeatureHistory(StatcastData(tuple(rows), tuple(rows)), slot_weights=history.slot_weights, schedule_history=history.schedule_history.rows, schedule_coverage_start='2025-04-29', schedule_coverage_end=CUTOFF, schedule_coverage_complete=True)
            with self.assertRaises(ValueError):
                prepare_live_feature_history(replace(bundle, feature_history=bad), target_game_pks=(900,), competition_policy=policy())

    def test_cached_population_and_coverage_tampering_are_rejected(self):
        bundle = research_bundle()
        from mlb_backtest.live_history import _plain_research
        for key, replacement in (('coverage_sha256','0'*64), ('history_population',{'recent_game_types':['R']})):
            damaged = _plain_research(bundle.cache_authority)
            damaged[key] = replacement
            with self.assertRaises(ValueError):
                replace(bundle, cache_authority=damaged)

    def test_regular_bundle_cannot_certify_combined_population(self):
        with self.assertRaisesRegex(ValueError, 'POLICY|POPULATION'):
            prepare_live_feature_history(_bundle(), target_game_pks=(900,), competition_policy=policy())

    def test_regular_prepared_bundle_cannot_be_consumed_for_postseason(self):
        bundle = _bundle()
        prepared = prepare_live_feature_history(bundle, target_game_pks=(900,))
        with self.assertRaisesRegex(ValueError, 'POLICY|POPULATION|PREPARED|BINDING'):
            build_live_raw_game_features(_participants(), _final(game_type='F'), bundle, {501: 1000, 601: 1000}, prepared_history=prepared, competition_policy=policy())
