import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from mlb_backtest import live_history
from tests.test_live_history import _history_schedule, _history_statcast, _statcast_without_game, _venue, _http, _card, _career_payload
from tests.test_postseason_research_sources import policy


DAYS = ('2026-07-17', '2026-07-20')


def combined_data():
    schedule = json.loads(_history_schedule(DAYS))
    for game in schedule['dates'][0]['games']:
        game['gameType'] = 'F'
    stream = io.StringIO(_history_statcast(DAYS).decode())
    reader = csv.DictReader(stream)
    rows = list(reader)
    for row in rows:
        if row['game_date'] == DAYS[0]:
            row['game_type'] = 'F'
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return json.dumps(schedule).encode(), output.getvalue().encode()


def fetcher(schedule, pitches):
    era = (Path(__file__).parent / 'fixtures/mlb_pitching_stats.json').read_bytes()
    def fetch(source, url, *, max_bytes):
        body = schedule if '/schedule?' in url else pitches if 'statcast_search' in url else _venue() if '/venues/' in url else era
        return _http(url, body)
    return fetch


class ResearchHistoryTests(unittest.TestCase):
    def test_each_official_postseason_history_type_is_preserved(self):
        for kind in ('F','D','L','W'):
            schedule, pitches = combined_data()
            schedule = schedule.replace(b'"gameType": "F"', ('"gameType": "'+kind+'"').encode())
            stream = io.StringIO(pitches.decode())
            reader = csv.DictReader(stream)
            rows = list(reader)
            for row in rows:
                if row['game_type'] == 'F':
                    row['game_type'] = kind
            output = io.StringIO()
            writer = csv.DictWriter(output,fieldnames=reader.fieldnames,lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                bundle = live_history.build_live_history(Path(tmp),'2026-07-21',_card(),fetcher=fetcher(schedule,output.getvalue().encode()),sleeper=lambda _:None,competition_policy=policy())
                self.assertEqual({row['game_type'] for row in bundle.feature_history.pitches},{'R',kind})
    def test_combined_history_preserves_types_and_binds_full_range(self):
        schedule, pitches = combined_data()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = live_history.build_live_history(Path(tmp), '2026-07-21', _card(), fetcher=fetcher(schedule, pitches), sleeper=lambda _: None, competition_policy=policy())
            self.assertEqual({row['game_type'] for row in bundle.feature_history.pitches}, {'R', 'F'})
            self.assertEqual(len(bundle.cache_authority['coverage']['played_game_pks']), 30)
            self.assertEqual(bundle.cache_authority['history_population']['recent_game_types'], ('R','F','D','L','W'))
            for binding in bundle.source_bindings:
                self.assertEqual(json.loads(binding.query_identity_json)['competition_scope'], 'postseason-research-v1')
            again = live_history.build_live_history(Path(tmp), '2026-07-21', _card(), fetcher=lambda *a, **k: self.fail('cache replay fetched'), sleeper=lambda _: None, competition_policy=policy())
            self.assertEqual(bundle.cache_authority, again.cache_authority)

    def test_missing_d4_postseason_game_invalidates_complete_d1(self):
        schedule, pitches = combined_data()
        missing = json.loads(schedule)['dates'][0]['games'][0]['gamePk']
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, 'coverage'):
            live_history.build_live_history(Path(tmp), '2026-07-21', _card(), fetcher=fetcher(schedule, _statcast_without_game(pitches, missing)), sleeper=lambda _: None, competition_policy=policy())

    def test_suspended_and_contradictory_history_fail_closed(self):
        schedule, pitches = combined_data()
        for mutation in ('Suspended', 'contradictory', 'empty'):
            payload = json.loads(schedule)
            if mutation == 'Suspended':
                payload['dates'][0]['games'][0]['status']['detailedState'] = mutation
            elif mutation == 'contradictory':
                clone = json.loads(json.dumps(payload['dates'][0]['games'][0]))
                clone['teams']['away']['team']['id'] = 999
                payload['dates'][0]['games'].append(clone)
            else:
                payload['dates'] = []
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                live_history.build_live_history(Path(tmp), '2026-07-21', _card(), fetcher=fetcher(json.dumps(payload).encode(), pitches), sleeper=lambda _: None, competition_policy=policy())

    def test_postponed_then_rescheduled_game_pk_uses_final_identity_and_pitches(self):
        schedule, pitches = combined_data()
        payload = json.loads(schedule)
        postponed = payload['dates'][0]['games'][0]
        game_pk = postponed['gamePk']
        rescheduled = json.loads(json.dumps(postponed))
        postponed['status']['detailedState'] = 'Postponed'
        postponed.pop('linescore', None)
        for side in ('away', 'home'):
            postponed['teams'][side].pop('score', None)
            postponed['teams'][side].pop('isWinner', None)
        rescheduled['officialDate'] = DAYS[1]
        payload['dates'][1]['games'].append(rescheduled)

        stream = io.StringIO(pitches.decode())
        reader = csv.DictReader(stream)
        rows = list(reader)
        for row in rows:
            if int(row['game_pk']) == game_pk:
                row['game_date'] = DAYS[1]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)

        with tempfile.TemporaryDirectory() as tmp:
            bundle = live_history.build_live_history(
                Path(tmp), '2026-07-21', _card(),
                fetcher=fetcher(json.dumps(payload).encode(), output.getvalue().encode()),
                sleeper=lambda _: None, competition_policy=policy(),
            )
        identities = bundle.cache_authority['coverage']['game_identities']
        survivor = [row for row in identities if row['game_pk'] == game_pk]
        self.assertEqual(len(survivor), 1)
        self.assertEqual(survivor[0]['official_date'], DAYS[1])
        self.assertEqual(survivor[0]['status'], 'Final')
        self.assertEqual(len(bundle.cache_authority['coverage']['played_game_pks']), 30)
        self.assertEqual({row['game_pk'] for row in bundle.feature_history.pitches}, set(bundle.cache_authority['coverage']['played_game_pks']))
        self.assertTrue(any(row['game_pk'] == game_pk and row['game_date'] == DAYS[1] for row in bundle.feature_history.pitches))

    def test_common_schedule_parser_rejects_cross_type_repeated_game_pk(self):
        schedule, _ = combined_data()
        payload = json.loads(schedule)
        postponed = payload['dates'][0]['games'][0]
        rescheduled = json.loads(json.dumps(postponed))
        postponed['status']['detailedState'] = 'Postponed'
        postponed.pop('linescore', None)
        for side in ('away', 'home'):
            postponed['teams'][side].pop('score', None)
            postponed['teams'][side].pop('isWinner', None)
        rescheduled['gameType'] = 'F' if postponed['gameType'] == 'R' else 'R'
        rescheduled['officialDate'] = DAYS[1]
        payload['dates'][1]['games'].append(rescheduled)
        with self.assertRaisesRegex(ValueError, 'conflicting duplicate gamePk'):
            live_history.parse_schedule(json.dumps(payload).encode(), game_types=('R', 'F'))

    def test_postponed_does_not_require_pitches(self):
        schedule, pitches = combined_data()
        payload = json.loads(schedule)
        game = payload['dates'][0]['games'][0]
        game['status']['detailedState'] = 'Postponed'
        game.pop('linescore', None)
        for side in ('away', 'home'):
            game['teams'][side].pop('score', None)
            game['teams'][side].pop('isWinner', None)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = live_history.build_live_history(Path(tmp), '2026-07-21', _card(), fetcher=fetcher(json.dumps(payload).encode(), _statcast_without_game(pitches, game['gamePk'])), sleeper=lambda _: None, competition_policy=policy())
            self.assertEqual(len(bundle.cache_authority['coverage']['played_game_pks']), 29)

    def test_known_earlier_regular_game_missing_pitches_fails_coverage(self):
        schedule, pitches = combined_data()
        payload = json.loads(schedule)
        payload['dates'][0]['games'][0]['gameType'] = 'R'
        missing = payload['dates'][0]['games'][0]['gamePk']
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError,'coverage'):
            live_history.build_live_history(Path(tmp),'2026-07-21',_card(),fetcher=fetcher(json.dumps(payload).encode(),_statcast_without_game(pitches,missing)),sleeper=lambda _:None,competition_policy=policy())

    def test_regular_cache_cannot_certify_research_and_career_stays_regular(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_history.build_live_history(root,'2026-07-21',_card(),fetcher=fetcher(_history_schedule(),_history_statcast()),sleeper=lambda _:None)
            with self.assertRaises(AssertionError):
                live_history.build_live_history(root,'2026-07-21',_card(),fetcher=lambda *a, **k: (_ for _ in ()).throw(AssertionError('must collect combined population')),sleeper=lambda _:None,competition_policy=policy())
            values, bindings = live_history.load_live_career_bf(root,'2026-07-21',[10],fetcher=lambda source,url,**kwargs:_http(url,_career_payload()),sleeper=lambda _:None,competition_policy=policy())
            self.assertEqual(values[10],123)
            self.assertIn('gameType=R',bindings[0].requested_url)
            self.assertEqual(json.loads(bindings[0].query_identity_json)['history_population']['career_bf_population'],'REGULAR_SEASON')
            self.assertIn('history_receipts_postseason_research_v1',bindings[0].receipt_relative)
