import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mlb_backtest import live_sources
from tests.test_live_sources import SCHEDULE_COMPLETE, _http


def policy():
    from mlb_backtest.competition_policy import POSTSEASON_ML_RESEARCH_V1
    return POSTSEASON_ML_RESEARCH_V1


def schedule(kind):
    payload = json.loads(SCHEDULE_COMPLETE.read_bytes())
    payload['dates'][0]['games'][0]['gameType'] = kind
    return json.dumps(payload).encode()


class ResearchSourcesTests(unittest.TestCase):
    def test_equal_to_start_observations_remain_nonprospective(self):
        saved = live_sources.parse_live_schedule(schedule('F'),requested_date='2026-07-21',competition_policy=policy())[0]
        boundary = datetime.fromisoformat(saved['scheduled_start_utc'].replace('Z','+00:00'))
        def fetcher(source,url,**kwargs):
            if '/schedule?' not in url:
                raise AssertionError('late observation must not acquire participants')
            return _http(url,schedule('F'))
        with tempfile.TemporaryDirectory() as tmp:
            slate = live_sources.observe_live_slate(Path(tmp),'2026-07-21',fetcher=fetcher,competition_policy=policy(),durable_clock=lambda:boundary)
            self.assertEqual(slate.initial_exclusion_reasons[saved['game_pk']],('OBSERVATION_AT_OR_AFTER_START',))
            final = live_sources.observe_final_game_schedule(Path(tmp),'2026-07-21',saved,fetcher=fetcher,competition_policy=policy(),durable_clock=lambda:boundary)
            self.assertEqual(final.reason_codes,('OBSERVATION_NOT_PREGAME',))
    def test_supported_and_unsupported_types_preserve_official_label(self):
        for kind in ('F', 'D', 'L', 'W', 'P', 'C', 'S', 'A', 'I', 'E', 'X'):
            with self.subTest(kind=kind):
                rows = live_sources.parse_live_schedule(schedule(kind), requested_date='2026-07-21', competition_policy=policy())
                self.assertEqual(rows[0]['game_type'], kind)

    def test_default_regular_projection_is_unchanged(self):
        from mlb_backtest.competition_policy import REGULAR_SEASON_V1
        raw = SCHEDULE_COMPLETE.read_bytes()
        self.assertEqual(live_sources.parse_live_schedule(raw, requested_date='2026-07-21'), live_sources.parse_live_schedule(raw, requested_date='2026-07-21', competition_policy=REGULAR_SEASON_V1))

    def test_unsupported_target_is_retained_without_boxscore(self):
        def fetcher(source, url, *, max_bytes):
            if '/schedule?' not in url:
                raise AssertionError('unsupported target must not fetch boxscore')
            return _http(url, schedule('P'))
        with tempfile.TemporaryDirectory() as tmp:
            slate = live_sources.observe_live_slate(Path(tmp), '2026-07-21', fetcher=fetcher, competition_policy=policy(), durable_clock=lambda: datetime(2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc))
            self.assertEqual(slate.games[0]['game_type'], 'P')
            self.assertFalse(slate.participants)
            identity = json.loads(slate.source_bindings[0].query_identity_json)
            self.assertEqual(identity['competition_scope'], 'postseason-research-v1')
            self.assertIn('history_population', identity)

    def test_final_recheck_rejects_type_change(self):
        saved = live_sources.parse_live_schedule(schedule('F'), requested_date='2026-07-21', competition_policy=policy())[0]
        with tempfile.TemporaryDirectory() as tmp:
            final = live_sources.observe_final_game_schedule(Path(tmp), '2026-07-21', saved, fetcher=lambda source, url, **kwargs: _http(url, schedule('D')), competition_policy=policy(), durable_clock=lambda: datetime(2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc))
            self.assertIn('GAME_IDENTITY_CHANGED', final.reason_codes)
