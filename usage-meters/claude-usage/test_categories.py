"""Category arithmetic, rolling windows, proxy controls and passive alerts.

All I/O uses disposable files; any real provider request/subprocess is forbidden.
"""
import copy
from datetime import datetime
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

import claude_usage_boxes as b
from test_usage import cu, no_category_dispatch, quiet_speaker


class Categories(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = b.load(Path(__file__).parent / 'fixtures/boxes-v2.toml')
        self.now = b.stamp('2025-11-11T16:00:00-05:00')
        self.policy['approved'] = True
        for run in self.policy['runs']:
            run['approved'] = True
        self.addCleanup(patch.stopall)
        quiet_speaker(self)  # before the host copies the module's functions
        no_category_dispatch(self)
        self.host = SimpleNamespace(**vars(cu))
        self.host.LOG = self.root / 'log.tsv'
        self.host.GUARD_STATE = self.root / 'guard.json'
        self.host.AUTH_DIRS = {'claude': self.root / 'claude/auth', 'codex': self.root / 'codex/auth'}
        for path in self.host.AUTH_DIRS.values():
            path.mkdir(parents=True)
        patch.object(b, 'matrix_notice', return_value=True).start()
        patch.object(cu.subprocess, 'run', side_effect=AssertionError('real subprocess forbidden')).start()
        patch.object(cu.urllib.request, 'urlopen', side_effect=AssertionError('real network forbidden')).start()
        self.rows = [dict(provider=s['provider'], email=s['email'], status='active', u7=.2, uf=.1,
                          u5=None, r5=None, r7=self.now + 200000, rf=self.now + 200000)
                     for s in self.policy['sources']]
        self.auth_files = {}
        for n, row in enumerate(self.rows):
            self.auth_files[self.host.AUTH_DIRS[row['provider']] / f'{n}.json'] = dict(type=row['provider'], email=row['email'], disabled=False, access_token='DO-NOT-EXPOSE')
        for path, data in self.auth_files.items():
            path.write_text(json.dumps(data))
        # Host functions use the original module's constants by design.
        patch.object(cu, 'AUTH_DIRS', self.host.AUTH_DIRS).start()
        patch.object(cu, 'GUARD_STATE', self.host.GUARD_STATE).start()

    def ledger(self, start=None):
        ledger = b.empty_ledger()
        b.observe(ledger, self.rows, start or self.now - 86400, self.policy)
        return ledger

    def record(self, ledger, rows=None, at=None):
        b.observe(ledger, rows or self.rows, at or self.now, self.policy)
        return ledger

    def test_confirmed_non_fable_math(self):
        self.assertAlmostEqual(b.gauges(dict(u7=.72, uf=.99))['non-fable'], 45)
        self.assertEqual(b.gauges(dict(u7=.72, uf=None))['non-fable'], None)

    def test_exclusion_coverage_boundary_requires_continuous_observation(self):
        rows=copy.deepcopy(self.rows)
        rows[0]['fable_excluded']=True
        ledger=b.empty_ledger()
        key='claude:'+rows[0]['email']
        self.record(ledger,rows,self.now)
        self.record(ledger,rows,self.now+60)
        self.assertEqual(ledger['sources'][key]['row']['fable_excluded_since'],self.now)
        self.record(ledger,rows,self.now+181)
        self.assertEqual(ledger['sources'][key]['row']['fable_excluded_since'],self.now+181)
        rows[0]['fable_excluded']=False
        self.record(ledger,rows,self.now+182)
        self.assertIsNone(ledger['sources'][key]['row']['fable_excluded_since'])
        rows[0]['fable_excluded']=True
        self.record(ledger,rows,self.now+183)
        self.assertEqual(ledger['sources'][key]['row']['fable_excluded_since'],self.now+183)

    def test_capacity_resets_are_separate_and_plan_weighted(self):
        amounts = b.capacities(self.policy)
        self.assertAlmostEqual(amounts['fable'], 410)
        self.assertAlmostEqual(amounts['non-fable'], 110)
        self.assertAlmostEqual(amounts['codex'], 314.2857142857)
        changed = copy.deepcopy(self.policy)
        changed['sources'][0]['fable_banked'] = 0
        self.assertEqual(b.capacities(changed)['non-fable'], 110)
        self.assertEqual(b.capacities(changed)['fable'], 110)

    def test_account_change_consumes_one_category(self):
        ledger = self.ledger()
        self.rows[2]['u7'] += .03
        self.rows[3]['u7'] += .14
        self.record(ledger)
        self.assertAlmostEqual(b.spent(ledger, 'codex', self.now - 86400, self.now), 5)

    def test_fable_change_does_not_consume_non_fable(self):
        ledger = self.ledger()
        self.rows[0].update(u7=.25, uf=.2)
        self.record(ledger)
        self.assertAlmostEqual(b.spent(ledger, 'fable', self.now - 86400, self.now), 10)
        self.assertEqual(b.spent(ledger, 'non-fable', self.now - 86400, self.now), 0)

    def test_quantized_non_fable_decrease_does_not_double_charge(self):
        ledger = self.ledger()
        self.rows[0].update(u7=.21)
        self.record(ledger, at=self.now - 600)
        self.rows[0].update(uf=.12)
        self.record(ledger)
        self.assertAlmostEqual(b.spent(ledger, 'non-fable', self.now - 86400, self.now), 2)
        self.rows[0].update(u7=.22, uf=.14)
        self.record(ledger, at=self.now + 600)
        self.assertAlmostEqual(b.spent(ledger, 'non-fable', self.now - 86400, self.now + 600), 2)

    def test_reset_timestamp_jitter_is_not_reset(self):
        ledger = self.ledger()
        self.rows[2]['r7'] += 1
        self.rows[2]['u7'] += .01
        self.record(ledger)
        self.assertEqual(ledger['gaps'], [])
        self.assertAlmostEqual(b.spent(ledger, 'codex', self.now - 86400, self.now), 1)

    def test_weekly_reset_retains_spend_and_flags_unobserved_tail(self):
        ledger = self.ledger()
        self.rows[2]['u7'] = .25
        self.record(ledger, at=self.now - 600)
        self.rows[2].update(u7=.02, r7=self.now + 600000)
        self.record(ledger)
        self.assertAlmostEqual(b.spent(ledger, 'codex', self.now - 86400, self.now), 7)
        self.assertTrue(any(e[1] == 'codex' for e in ledger['gaps']))

    def test_fable_reset_never_creates_fake_non_fable_spend(self):
        for refund in (False, True):
            with self.subTest(refund=refund):
                ledger = self.ledger()
                rows = copy.deepcopy(self.rows)
                rows[0].update(uf=0, rf=self.now + 600000)
                if refund:
                    rows[0]['u7'] -= .05
                self.record(ledger, rows)
                self.assertEqual(b.spent(ledger, 'non-fable', self.now - 86400, self.now), 0)
                self.assertTrue(any(e[1] == 'non-fable' for e in ledger['gaps']))

    def test_weekly_reset_waits_for_post_reset_model_snapshots(self):
        rows = copy.deepcopy(self.rows)
        rows[0].update(u7=.89, uf=1., nf_points=78., observed_at=self.now-600,
                       fable_observed_at=self.now-600, nf_observed_at=self.now-600)
        ledger = b.empty_ledger()
        self.record(ledger, rows, self.now-600)
        rows[0].update(u7=0., observed_at=self.now)
        self.record(ledger, rows, self.now)
        self.record(ledger, rows, self.now+60)
        for category in ('fable', 'non-fable'):
            self.assertEqual(b.spent(ledger, category, self.now-3600, self.now+60), 0.)
        rows[0].update(u7=.02, uf=.01, nf_points=3., observed_at=self.now+120,
                       fable_observed_at=self.now+120, nf_observed_at=self.now+120)
        self.record(ledger, rows, self.now+120)
        self.record(ledger, rows, self.now+180)
        self.assertAlmostEqual(b.spent(ledger, 'fable', self.now-3600, self.now+180), 1.)
        self.assertAlmostEqual(b.spent(ledger, 'non-fable', self.now-3600, self.now+180), 3.)

    def test_all_four_rolling_limits_and_cutoff(self):
        self.policy['runs'] = []
        ledger = self.ledger(start=self.now - 2 * 86400)
        self.record(ledger)
        ledger['events'] = [[self.now - 3600, 'fable', 100], [self.now - 3599, 'fable', 2]]
        items = [r for r in b.readings(self.policy, ledger, self.now) if r['category'] == 'fable']
        self.assertEqual([r['allowed'] for r in items], [12.3, 24.6, 49.2, 102.5])
        self.assertEqual([r['used'] for r in items], [2, 102, 102, 102])

    def test_run_is_one_category_box_and_ignores_pre_run_spend(self):
        ledger = self.ledger()
        run = b.active_run(self.policy, self.now)
        first = run['start']
        self.record(ledger, at=first)
        b.readings(self.policy, ledger, first)
        ledger['events'] += [[first - 1, 'fable', 200], [first + 1, 'fable', 4]]
        self.record(ledger)
        items = b.readings(self.policy, ledger, self.now)
        self.assertEqual(len(items), 3)
        fable = items[0]
        self.assertEqual(fable['used'], 4)
        self.assertEqual(fable['allowed'], 20)
        self.assertEqual(fable['end'], b.stamp('2025-11-11T18:15:00-05:00'))

    def test_new_activation_preserves_logged_spend_and_grandfathers_old_gaps(self):
        self.policy['runs'] = []
        ledger = self.ledger()
        ledger['events'] = [[self.now - 1, 'fable', 200]]
        ledger['gaps'] = [[self.now - 1, 'fable', 'old']]
        ledger['activated_at'] = self.now
        self.record(ledger)
        items = b.readings(self.policy, ledger, self.now)
        self.assertTrue(all(not r['incomplete'] for r in items))
        self.assertTrue(all(r['used'] == 200 for r in items if r['category'] == 'fable'))
        self.assertTrue(all(r['historical_partial'] for r in items if r['category'] == 'fable'))

    def test_final_capacity_does_not_add_week_and_five_hour(self):
        ledger = self.ledger()
        self.rows[3].update(u7=.9, u5=.5, r5=self.now + 7200)
        self.record(ledger)
        amount = b.remaining_capacity(self.policy, ledger, self.now + 3600, self.now)
        # acct2 80 + 200 banked; acct3 min(10 weekly, 8 from remaining5h)/7
        self.assertAlmostEqual(amount['codex'], 280 + 8 / 7)
        self.rows[3]['r5'] = self.now + 600
        self.record(ledger, at=self.now + 1)
        amount = b.remaining_capacity(self.policy, ledger, self.now + 3600, self.now + 1)
        self.assertAlmostEqual(amount['codex'], 280 + 10 / 7)

    def test_final_capacity_is_frozen_at_opening(self):
        policy = copy.deepcopy(self.policy)
        policy['runs'] = [dict(name='final', start=self.now, end=self.now + 3600, approved=True, remaining=True)]
        ledger = self.ledger()
        self.record(ledger)
        original = b.readings(policy, ledger, self.now)[2]['allowed']
        self.rows[2]['u7'] += .1
        self.record(ledger, at=self.now + 600)
        self.assertEqual(b.readings(policy, ledger, self.now + 600)[2]['allowed'], original)

    def test_missing_account_is_incomplete_only_in_its_categories(self):
        self.policy['runs'] = []
        ledger = self.ledger()
        self.record(ledger, self.rows[:2])
        items = b.readings(self.policy, ledger, self.now)
        self.assertTrue(all(r['incomplete'] for r in items if r['category'] == 'codex'))
        self.assertFalse(any(r['incomplete'] for r in items if r['category'] != 'codex'))

    def assertAuthUntouched(self):
        """No hard stops: a category stop never edits an auth file."""
        found = {path: json.loads(path.read_text()) for root in self.host.AUTH_DIRS.values() for path in root.glob('*.json')}
        self.assertEqual(found, self.auth_files)
        self.assertEqual(cu.guard_state(self.host.GUARD_STATE)['files'], {})

    def trip(self, category):
        reading = dict(category=category, name='test', used=20, allowed=20,
                       start=self.now - 1, end=self.now + 100, approved=True, incomplete=False, run=True)
        return b.enforce(self.host, self.policy, self.ledger(), [reading], self.now)

    def test_fable_trip_speaks_fable_and_blocks_no_account(self):
        events = self.trip('fable')
        self.assertTrue(events[0][4])
        self.assertEqual(self.spoken, ['test, 100 percent, Fable.'])
        guard = cu.guard_state(self.host.GUARD_STATE)
        self.assertEqual({v['category'] for v in guard['latches'].values()}, {'fable'})
        self.assertAuthUntouched()
        self.assertNotIn('DO-NOT-EXPOSE', self.host.GUARD_STATE.read_text() + repr(self.spoken))

    def test_non_fable_trip_speaks_opus_only(self):
        self.trip('non-fable')
        self.assertEqual(self.spoken, ['test, 100 percent, Opus.'])
        self.assertAuthUntouched()

    def test_codex_trip_keeps_speaking_after_expiry(self):
        self.trip('codex')
        b.enforce(self.host, self.policy, self.ledger(), [], self.now + 86400)
        self.assertEqual(self.spoken, ['test, 100 percent, Codex.'] * 2)
        guard = cu.guard_state(self.host.GUARD_STATE)
        self.assertEqual({v['category'] for v in guard['latches'].values()}, {'codex'})
        self.assertAuthUntouched()

    def test_unapproved_run_never_creates_stops(self):
        self.policy['runs'][0]['approved'] = False
        ledger = self.ledger()
        self.record(ledger)
        items = b.readings(self.policy, ledger, self.now)
        for r in items:
            r['used'] = 1000
        self.assertEqual(b.enforce(self.host, self.policy, ledger, items, self.now), [])

    def test_human_release_only_its_category_and_rebases(self):
        self.trip('codex')
        self.trip('fable')
        both = self.spoken[-1]
        self.assertEqual(sorted(both.split('. ')), ['test, 100 percent, Codex', 'test, 100 percent, Fable.'])
        path = self.host.LOG.parent / 'category-ledger.json'
        path.write_text(json.dumps(self.ledger()))
        self.assertFalse(b.uncap(self.host, 'codex', path, self.policy, confirmer=lambda _: False, reader=lambda: self.rows))
        self.assertEqual(self.spoken[-1], both)
        self.assertTrue(b.uncap(self.host, 'codex', path, self.policy, confirmer=lambda _: True, reader=lambda: self.rows))
        self.assertEqual(self.spoken[-1], 'test, 100 percent, Fable.')  # Fable is still over its box
        guard = cu.guard_state(self.host.GUARD_STATE)
        self.assertEqual({v['category'] for v in guard['latches'].values()}, {'fable'})
        self.assertAuthUntouched()
        self.assertIn('codex', json.loads(path.read_text())['forgiven'])

    def test_rate_alert_threshold_is_box_exhaustion_before_end(self):
        ledger = self.ledger()
        self.record(ledger)
        b.readings(self.policy, ledger, b.active_run(self.policy, self.now)['start'])
        samples = {'events': [[self.now - 5, 'fable', 1e6]]}
        events = b.rate_events(self.policy, ledger, samples, self.now)
        self.assertEqual(len(events), 1)
        self.assertIn('Five-minute rate', events[0][2])
        self.assertEqual(events[0][3], 900)
        self.assertFalse(events[0][4])
        self.assertIn('stops nothing', events[0][2])
        self.assertEqual(b.rate_events(self.policy, ledger, {'events': []}, self.now), [])

    def test_rate_is_warning_only_without_guard_mutation(self):
        ledger = self.ledger()
        self.record(ledger)
        b.readings(self.policy, ledger, self.now)
        b.rate_events(self.policy, ledger, {'events': [[self.now - 2, 'codex', 1e10]]}, self.now)
        self.assertFalse(self.host.GUARD_STATE.exists())

    def test_delivery_retries_each_destination_and_limits_rate_frequency(self):
        calls = dict(desktop=[], thread=[], file=[])
        def sender(channel):
            def send(line):
                calls[channel].append(line)
                return 'steered' if channel == 'thread' else True
            return send
        senders = {c: sender(c) for c in calls}
        path = self.root / 'notices.json'
        event = [('rate:fable', 2, 'test alert', 900, False)]
        b.deliver(self.host, event, self.now, path, senders)
        b.deliver(self.host, event, self.now + 899, path, senders)
        self.assertTrue(all(len(v) == 1 for v in calls.values()))
        self.assertIn('delivery=steered', calls['file'][0])
        b.deliver(self.host, event, self.now + 900, path, senders)
        self.assertTrue(all(len(v) == 2 for v in calls.values()))

    def test_failed_thread_does_not_hide_desktop_or_file(self):
        path = self.root / 'receipts.json'
        seen = []
        with redirect_stdout(io.StringIO()):
            b.deliver(self.host, [('rate:test', 2, 'alert', 900, False)], self.now, path,
                      {'desktop': lambda line: True, 'thread': lambda line: False, 'file': lambda line: seen.append(line) or True})
        self.assertIn('FAILED', seen[0])
        state = json.loads(path.read_text())['rate:test']
        self.assertNotIn('thread', state)
        self.assertIn('desktop', state)

    def test_steer_success_and_idle_fallback_never_queue(self):
        (self.root / 'notify-thread').write_text('a-thread')
        with patch.object(b.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"delivery":"steered"}', stderr='')) as run:
            self.assertEqual(b.thread_notice(self.host, 'alert'), 'steered')
            self.assertIn('steer', run.call_args.args[0])
        replies = [SimpleNamespace(returncode=1, stdout='', stderr='Thread is idle'), SimpleNamespace(returncode=0, stdout='{"delivery":"started"}', stderr='')]
        with patch.object(b.subprocess, 'run', side_effect=replies) as run:
            self.assertEqual(b.thread_notice(self.host, 'alert'), 'started')
            self.assertEqual([c.args[0][-2] for c in run.call_args_list], ['steer', 'auto'])
            self.assertNotIn('queue', str(run.call_args_list))

    def test_non_idle_steer_failure_never_falls_back(self):
        (self.root / 'notify-thread').write_text('a-thread')
        with patch.object(b.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='transport unavailable')) as run:
            self.assertFalse(b.thread_notice(self.host, 'alert'))
            self.assertEqual(run.call_count, 1)

    def test_missing_notify_file_never_sends_to_old_threads(self):
        self.assertFalse(b.thread_notice(self.host, 'alert'))

    def test_claude_partial_message_usage_is_incremental(self):
        previous = {}
        record = dict(type='assistant', timestamp='2025-11-11T21:00:00Z', message=dict(id='msg', model='claude-fable-5-2', usage=dict(input_tokens=100, output_tokens=10)))
        self.assertEqual(b.usage_record(record, 'claude', previous)[2], 150)
        self.assertIsNone(b.usage_record(record, 'claude', previous))
        record['message']['usage']['output_tokens'] = 20
        self.assertEqual(b.usage_record(record, 'claude', previous)[2], 50)

    def test_codex_cumulative_counts_dedup_and_compaction(self):
        previous = {}
        record = dict(timestamp='2025-11-11T21:00:00Z', payload=dict(type='token_count', info=dict(total_token_usage=dict(input_tokens=1000, cached_input_tokens=100, output_tokens=20), last_token_usage=dict(input_tokens=100, output_tokens=10))))
        self.assertEqual(b.usage_record(record, 'codex', previous)[2], 150)
        self.assertIsNone(b.usage_record(record, 'codex', previous))
        record['payload']['info']['total_token_usage']['input_tokens'] += 50
        self.assertEqual(b.usage_record(record, 'codex', previous)[2], 50)
        record['payload']['info']['total_token_usage']['input_tokens'] = 10
        self.assertEqual(b.usage_record(record, 'codex', previous)[2], 150)

    def test_sampler_incremental_partial_records_and_rotation(self):
        root = self.root / 'sessions'
        root.mkdir()
        path = root / 'test.jsonl'
        record = dict(type='assistant', timestamp=datetime.fromtimestamp(self.now).astimezone().isoformat(), message=dict(id='msg', model='claude-fable-5-2', usage=dict(input_tokens=100)))
        text = json.dumps(record)
        path.write_text(text[:40])
        os.utime(path, (self.now, self.now))
        state = b.sample_sessions({}, {'claude': root}, self.now)
        self.assertEqual(state['events'], [])
        path.write_text(text + '\n')
        os.utime(path, (self.now, self.now))
        state = b.sample_sessions(state, {'claude': root}, self.now)
        self.assertEqual(len(state['events']), 1)
        state = b.sample_sessions(state, {'claude': root}, self.now)
        self.assertEqual(len(state['events']), 1)
        self.assertNotIn('message', json.dumps(state))

    def test_category_messages_are_one_line_and_plain(self):
        ledger = self.ledger()
        self.record(ledger)
        for reading in b.readings(self.policy, ledger, self.now):
            text = b.message(reading, self.now)
            self.assertEqual(len(text.splitlines()), 1)
            self.assertIn('points used', text)
            self.assertIn('left', text)
            self.assertNotIn('last 30', text)

    def test_uncap_captures_unobserved_pre_release_consumption(self):
        self.trip('non-fable')
        ledger = self.ledger()
        path = self.root / 'category-ledger.json'
        path.write_text(json.dumps(ledger))
        rows = copy.deepcopy(self.rows)
        rows[0]['u7'] += .05  # pre-release non-Fable use not yet polled
        self.assertTrue(b.uncap(self.host, 'non-fable', path, self.policy,
                                confirmer=lambda _: True, reader=lambda: rows))
        ledger = json.loads(path.read_text())
        release = ledger['forgiven']['non-fable']
        b.observe(ledger, rows, release + 600, self.policy)
        self.assertEqual(b.spent(ledger, 'non-fable', release, release + 600), 0)
        rows[0]['u7'] += .01
        b.observe(ledger, rows, release + 1200, self.policy)
        self.assertAlmostEqual(b.spent(ledger, 'non-fable', release, release + 1200), 2)

    def test_non_fable_preserves_inherited_global_restrictions(self):
        config = self.host.AUTH_DIRS['claude'].parent / 'config.yaml'
        config.write_text('oauth-excluded-models:\n  claude:\n    - "*restricted*"\n')
        self.trip('non-fable')
        # A per-account exclusion would override the global one; none is written.
        self.assertAuthUntouched()
        self.assertEqual(config.read_text(), 'oauth-excluded-models:\n  claude:\n    - "*restricted*"\n')
        self.assertEqual(self.spoken, ['test, 100 percent, Opus.'])

    def test_intermittent_rate_risk_still_obeys_fifteen_minute_cooldown(self):
        calls = []
        senders = {'desktop': lambda line: calls.append(line) or True}
        path = self.root / 'rate-notices.json'
        event = [('rate:fable', 2, 'test', 900, False)]
        b.deliver(self.host, event, self.now, path, senders)
        b.deliver(self.host, [], self.now + 30, path, senders)
        b.deliver(self.host, event, self.now + 60, path, senders)
        self.assertEqual(len(calls), 1)
        b.deliver(self.host, event, self.now + 900, path, senders)
        self.assertEqual(len(calls), 2)

    def test_oversized_record_cannot_hide_following_usage(self):
        root = self.root / 'sessions'
        root.mkdir()
        path = root / 'large.jsonl'
        path.write_text('{}\n')
        os.utime(path, (self.now, self.now))
        state = b.sample_sessions({}, {'claude': root}, self.now)
        record = dict(type='assistant', timestamp=datetime.fromtimestamp(self.now).astimezone().isoformat(),
                      message=dict(id='new', model='claude-fable-5-2', usage=dict(input_tokens=100)))
        with path.open('a') as out:
            out.write(json.dumps({'type': 'user', 'content': 'x' * 1100000}) + '\n' + json.dumps(record) + '\n')
        os.utime(path, (self.now, self.now))
        for _ in range(3):
            b.sample_sessions(state, {'claude': root}, self.now)
        self.assertEqual(len(state['events']), 1)
        self.assertEqual(next(iter(state['files'].values()))['offset'], path.stat().st_size)
        self.assertNotIn('content', json.dumps(state))

    def test_claude_message_copies_are_not_double_counted(self):
        root = self.root / 'sessions'
        root.mkdir()
        record = dict(type='assistant', timestamp=datetime.fromtimestamp(self.now).astimezone().isoformat(),
                      message=dict(id='shared', model='claude-fable-5-2', usage=dict(input_tokens=100)))
        for name in ('a', 'b'):
            path = root / (name + '.jsonl')
            path.write_text(json.dumps(record) + '\n')
            os.utime(path, (self.now, self.now))
        state = b.sample_sessions({}, {'claude': root}, self.now)
        self.assertEqual(sum(e[2] for e in state['events']), 100)

    def test_passive_rate_covers_normal_rolling_windows(self):
        self.policy['runs'] = []
        ledger = self.ledger()
        self.record(ledger)
        events = b.rate_events(self.policy, ledger, {'events': [[self.now - 5, 'fable', 2e6]]}, self.now)
        self.assertEqual(len(events), 1)
        self.assertIn('rolling window', events[0][2])
        self.assertEqual(events[0][0], 'rate:fable')

    def test_rolling_projection_subtracts_expiring_usage(self):
        reading = dict(category='fable', start=self.now - 3600, end=self.now + 3600, allowed=10)
        ledger = self.ledger()
        ledger['events'] = [[self.now - 3590, 'fable', 9]]
        # 9 points expire in 10s; 1 point/h never crosses the10point cap.
        self.assertIsNone(b.rolling_eta(reading, ledger, [], 1 / 3600, self.now))
        self.assertIsNotNone(b.rolling_eta(reading, ledger, [], 20 / 3600, self.now))

    def test_historical_test_trips_only_non_fable_from_logged_use(self):
        config = self.root / 'test-box.toml'
        config.write_text('name="Live test"\ncategory="non-fable"\nstart="2025-11-10T00:00:00-05:00"\nend="2025-11-13T00:00:00-05:00"\nallow=0.01\n')
        old = [dict(r, time=self.now - 3600) for r in self.rows]
        self.rows[0]['u7'] += .01
        with patch.object(b, 'deliver_current') as delivery, redirect_stdout(io.StringIO()):
            self.assertEqual(b.historical_test(self.host, self.policy, old, self.rows, config, self.now), 0)
        self.assertTrue(delivery.called)
        guard = cu.guard_state(self.host.GUARD_STATE)
        self.assertEqual({v['category'] for v in guard['latches'].values()}, {'non-fable'})
        self.assertEqual(len(self.spoken), 1)
        self.assertRegex(self.spoken[0], r'^Live test, \d+ percent, Opus\.$')
        self.assertAuthUntouched()

    def test_version_two_cli_preserves_one_fetch_per_provider_and_tsv(self):
        config = self.root / 'boxes.toml'
        config.write_text((Path(__file__).parent / 'fixtures/boxes-v2.toml').read_text())
        self.host.fetch = lambda *args: []
        self.host.fetch_modal = lambda: (_ for _ in ()).throw(OSError())
        args = SimpleNamespace(boxes=config, uncap=None, accept_defaults=False, passive=False,
                               test_box=None, log=True, warn=False)
        # Use isolated host log helpers, whose functions retain original globals.
        with patch.object(cu, 'LOG', self.host.LOG), patch.object(self.host, 'fetch', return_value=[]) as fetch:
            self.assertEqual(b.cli(self.host, args, lambda: datetime.fromtimestamp(self.now, cu.ET)), 0)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.host.LOG.read_text(), cu.HEADER)

    def test_passive_cli_never_fetches_provider_state(self):
        config = self.root / 'boxes.toml'
        config.write_text((Path(__file__).parent / 'fixtures/boxes-v2.toml').read_text())
        args = SimpleNamespace(boxes=config, uncap=None, accept_defaults=False, passive=True,
                               test_box=None, log=False, warn=False)
        with patch.object(b, 'passive') as passive, patch.object(self.host, 'fetch', side_effect=AssertionError('network forbidden')):
            self.assertEqual(b.cli(self.host, args, lambda: datetime.fromtimestamp(self.now, cu.ET)), 0)
            self.assertTrue(passive.called)

    @staticmethod
    def limit(errors=(), cooldowns=(), rejected=None, unavailable=None):
        cooldowns = [dict(scope=scope, model=model, retry_at=retry) for scope, model, retry in cooldowns]
        return dict(unavailable=bool(cooldowns) if unavailable is None else unavailable,
                    errors=list(errors), rejected=rejected or {}, cooldowns=cooldowns)

    def test_recovery_category_transitions_debounced_and_category_only(self):
        rows = copy.deepcopy(self.rows)
        for r in rows:
            r['limit'] = self.limit()
        codex = [r for r in rows if r['provider'] == 'codex']
        self.assertTrue(codex)
        dead = lambda retry: self.limit(['usage_limit_reached'], [('credential', None, retry)])
        status = lambda: b.category_status(self.policy, rows, self.now)
        run = lambda: b.recovery_transitions(self.host, status(), self.now)
        self.assertEqual(run(), [])  # silent baseline
        codex[0]['limit'] = dead(self.now + 3600)
        self.assertEqual(run(), [])  # one account out: no category transition
        for r in codex:
            r['limit'] = dead(self.now + 7200)
        codex[0]['limit'] = dead(self.now + 3600)
        self.assertEqual(run(), [])  # first sighting is unconfirmed
        [hit] = run()
        self.assertEqual((hit['category'], hit['transition'], hit['earliest_return']),
                         ('codex', 'limit-hit', self.now + 3600))
        self.assertEqual(run(), [])  # steady state never re-fires
        self.assertEqual(b.recovery_transitions(self.host, {'codex': (None, None)}, self.now), [])  # unreadable is not "back"
        self.assertEqual(run(), [])
        codex[0]['limit'] = self.limit()  # provider reset: the proxy cleared the cooldown
        self.assertEqual(run(), [])
        [back] = run()
        self.assertEqual((back['category'], back['transition']), ('codex', 'available-again'))
        lines = (self.root / 'recovery-category-events.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(l)['id'] for l in lines], [hit['id'], back['id']])
        self.assertNotIn('@', ''.join(lines))  # no individual account in the event
        self.assertEqual(oct((self.root / 'recovery-category-events.jsonl').stat().st_mode & 0o777), '0o600')

    def test_full_meters_without_limit_errors_are_not_out(self):
        rows = copy.deepcopy(self.rows)
        for r in rows:
            r.update(u7=1.0, uf=1.0, r7=self.now + 3600, rf=self.now + 3600, limit=self.limit())
        status = b.category_status(self.policy, rows, self.now)
        self.assertEqual({c: status[c][0] for c in b.CATEGORIES}, dict.fromkeys(b.CATEGORIES, False))

    def test_fable_excluded_account_does_not_veto_fable_death(self):
        rows = copy.deepcopy(self.rows)
        claude = [r for r in rows if r['provider'] == 'claude']
        self.assertGreater(len(claude), 1)
        later = self.now + 3600
        for r in rows:
            r['limit'] = self.limit()
        for r in claude[1:]:
            r['fable_excluded'] = True
        claude[0]['limit'] = self.limit([], [('model', 'claude-fable-5-2', later)], {'claude-fable-5-2': {'7d_oi': later}})
        self.assertEqual(b.category_status(self.policy, rows, self.now)['fable'], (True, later))
        claude[1]['fable_excluded'] = False
        self.assertEqual(b.category_status(self.policy, rows, self.now)['fable'], (False, None))

    def test_account_limit_requires_error_cooldown_and_category_scope(self):
        codex, claude = dict(provider='codex'), dict(provider='claude')
        later = self.now + 3600
        account = lambda row, category, limit: b.account_limit(dict(row, limit=limit), category, self.now)
        usage = ['usage_limit_reached']
        self.assertEqual(account(codex, 'codex', self.limit(usage, [('credential', None, later)])), (True, later))
        self.assertFalse(account(codex, 'codex', self.limit(usage, [('credential', None, self.now - 1)]))[0])  # expired
        self.assertFalse(account(codex, 'codex', self.limit(usage, [('model', 'gpt-6.2-sol', later)]))[0])  # one model
        self.assertFalse(account(codex, 'codex', self.limit([], [('credential', None, later)]))[0])  # no limit error
        self.assertFalse(account(codex, 'codex', self.limit(usage, [('model', 'gpt-6.2-sol', later)], {'gpt-6.2-sol': dict(any=later)}))[0])
        self.assertFalse(account(codex, 'codex', self.limit(usage, [('credential', None, later)], unavailable=False))[0])
        rate = ['rate_limit_error']
        opus = self.limit(rate, [('model', 'claude-opus-5-5', later)], {'claude-opus-5-5': dict(any=later, **{'7d': later})})
        self.assertEqual(account(claude, 'non-fable', opus), (True, later))
        self.assertFalse(account(claude, 'fable', opus)[0])  # an Opus cooldown is not a Fable death
        five_hour = self.limit(rate, [('model', 'claude-opus-5-5', later)], {'claude-opus-5-5': dict(any=later, **{'5h': later})})
        self.assertFalse(account(claude, 'non-fable', five_hour)[0])  # only the shared 7-day window counts
        stale = self.limit([], [('model', 'claude-opus-5-5', later)], {'claude-opus-5-5': {'7d': self.now - 1}})
        self.assertFalse(account(claude, 'non-fable', stale)[0])  # kept headers from a window that already reset
        fable = self.limit(rate, [('model', 'claude-fable-5-2', later)], {'claude-fable-5-2': dict(any=later, **{'7d_oi': later})})
        self.assertEqual(account(claude, 'fable', fable), (True, later))
        partial = self.limit([], [('model', 'claude-fable-5-2', later)], {'claude-fable-5-2': {'7d_oi': later}}, unavailable=False)
        self.assertEqual(account(claude, 'fable', partial), (True, later))  # other models still run: no account error
        self.assertFalse(account(claude, 'fable', self.limit([], [('model', 'claude-fable-5-2', later)]))[0])  # not rejected
        self.assertFalse(account(claude, 'non-fable', fable)[0])
        self.assertEqual(b.account_limit(claude, 'fable', self.now), (None, None))

    def test_notice_thread_unset_sends_nothing(self):
        self.addCleanup(setattr, b, 'NOTICE_THREAD', b.NOTICE_THREAD)
        self.addCleanup(setattr, b, '_run', b._run)
        calls = []
        b._run = lambda *args, **kwargs: calls.append(args)
        b.NOTICE_THREAD = ''
        self.assertTrue(b._death_send('mock', 'text', 'key'))
        self.assertEqual(calls, [])

    def test_own_box_cap_goes_to_recovery_only_and_limit_hit_fans_out_once(self):
        guard = self.host.GUARD_STATE
        self.addCleanup(setattr, b, 'recovery_host_current', b.recovery_host_current)
        b.recovery_host_current = lambda: True
        write = lambda latches: guard.write_text(json.dumps(dict(latches=latches, released=[], files={})))
        write({})
        self.assertEqual(b.cap_transitions(self.host, self.now), [])  # silent baseline
        write({'pool:x': dict(pool='codex:a@x:week', box='acct', at=self.now)})
        self.assertEqual(b.cap_transitions(self.host, self.now), [])  # per-account latch is not a category death
        write({'category:codex:p': dict(pool='p', category='codex', box='30-minute rate', at=self.now - 5, end=self.now + 600)})
        [capped] = b.cap_transitions(self.host, self.now)
        self.assertEqual((capped['category'], capped['transition'], capped['boxes']), ('codex', 'capped', ['30-minute rate']))
        self.assertEqual(b.cap_transitions(self.host, self.now), [])
        # No hard stops: our own box no longer stops a category, so 'capped' is not a death.
        self.assertFalse((self.root / 'category-death-outbox.json').exists())
        sent, idle = [], {'idle'}
        sender = lambda dest, text, key: sent.append((dest, text)) or dest != 'agent:idle'
        lister = lambda projects=None: (['t1', 'idle', 'organizer'], [])
        running = lambda thread: thread not in idle
        self.addCleanup(setattr, b, 'NOTICE_THREAD', b.NOTICE_THREAD)
        b.NOTICE_THREAD = 'organizer'
        deliver = lambda at: b.death_deliver(self.host, at, sender=sender, lister=lister, running=running)
        deliver(self.now)
        self.assertEqual(sent, [])
        write({})
        [cleared] = b.cap_transitions(self.host, self.now + 180)
        self.assertEqual(cleared['transition'], 'cap-cleared')
        deliver(self.now + 180)
        self.assertEqual(sent, [])  # return events: the recovery agent only, no broadcast
        ids = [json.loads(l)['id'] for l in (self.root / 'recovery-category-events.jsonl').read_text().splitlines()]
        self.assertEqual(ids, [capped['id'], cleared['id']])
        # A provider limit-hit is still a death: it fans out once to every destination.
        death = dict(id='codex:limit-hit:1', category='codex', label='Codex', transition='limit-hit',
                     first_seen=self.now, earliest_return=None)
        b.death_queue(self.host, [death], self.now + 200)
        self.assertIn(death['id'], json.loads((self.root / 'category-death-outbox.json').read_text())['outbox'])
        deliver(self.now + 200)
        dests = [d for d, _ in sent]
        self.assertEqual(sorted(dests), ['agent:idle', 'agent:t1', 'discord', 'mock'])  # organizer not double-sent
        self.assertIn('USAGE CATEGORY DEATH: Codex', sent[0][1])
        self.assertIn('grants no uncap', sent[0][1])
        self.assertNotIn('@', ''.join(t for _, t in sent))
        deliver(self.now + 260)
        self.assertEqual(len(sent), 4)  # idle agent skipped after T3 says not running; done

    def test_final_preflight_names_allowance_stops_once_before_final(self):
        prep = self.policy['preparation'] = dict(start=self.now - 86400, end=self.now + 7200, final_end=self.now + 18900, active_hours=26)
        guard = self.host.GUARD_STATE
        guard.write_text(json.dumps(dict(released=[], files={}, latches={
            'category:codex:p': dict(pool='codex:a@x:week', category='codex', box='Preparation total, all usage (final third reserved)',
                                     used=200.0, allowed=199.5, at=self.now, end=prep['end']),
            'category:fable:p': dict(pool='claude:a@x:fable', category='fable', box='30-minute rate', used=3., allowed=2., at=self.now)})))
        self.assertEqual(b.final_preflight(self.host, self.policy, prep['end'] - 901), [])
        self.assertEqual(b.final_preflight(self.host, self.policy, prep['end']), [])
        at = prep['end'] - 840
        [(key, level, line, _, trip)] = b.final_preflight(self.host, self.policy, at)  # rate stop left out
        self.assertEqual((key.split(':')[:2], level, trip), (['final-preflight', 'codex'], 4, False))
        self.assertTrue(line.startswith('STOPPED  Codex:'))
        self.assertIn('uncap', line)
        self.assertNotIn('@', line)
        notices = []
        b.matrix_notice.side_effect = lambda text: notices.append(text) or True
        self.host.desktop_notify = lambda text: True
        events = b.final_preflight(self.host, self.policy, at)
        b.deliver_current(self.host, events, at)
        b.deliver_current(self.host, b.final_preflight(self.host, self.policy, at + 60), at + 60)
        self.assertEqual(len(notices), 1)  # one notice, no repeat a minute later
        self.assertIn('will still block the final runs', notices[0])
        import claude_usage_messages as human
        for legacy in (dict(pool='codex:a@x:week', box='Preparation total', at=self.now), dict(pool='codex:a@x:week', box='Preparation total')):
            guard.write_text(json.dumps(dict(released=[], files={}, latches={'pool:legacy': legacy})))
            [(_, _, legacy_line, _, _)] = b.final_preflight(self.host, self.policy, at)  # pool-only, used=None
            self.assertIsNotNone(human.event(legacy_line, at))  # renders; never an empty "delivered" notice
        guard.write_text(json.dumps(dict(released=[], files={}, latches={})))  # human uncap
        b.deliver_current(self.host, b.final_preflight(self.host, self.policy, at + 120), at + 120)
        self.assertEqual(len(notices), 1)  # cleared silently, no "ended" note

    def test_category_death_retries_until_delivered(self):
        event = dict(id='fable:limit-hit:1', category='fable', label='Fable', transition='limit-hit',
                     first_seen=self.now, earliest_return=None)
        b.death_queue(self.host, [event], self.now)
        b.death_queue(self.host, [event], self.now)  # re-detected after a crash: one entry
        sent, up = [], {'discord': False, 'mock': False}
        sender = lambda dest, text, key: sent.append(dest) or up.get(dest, True)
        lists = iter([(['a'], ['p2']), (['b'], [])])
        lister = lambda projects=None: next(lists)
        running = lambda thread: None  # unknown: keep retrying
        for i in range(4):
            b.death_deliver(self.host, self.now + 7200 * i, sender=sender, lister=lister, running=running)
        self.assertEqual(sent.count('dashboard'), 1)  # supplement after 3 Discord failures
        self.assertEqual(sent.count('discord'), 4)  # Discord keeps retrying
        self.assertEqual(sent.count('agent:b'), 1)  # failed project listing retried later
        up.update(discord=True, mock=True)
        b.death_deliver(self.host, self.now + 30000, sender=sender, lister=lister, running=running)
        state = json.loads((self.root / 'category-death-outbox.json').read_text())
        self.assertEqual((state['outbox'], state['done']), ({}, [event['id']]))  # done only when delivered

    def test_first_cap_reading_of_existing_category_latch_is_a_death(self):
        self.host.GUARD_STATE.write_text(json.dumps(dict(released=[], files={}, latches={
            'category:fable:p': dict(pool='p', category='fable', box='fable box', at=self.now)})))
        [event] = b.cap_transitions(self.host, self.now)
        self.assertEqual(event['transition'], 'capped')

    def test_category_status_missing_source_is_unknown(self):
        rows = [r for r in self.rows if r['provider'] != 'codex']
        self.assertIsNone(b.category_status(self.policy, rows, self.now)['codex'][0])
        blank = copy.deepcopy(self.rows)
        for r in blank:
            r['limit'] = None if r['provider'] == 'codex' else self.limit()  # row present, no cooldown view
        self.assertIsNone(b.category_status(self.policy, blank, self.now)['codex'][0])

    def test_account_and_fraction_alerts_never_wake_notification_thread(self):
        calls = []
        events = [('cap:claude:email:fable', 3, '99 percent', 1800, False),
                  ('fable:last 1h', 2, '75 percent of box', 1800, False),
                  ('rate:fable', 2, 'category rate warning', 900, False),
                  ('category-out:codex', 4, 'whole Codex exhausted', 1800, False)]
        b.deliver(self.host, events, self.now, self.root / 'routing.json',
                  {'desktop': lambda _: True, 'thread': lambda line: calls.append(line) or 'steered', 'file': lambda _: True})
        self.assertEqual(calls, ['category rate warning', 'whole Codex exhausted'])

    def test_new_accounts_do_not_silence_an_existing_category_alert(self):
        self.trip('fable')
        extra = self.host.AUTH_DIRS['claude'] / 'new.json'
        self.auth_files[extra] = dict(type='claude', email='new@test', access_token='safe-test-only')
        extra.write_text(json.dumps(self.auth_files[extra]))
        guard = cu.guard_state(self.host.GUARD_STATE)
        cu.apply_hard_stops(guard, self.host.GUARD_STATE)
        self.assertEqual(self.spoken, ['test, 100 percent, Fable.'] * 2)
        self.assertAuthUntouched()

    def test_rolling_rate_alert_requires_cap_within_one_hour(self):
        self.policy['runs'] = []
        ledger = self.ledger()
        self.record(ledger)
        # 2 categorypoints/h eventually reaches 24h cap, but not in next60m.
        small = 2 / 12 / self.policy['rate_points_per_million']['fable'] * 1e6
        self.assertEqual(b.rate_events(self.policy, ledger, {'events': [[self.now - 5, 'fable', small]]}, self.now), [])
        high = 30 / 12 / self.policy['rate_points_per_million']['fable'] * 1e6
        self.assertEqual(len(b.rate_events(self.policy, ledger, {'events': [[self.now - 5, 'fable', high]]}, self.now)), 1)


if __name__ == '__main__':
    unittest.main()
