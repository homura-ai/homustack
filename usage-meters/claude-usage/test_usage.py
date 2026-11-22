"""Fake data only: never calls the proxy."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('cu', str(Path(__file__).with_name('claude-usage')))
spec = importlib.util.spec_from_loader(loader.name, loader)
cu = importlib.util.module_from_spec(spec)
loader.exec_module(cu)

SECRET = 'SECRET-TOKEN-123'


def no_category_dispatch(test):
    """The real machine has /etc/agent-cloud/category-routes.json and passwordless sudo, and
    claude_usage_category.publish binds the real subprocess.run when it is defined. Point the
    routes file at a missing path so no test can queue a live CATEGORY notice."""
    import claude_usage_category
    patch.object(claude_usage_category, 'CONFIG', test.root / 'no-category-routes.json').start()


def quiet_speaker(test):
    """No hard stops: where a stop used to fire, a voice reads the over-cap boxes.

    Record it instead of starting it. test.spoken gets each phrase the repeating voice is
    set to and None each time it is stopped; test.said gets each one-off reminder. Starting
    any real process fails the test, and the speech files live in the test's own folder.
    """
    test.spoken, test.said = [], []
    default = cu.play_music.__defaults__[0]
    patch.object(cu, 'play_music', side_effect=lambda phrase=default: test.spoken.append(phrase)).start()
    patch.object(cu, 'stop_music', side_effect=lambda: test.spoken.append(None)).start()
    patch.object(cu, 'say_once', side_effect=test.said.append).start()
    patch.object(cu, 'SPEECH_TEXT', test.root / 'over-cap-speech.txt').start()
    patch.object(cu, 'SPEECH_PID', test.root / 'over-cap-speech.pid').start()
    patch.object(cu.subprocess, 'Popen', side_effect=AssertionError('real speaker or process forbidden')).start()


DATA = {'files': [
    {'email': 'a@x', 'provider': 'claude', 'status': 'active', 'access_token': SECRET,
     'quota': {'signals': {'Anthropic-Ratelimit-Unified-7d-Utilization': 0.33,
                           'Anthropic-Ratelimit-Unified-7d-Reset': 1763204400}}},
    {'email': 'b@x', 'provider': 'claude', 'status': 'error', 'refresh_token': SECRET,
     'quota': {'signals': {'Anthropic-Ratelimit-Unified-5h-Utilization': 1.02,
                           'Anthropic-Ratelimit-Unified-5h-Reset': 1762819200,
                           'Anthropic-Ratelimit-Unified-7d-Utilization': 1.11,
                           'Anthropic-Ratelimit-Unified-7d-Reset': 1762851600}}},
    {'email': 'codex@x', 'provider': 'codex', 'status': 'active'}]}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.resets_patch = patch.object(cu, 'RESETS', Path(self.temporary.name) / 'resets.json')
        self.resets_patch.start()
        self.addCleanup(self.resets_patch.stop)

    def test_limit_evidence_from_proxy_auth_state(self):
        claude = dict(provider='claude', email='a@x', status='error', unavailable=True,
                      status_message=json.dumps(dict(type='error', error=dict(type='rate_limit_error', message='secret text'))),
                      cooldowns=[dict(scope='model', model_key='claude-opus-5-5', reason='quota',
                                      retry_at='2026-11-24T09:00:22Z', http_status=429),
                                 dict(scope='model', model_key='claude-sonnet-5-5', reason='transient',
                                      retry_at='2026-11-24T09:00:22Z')],
                      model_quotas={'claude-opus-5-5': dict(signals={cu.PREFIX + 'Status': 'rejected',
                                                                     cu.PREFIX + '7d-Status': 'rejected',
                                                                     cu.PREFIX + '7d-Reset': '1795510800',
                                                                     cu.PREFIX + '5h-Status': 'allowed',
                                                                     cu.PREFIX + '7d_sonnet-Status': 'rejected'})})
        [row] = cu.parse([claude], excluded_accounts=set())
        self.assertEqual(row['limit'], dict(unavailable=True, errors=['rate_limit_error'],
                                            rejected={'claude-opus-5-5': {'any': None, '7d': 1795510800., '7d_sonnet': None}},
                                            cooldowns=[dict(scope='model', model='claude-opus-5-5',
                                                            retry_at=cu.observation_time('2026-11-24T09:00:22Z'))]))
        self.assertNotIn('secret text', json.dumps(row))
        fable = dict(claude, model_quotas={'claude-fable-5-2': dict(observed_at='2026-11-21T23:00:00Z', signals={
            cu.PREFIX + '7d_oi-Status': 'rejected', 'Retry-After': '120'})})
        [row] = cu.parse([fable], excluded_accounts=set())
        self.assertEqual(row['limit']['rejected'], {'claude-fable-5-2': {'7d_oi': cu.observation_time('2026-11-21T23:00:00Z') + 120}})
        codex = dict(provider='codex', email='c@x', status='error', unavailable=True,
                     status_message=json.dumps(dict(error=dict(type='usage_limit_reached'))),
                     cooldowns=[dict(scope='credential', reason='credential_quota', retry_at='2026-11-23T23:19:19Z')])
        [row] = cu.parse_codex([codex], checked_at=0)
        self.assertEqual((row['limit']['errors'], row['limit']['cooldowns'][0]['scope']), (['usage_limit_reached'], 'credential'))
        [old] = cu.parse_codex([dict(provider='codex', email='d@x', status='active')], checked_at=0)
        self.assertIsNone(old['limit'])  # a proxy without a cooldown view proves nothing

    def test_parse_and_render(self):
        rows = cu.parse(DATA)
        self.assertEqual([r['email'] for r in rows], ['a@x', 'b@x'])
        text = cu.render(rows)
        self.assertIn('a@x: active | 5h - (resets -) | 7d 33% (resets Sat 11/15 06:00 ET)', text)
        self.assertIn('b@x: error | 5h 102% (resets Mon 11/10 19:00 ET) | 7d 111% (resets Tue 11/11 04:00 ET)', text)

    def test_no_secrets_in_output(self):
        rows = cu.parse(DATA)
        blob = cu.render(rows) + cu.tsv(rows, datetime(2025, 11, 10, 23, 0, tzinfo=timezone.utc)) + repr(rows)
        self.assertNotIn(SECRET, blob)

    def test_tsv_line(self):
        rows = cu.parse(DATA)
        line = cu.tsv(rows, datetime(2025, 11, 10, 23, 0, tzinfo=timezone.utc)).splitlines()[0]
        self.assertEqual(line, '2025-11-10T18:00:00\ta@x\tactive\t\t0.33\t1763204400\tclaude\t\t\t')
        second = cu.tsv(rows, datetime(2025, 11, 10, 23, 0, tzinfo=timezone.utc)).splitlines()[1]
        self.assertTrue(second.endswith('\tclaude\t1762819200\t\t'))

    def test_fetch_uses_bearer_from_file_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / 'k'
            key.write_text(SECRET + '\n')
            seen = {}

            class R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return b'{"files": []}'

            def opener(request, timeout=None):
                seen['auth'] = request.get_header('Authorization')
                seen['url'] = request.full_url
                return R()
            self.assertEqual(cu.fetch(key_file=key, opener=opener), {'files': []})
            self.assertEqual(seen['auth'], 'Bearer ' + SECRET)
            self.assertTrue(seen['url'].startswith('http://127.0.0.1:'))

    def test_codex_parse_and_render(self):
        data = {'files': [
            {'email': 'acct2@x', 'provider': 'codex', 'status': 'active', 'access_token': SECRET,
             'quota': {'signals': {'X-Codex-Plan-Type': 'prolite', 'X-Codex-Primary-Used-Percent': 1,
                                   'X-Codex-Primary-Window-Minutes': 10080, 'X-Codex-Primary-Reset-At': 1763422334,
                                   'X-Codex-Secondary-Used-Percent': 0}}},
            {'email': 'acct3@x', 'provider': 'codex', 'status': 'error', 'refresh_token': SECRET,
             'next_retry_after': '2025-11-10T22:57:41.000829251-05:00',
             'quota': {'signals': {'X-Codex-Plan-Type': 'edu_plus', 'X-Codex-Primary-Used-Percent': 100,
                                   'X-Codex-Primary-Window-Minutes': 300, 'X-Codex-Primary-Reset-At': 1762833461,
                                   'X-Codex-Secondary-Used-Percent': 9, 'X-Codex-Secondary-Reset-At': 1763420740}}},
            {'email': 'bare@x', 'provider': 'codex', 'status': 'active', 'quota': {'signals': {}}},
            {'email': 'c@x', 'provider': 'claude', 'status': 'active'}]}
        rows = cu.parse_codex(data)
        self.assertEqual([r['email'] for r in rows], ['acct2@x', 'acct3@x', 'bare@x'])
        self.assertEqual((rows[0]['u5'], rows[0]['u7']), (None, 0.01))
        self.assertEqual((rows[1]['u5'], rows[1]['u7']), (1.0, 0.09))
        text = cu.render(rows)
        self.assertIn('codex acct2@x: active | 5h - (resets -) | 7d 1% (resets ', text)
        self.assertIn('plan prolite', text)
        self.assertIn('codex acct3@x: error | 5h 100%', text)
        self.assertIn('7d 9%', text)
        self.assertIn('retry 2025-11-10T22:57', text)
        self.assertIn('codex bare@x: active | the proxy exposes no usage figures', text)
        self.assertNotIn(SECRET, text + cu.tsv(rows, datetime(2025, 11, 10, 23, 0, tzinfo=timezone.utc)))
        self.assertEqual(cu.tsv(rows, datetime(2025, 11, 10, 23, 0, tzinfo=timezone.utc)).splitlines()[0].split('\t')[6], 'codex')

    def test_log_migration_adds_provider_column(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with patch.object(cu, 'LOG', Path(d) / 'sub' / 'log.tsv'):
                cu.LOG.parent.mkdir()
                cu.LOG.write_text('time_et\temail\tstatus\tutil_5h\tutil_7d\treset_7d_epoch\n2025-11-10T18:15:52\ta@x\tactive\t\t0.34\t1763204400\n')
                cu.prepare_log()
                lines = cu.LOG.read_text().splitlines()
                self.assertTrue(lines[0].endswith('\tprovider\treset_5h_epoch\tutil_7d_fable\treset_7d_fable_epoch'))
                self.assertTrue(lines[1].endswith('\tclaude\t\t\t'))
                cu.prepare_log()
                self.assertEqual(cu.LOG.read_text().splitlines(), lines)
                self.assertEqual(cu.LOG.stat().st_mode & 0o777, 0o600)

    def test_bare_list_input(self):
        self.assertEqual(cu.parse([]), [])


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state' / 'warn-state.json'
        self.config = self.root / 'config' / 'must-last.json'
        self.now = datetime(2025, 11, 10, 18, tzinfo=cu.ET)
        self.threads, self.desktops = [], []
        self.output = io.StringIO()
        self.addCleanup(patch.stopall)
        # Every test is barred from real processes, proxy calls and production state.
        patch.object(cu.subprocess, 'run', side_effect=AssertionError('real subprocess forbidden')).start()
        patch.object(cu.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')).start()
        patch.object(cu, 'LOG', self.root / 'log.tsv').start()
        patch.object(cu, 'WARN_STATE', self.state).start()
        patch.object(cu, 'BOX_STATE', self.state).start()
        patch.object(cu, 'BOXES', self.root / 'boxes.toml').start()
        patch.object(cu, 'GUARD_STATE', self.root / 'hard-stops.json').start()
        patch.object(cu, 'AUTH_DIRS', {'claude': self.root / 'no-claude-auth', 'codex': self.root / 'no-codex-auth'}).start()
        quiet_speaker(self)
        no_category_dispatch(self)
        patch.object(cu, 'MUST_LAST', self.config).start()
        self.resets = self.root / 'config' / 'resets.json'
        patch.object(cu, 'RESETS', self.resets).start()
        patch.object(cu, 'THREAD_FILE', self.root / 'notify-thread').start()
        patch.object(cu, 'fetch', side_effect=AssertionError('real fetch forbidden')).start()
        patch.object(cu, 'fetch_modal', side_effect=OSError('modal forbidden in tests')).start()
        patch.object(cu, 'MODAL_LOG', self.root / 'modal.tsv').start()
        patch.object(cu, 'MODAL_STATE', self.root / 'modal-state.json').start()

    def row(self, utilization=0.8, hours_to_reset=12, **changes):
        result = dict(provider='claude', email='fake@example.test', status='active',
                      u5=None, r5=None, u7=utilization,
                      r7=(self.now + timedelta(hours=hours_to_reset)).timestamp())
        result.update(changes)
        # Tests with a Fable meter supply its own known deadline explicitly.
        if 'uf' in changes:
            result.setdefault('rf', result['r7'])
        return result

    def sample(self, row, minutes_ago, utilization, window='7d', **changes):
        return dict(row, time=(self.now - timedelta(minutes=minutes_ago)).timestamp(),
                    **{'u' + window[0]: utilization}, **changes)

    def history(self, row):
        return [self.sample(row, 120, 0.4), self.sample(row, 30, 0.7)]

    def warning(self, row, history=None, now=None):
        def thread(text):
            self.threads.append(text)
            return True

        def desktop(text):
            self.desktops.append(text)
            return True

        with redirect_stdout(self.output):
            return cu.warn([row], self.history(row) if history is None else history,
                           now or self.now, state_path=self.state, config_path=self.config,
                           thread_sender=thread, desktop_sender=desktop)

    def configure(self, entries):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({'must_last': entries}))

    def test_warning_before_reset_and_plain_projection(self):
        row = self.row()
        lines = self.warning(row)
        self.assertEqual(len(lines), 1)
        self.assertIn("at the last 30 minutes' pace", lines[0])
        self.assertIn('runs out Mon 19:00 ET, before its Tue 06:00 ET reset', lines[0])
        self.assertEqual(self.threads, ['For you: ' + lines[0]])
        self.assertEqual(self.desktops, lines)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        text = cu.render([row], self.history(row), self.now)
        self.assertIn('7d: net observed pace; at the last 30 min pace: reaches 100% Mon 19:00 ET', text)
        self.assertNotIn(SECRET, self.output.getvalue() + self.state.read_text() + text)

    def test_after_reset_never_warns_even_before_thursday(self):
        # Regression: reset 19:00, exhaustion 20:00, must-last Thursday.
        row = self.row(hours_to_reset=1)
        history = [self.sample(row, 120, 0.6), self.sample(row, 30, 0.75)]
        self.configure([{'label': "Thursday's launch", 'at': '2025-11-13T21:00:00-05:00'}])
        p = cu.project(row, '7d', history, self.now)
        self.assertEqual(p['status'], 'no exhaustion before next reset')
        self.assertNotIn('at', p)
        self.assertEqual(cu.short_when(row['r7']), 'Mon 19:00 ET')
        self.assertEqual(self.warning(row, history), [])
        self.assertEqual((self.threads, self.desktops), ([], []))

    def test_equal_to_reset_does_not_warn(self):
        row = self.row(hours_to_reset=1)
        self.assertEqual(self.warning(row), [])

    def test_must_last_earliest_future_between_runout_and_reset(self):
        row = self.row()
        self.configure([
            {'label': 'later', 'at': '2025-11-10T23:00:00-05:00'},
            {'label': 'earliest', 'at': '2025-11-10T20:00:00-05:00'},
            {'label': 'past', 'at': '2025-11-10T17:00:00-05:00'},
            {'label': 'before runout', 'at': '2025-11-10T18:30:00-05:00'},
            {'label': 'after reset', 'at': '2025-11-13T21:00:00-05:00'},
        ])
        self.assertIn('(and before earliest)', self.warning(row)[0])

    def test_default_must_last_and_past_filter(self):
        self.assertEqual(cu.must_last_times(self.config, self.now), [])  # no built-in deadlines
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({'must_last': [
            {'label': "tonight's demo end", 'at': '2025-11-10T23:45:00-05:00'},
            {'label': "Thursday's launch", 'at': '2025-11-13T21:00:00-05:00'}]}))
        self.assertIn("before tonight's demo end", self.warning(self.row())[0])
        tomorrow = self.now + timedelta(days=1)
        labels = [label for _, label in cu.must_last_times(self.config, tomorrow)]
        self.assertEqual(labels, ["Thursday's launch"])

    def test_insufficient_history_no_warning(self):
        row = self.row()
        for history in ([], [self.sample(row, 14, 0.7)], [self.sample(row, 121, 0.2)]):
            with self.subTest(history=history):
                self.assertEqual(cu.project(row, '7d', history, self.now)['status'], 'not enough data')
                self.assertEqual(self.warning(row, history), [])
        self.assertIn('7d: net observed pace: not enough data', cu.render([row], [], self.now))

    def test_exactly_fifteen_minutes_qualifies(self):
        row = self.row()
        self.assertTrue(self.warning(row, [self.sample(row, 15, 0.7)]))

    def test_no_burn(self):
        row = self.row()
        history = [self.sample(row, 30, row['u7'])]
        self.assertEqual(cu.project(row, '7d', history, self.now)['status'], 'no burn')
        self.assertIn('7d: net observed pace: no burn', cu.render([row], history, self.now))
        self.assertEqual(self.warning(row, history), [])

    def test_reset_drop_restarts_span(self):
        row = self.row(utilization=0.2)
        history = [self.sample(row, 120, 0.8), self.sample(row, 30, 0.95), self.sample(row, 10, 0.1)]
        self.assertEqual(cu.project(row, '7d', history, self.now)['status'], 'collecting post-reset pace')
        self.assertEqual(self.warning(row, history), [])
        history[2]['time'] = (self.now - timedelta(minutes=20)).timestamp()
        p = cu.project(row, '7d', history, self.now)
        self.assertAlmostEqual(p['rate'], 0.3)
        self.assertTrue(self.warning(row, history))

    def test_changed_reset_restarts_span_without_drop(self):
        row = self.row()
        history = [self.sample(row, 120, 0.2, r7=row['r7'] - 3600),
                   self.sample(row, 10, 0.7)]
        self.assertEqual(cu.project(row, '7d', history, self.now)['status'], 'collecting post-reset pace')

    def test_fractional_live_reset_matches_logged_integer(self):
        row = self.row()
        row['r7'] = int(row['r7']) + 0.6  # live value with a fraction; the TSV log stores whole seconds
        history = [self.sample(row, 120, 0.2, r7=int(row['r7'])), self.sample(row, 30, 0.7, r7=int(row['r7']))]
        p = cu.project(row, '7d', history, self.now)
        self.assertNotEqual(p['status'], 'collecting post-reset pace')
        self.assertIsNotNone(p.get('rate'))

    def test_higher_long_rate_and_basis(self):
        row = self.row()
        history = [self.sample(row, 120, 0.2), self.sample(row, 30, 0.75)]
        self.assertEqual(cu.project(row, '7d', history, self.now)['minutes'], 120)
        self.assertIn("last 2 hours' pace", self.warning(row, history)[0])
        self.assertIn('last 2 h pace', cu.render([row], history, self.now))

    def test_hourly_limit_and_earlier_projection_rewarn(self):
        row = self.row()
        # Initial run-out: 22:00. Fast recent burn later moves it to 18:40.
        slow = [self.sample(row, 120, 0.7), self.sample(row, 30, 0.775)]
        first = self.warning(row, slow)
        self.assertTrue(first)
        later = self.now + timedelta(minutes=10)
        self.assertEqual(self.warning(row, slow, later), [])
        fast = [self.sample(row, 20, 0.4)]
        self.assertTrue(self.warning(row, fast, later))
        self.assertEqual(len(self.threads), 2)
        # A full hour since the latest warning permits the same projection again.
        self.assertTrue(self.warning(row, fast, later + timedelta(hours=1)))

    def test_exactly_one_hour_earlier_does_not_bypass_limit(self):
        row = self.row()
        self.warning(row, [self.sample(row, 120, 0.7)])  # 22:00
        # 0.2/3 per hour => 21:00, exactly one hour earlier.
        self.assertEqual(self.warning(row, [self.sample(row, 120, 2 / 3)]), [])

    def test_back_on_pace_once_and_clears_state(self):
        row = self.row()
        self.warning(row)
        steady = [self.sample(row, 120, row['u7'])]
        self.assertEqual(self.warning(row, steady), ['Claude fake@example.test 7d: back on pace. ' + cu.SHARED_GUARD])
        self.assertEqual(self.threads[-1], 'For you: Claude fake@example.test 7d: back on pace. ' + cu.SHARED_GUARD)
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.assertEqual(self.warning(row, steady), [])

    def test_skips_exhausted_inactive_and_missing_data(self):
        for changes in ({'u7': 1}, {'u7': 1.2}, {'u5': 1}, {'status': 'error'}, {'u7': None}, {'r7': None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.warning(self.row(**changes)), [])
        self.assertEqual(self.threads, [])

    def test_5h_and_provider_isolation(self):
        row = self.row(provider='codex', u5=0.8, r5=self.row()['r7'], u7=None, exposed=True)
        history = [self.sample(row, 30, 0.6, window='5h')]
        other = self.sample(row, 120, 0, window='5h')
        other['provider'] = 'claude'
        lines = self.warning(row, [other] + history)
        self.assertEqual(len(lines), 1)
        self.assertIn('Codex fake@example.test:', lines[0])
        self.assertIn('5-hour window', lines[0])
        self.assertIn("last 2 hours' pace", lines[0])
        self.assertAlmostEqual(cu.project(row, '5h', [other] + history, self.now)['rate'], 0.4)

    def test_provider_log_migration_and_read_roundtrip(self):
        cu.LOG.write_text('time_et\temail\tstatus\tutil_5h\tutil_7d\treset_7d_epoch\tprovider\n'
                          '2025-11-10T18:00:00\ta@x\tactive\t0.2\t0.3\t1763204400\tcodex\n')
        cu.prepare_log()
        migrated = cu.LOG.read_text()
        self.assertTrue(migrated.endswith('\tcodex\t\t\t\n'))
        cu.prepare_log()
        self.assertEqual(cu.LOG.read_text(), migrated)
        parsed = cu.read_log(cu.LOG)
        self.assertEqual(parsed[0]['provider'], 'codex')
        self.assertIsNone(parsed[0]['r5'])
        self.assertEqual(parsed[0]['time'], self.now.timestamp())
        row = self.row(u5=0.2, r5=self.now.timestamp() + 3600)
        with cu.LOG.open('a') as out:
            out.write(cu.tsv([row], self.now))
        self.assertEqual(cu.read_log(cu.LOG)[1]['r5'], row['r5'])

    def test_missing_thread_file_falls_back_to_env_thread(self):
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0)

        with patch.object(cu, 'ENV_THREAD', ''):
            self.assertFalse(cu.send_thread('For you: fake', thread_file=self.root / 'missing', run=run))
        self.assertEqual(calls, [])  # no thread configured: nothing sent
        with patch.object(cu, 'ENV_THREAD', 'fallback-thread'):
            self.assertTrue(cu.send_thread('For you: fake', thread_file=self.root / 'missing', run=run))
        args, kwargs = calls[0]
        self.assertEqual(args, [str(cu.T3), 'thread', 'send', 'fallback-thread', '-', '--mode', 'auto'])
        self.assertEqual(kwargs['input'], 'For you: fake')
        self.assertNotIn('shell', kwargs)

    def test_failed_file_thread_send_falls_back_and_desktop_uses_clean_env(self):
        val = self.root / 'notify-thread'
        val.write_text('fake-file-thread\n')
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=1 if args[3] == 'fake-file-thread' else 0)

        with patch.object(cu, 'ENV_THREAD', 'fallback-thread'):
            self.assertTrue(cu.send_thread('For you: fake', thread_file=val, run=run))
        self.assertEqual([args[3] for args in calls], ['fake-file-thread', 'fallback-thread'])
        desktop_calls = []

        def desktop(args, **kwargs):
            desktop_calls.append(args)
            return SimpleNamespace(returncode=0)

        self.assertTrue(cu.desktop_notify('fake', run=desktop))
        self.assertEqual(desktop_calls[0], ['env', '-u', 'LD_LIBRARY_PATH', '-u', 'LD_PRELOAD',
                                          f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{cu.os.getuid()}/bus',
                                          'notify-send', '-u', 'critical', '-a', 'claude-usage', 'Usage', 'fake'])

    def test_failed_delivery_retries_and_no_exception_details_leak(self):
        row = self.row()
        with redirect_stdout(self.output):
            result = cu.warn([row], self.history(row), self.now, state_path=self.state,
                             config_path=self.config, thread_sender=lambda _: False,
                             desktop_sender=lambda _: False)
        self.assertTrue(result)
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.assertTrue(self.warning(row))

        def fail(*args, **kwargs):
            raise subprocess.TimeoutExpired(SECRET, 1)

        self.assertFalse(cu.send_thread('For you: fake', thread_file=self.root / 'missing', run=fail))
        self.assertFalse(cu.desktop_notify('fake', run=fail))
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_main_warn_implies_logging_with_fake_clock_and_senders(self):
        row = self.row()
        cu.BOXES.write_text('''approved = true
[[boxes]]
name = "practice"
start = "2025-11-10T16:00:00-05:00"
end = "2025-11-10T23:00:00-05:00"
[boxes.allow]
"claude:fake@example.test:all" = 50
''')
        cu.prepare_log()
        old = self.row(utilization=0.4)
        with cu.LOG.open('a') as out:
            out.write(cu.tsv([old], self.now - timedelta(hours=2)))
        # Fake provider fetches; real fetch and subprocess calls remain forbidden.
        with patch.object(cu, 'fetch', side_effect=[DATA, {'files': []}]), \
                patch.object(cu, 'parse', return_value=[row]), \
                patch.object(cu, 'send_direct', side_effect=lambda text: self.threads.append(text) or True), \
                patch.object(cu, 'desktop_notify', side_effect=lambda text: self.desktops.append(text) or True), \
                redirect_stdout(self.output):
            self.assertEqual(cu.main(['--warn'], clock=lambda: self.now), 0)
        self.assertEqual(len(cu.read_log(cu.LOG)), 2)
        self.assertEqual(len(self.threads), 1)
        self.assertIn('40.0 of 50 points used', self.threads[0])
        self.assertIn('At this rate it will exceed 50', self.threads[0])
        self.assertEqual(self.desktops, self.threads)
        self.assertNotIn(SECRET, self.output.getvalue() + cu.LOG.read_text() + self.state.read_text())

    def fable_history(self, row, now=None, old_fable=0.6, old_all=None, minutes=30):
        return [dict(row, time=((now or self.now) - timedelta(minutes=minutes)).timestamp(),
                     uf=old_fable, u7=row['u7'] if old_all is None else old_all)]

    def configure_resets(self, left):
        self.resets.parent.mkdir(parents=True, exist_ok=True)
        self.resets.write_text(json.dumps({'fable_resets_left': left}))

    def test_verified_1904_meters_with_labelled_estimate_and_no_secrets(self):
        self.now = datetime(2025, 11, 10, 19, 4, tzinfo=cu.ET)
        row = self.row(utilization=0.39, uf=0.41)
        fable_signals = {cu.PREFIX + '7d_oi-Utilization': 0.41,
                         cu.PREFIX + '7d_oi-Reset': row['r7'], 'access_token': SECRET}
        for location in ('quota', 'file'):
            data = {'provider': 'claude', 'email': row['email'], 'status': 'active',
                    'access_token': SECRET,
                    'quota': {'signals': {cu.PREFIX + '7d-Utilization': 0.39,
                                          cu.PREFIX + '7d-Reset': row['r7']}}}
            model_parent = data['quota'] if location == 'quota' else data
            model_parent['model_quotas'] = {'claude-fable-5-2': {'signals': fable_signals}}
            parsed = cu.parse({'files': [data]})
            self.assertEqual((parsed[0]['u7'], parsed[0]['uf']), (0.39, 0.41))
            self.assertEqual(parsed[0]['rf'], row['r7'])
            text = cu.render(parsed, now=self.now)
            self.assertIn('all models (7d) 39% | Fable limit (7d_oi) 41%', text)
            self.assertIn('Fable resets left: 3', text)
            self.assertIn('Fable limit (7d_oi): net observed pace: not enough data', text)
            self.assertIn('ASSUMPTION:', text)
            self.assertNotIn('inferred', text)
            self.assertIn(cu.NON_FABLE + ': 18.5%', text)
            self.assertNotIn('20.5%', text)
            log = cu.tsv(parsed, self.now)
            self.assertEqual(log.split('\t')[8:10], ['0.41', str(int(row['r7'])) + '\n'])
            self.assertNotIn(SECRET, text + log + repr(parsed))

    def test_resets_defaults_and_manual_inventory_unchanged(self):
        self.assertEqual(cu.reset_config(), {'fable_resets_left': 3, 'fable_share_of_total': 0.5})
        self.assertFalse(self.resets.exists())
        self.configure_resets(2)
        before = self.resets.read_text()
        row = self.row(utilization=0.39, uf=0.8)
        lines = self.warning(row, self.fable_history(row))
        self.assertIn(cu.BANKED_NOTICE, lines[0])
        self.assertEqual(self.resets.read_text(), before)

    def test_invalid_resets_config_is_conservative_and_does_not_leak(self):
        self.resets.parent.mkdir(parents=True)
        for text in ('{', '[]', '{"fable_resets_left": -1}',
                     '{"fable_resets_left": true}', '{"fable_resets_left": "' + SECRET + '"}'):
            self.resets.write_text(text)
            output = io.StringIO()
            with redirect_stderr(output):
                self.assertEqual(cu.reset_config(), {'fable_resets_left': 0, 'fable_share_of_total': 0.5})
            self.assertNotIn(SECRET, output.getvalue())

    def test_fable_runout_with_resets_is_thread_only_information(self):
        row = self.row(utilization=0.39, uf=0.8)
        lines = self.warning(row, self.fable_history(row))
        self.assertEqual(len(lines), 1)
        self.assertIn('Fable limit runs out Mon 18:30 ET, before its Tue 06:00 ET reset', lines[0])
        self.assertIn(cu.BANKED_NOTICE, lines[0])
        self.assertIn('ASSUMPTION: ' + cu.RESET_ASSUMPTION, lines[0])
        self.assertEqual(self.threads, ['For you: ' + lines[0]])
        self.assertEqual(self.desktops, [])
        text = cu.render([row], self.fable_history(row), self.now)
        self.assertIn('Fable limit (7d_oi): net observed pace; at the last 2 h pace: reaches 100% Mon 18:30 ET', text)
        self.assertIn(cu.BANKED_NOTICE, text)

    def test_fable_with_no_resets_is_alarm(self):
        self.configure_resets(0)
        row = self.row(utilization=0.39, uf=0.8)
        lines = self.warning(row, self.fable_history(row))
        self.assertEqual(len(lines), 1)
        self.assertIn('no banked Fable resets remain', lines[0])
        self.assertEqual(self.desktops, lines)

    def test_all_models_alarm_even_with_banked_resets(self):
        row = self.row(utilization=0.8, uf=0.41)
        lines = self.warning(row, self.fable_history(row, old_all=0.6, old_fable=0.41))
        self.assertEqual(len(lines), 2)
        self.assertIn('confirmed: 7d is Current week (all models)', lines[0])
        self.assertIn('assumed: ' + cu.RESET_ASSUMPTION, lines[0])
        self.assertNotIn('covered by resets', lines[0])
        self.assertEqual(self.desktops, lines)

    def test_three_meters_warn_with_two_desktops_and_each_line_prefixed(self):
        row = self.row(utilization=0.8, uf=0.8)
        lines = self.warning(row, self.fable_history(row, old_all=0.6))
        self.assertEqual(len(lines), 3)
        self.assertEqual(self.threads[0].splitlines(), ['For you: ' + line for line in lines])
        self.assertEqual(self.desktops, [lines[0], lines[2]])
        self.assertIn('confirmed:', lines[0])
        self.assertIn(cu.BANKED_NOTICE, lines[1])

    def test_fable_specific_reset_limits_warning_independently(self):
        row = self.row(utilization=0.39, uf=0.8, rf=self.now.timestamp() + 1800)
        history = self.fable_history(row)
        self.assertEqual(self.warning(row, history), [])  # Run-out exactly at reset.
        row['rf'] -= 60
        history = self.fable_history(row)
        self.assertEqual(self.warning(row, history), [])  # Run-out after reset.
        row['rf'] += 120
        self.assertTrue(self.warning(row, self.fable_history(row)))

    def test_fable_change_or_drop_restarts_shared_and_fable_spans(self):
        row = self.row(utilization=0.8, uf=0.8, rf=self.now.timestamp() + 7200)
        for changes in ({'uf': 0.95}, {'rf': row['rf'] - 3600}):
            history = [dict(self.fable_history(row, old_all=0.6)[0], **changes)]
            self.assertEqual(cu.project(row, 'fable', history, self.now)['status'], 'collecting post-reset pace')
            self.assertEqual(cu.project(row, '7d', history, self.now)['status'], 'collecting post-reset pace')
        # A banked reset may lower the all-models meter; each span tracks its own drop.
        history = self.fable_history(row, old_fable=0.6, old_all=0.9)
        self.assertEqual(cu.project(row, '7d', history, self.now)['status'], 'collecting post-reset pace')
        self.assertEqual(cu.project(row, 'fable', history, self.now)['status'], 'burn')

    def test_fable_uses_higher_short_or_long_rate_and_insufficient_history(self):
        row = self.row(utilization=0.39, uf=0.8)
        history = self.fable_history(row, old_fable=0.4, minutes=120)
        history += self.fable_history(row, old_fable=0.6, minutes=30)
        self.assertEqual(cu.project(row, 'fable', history, self.now)['minutes'], 30)
        history[0]['uf'] = 0
        history[1]['uf'] = 0.75
        self.assertEqual(cu.project(row, 'fable', history, self.now)['minutes'], 120)
        for history in ([], self.fable_history(row, minutes=14)):
            self.assertEqual(self.warning(row, history), [])
            self.assertEqual(cu.project(row, 'fable', history, self.now)['status'], 'not enough data')

    def test_fable_six_hour_limit_and_earlier_projection_bypass(self):
        row = self.row(utilization=0.39, uf=0.8, hours_to_reset=24)
        self.assertTrue(self.warning(row, self.fable_history(row)))
        later = self.now + timedelta(hours=1)
        self.assertEqual(self.warning(row, self.fable_history(row, now=later), later), [])
        later = self.now + timedelta(hours=6)
        self.assertTrue(self.warning(row, self.fable_history(row, now=later), later))
        self.assertEqual(self.desktops, [])
        # Force a much earlier projection within the cooldown.
        self.state.unlink()
        slow = self.fable_history(row, old_fable=0.75, minutes=120)
        self.assertTrue(self.warning(row, slow))  # Eight hours to run-out.
        later = self.now + timedelta(minutes=10)
        self.assertTrue(self.warning(row, self.fable_history(row, now=later), later))

    def test_information_cooldown_survives_quiet_period(self):
        row = self.row(utilization=0.39, uf=0.8)
        self.assertTrue(self.warning(row, self.fable_history(row)))
        self.assertEqual(self.warning(row, self.fable_history(row, old_fable=0.8)), [])
        self.assertEqual(self.warning(row, self.fable_history(row)), [])
        self.assertEqual(len(self.threads), 1)
        self.assertEqual(self.desktops, [])

    def test_information_cooldown_survives_exhausted_or_inactive_observation(self):
        row = self.row(utilization=0.39, uf=0.8)
        self.warning(row, self.fable_history(row))
        self.assertEqual(self.warning(dict(row, uf=1), self.fable_history(row)), [])
        self.assertEqual(self.warning(dict(row, status='error'), self.fable_history(row)), [])
        self.assertEqual(self.warning(row, self.fable_history(row)), [])
        self.assertEqual(len(self.threads), 1)

    def test_information_failed_thread_send_remains_retryable(self):
        row = self.row(utilization=0.39, uf=0.8)
        with redirect_stdout(self.output):
            result = cu.warn([row], self.fable_history(row), self.now, state_path=self.state,
                             config_path=self.config, resets_path=self.resets,
                             thread_sender=lambda _: False,
                             desktop_sender=lambda _: self.fail('information cannot notify desktop'))
        self.assertTrue(result)
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.assertTrue(self.warning(row, self.fable_history(row)))

    def test_inventory_change_escalates_immediately_and_alarm_recovers(self):
        row = self.row(utilization=0.39, uf=0.8)
        history = self.fable_history(row)
        self.warning(row, history)
        self.configure_resets(0)
        lines = self.warning(row, history)
        self.assertEqual(self.desktops, lines)
        self.assertEqual(len(lines), 1)
        steady = self.fable_history(row, old_fable=0.8)
        self.assertEqual(self.warning(row, steady), ['Claude fake@example.test Fable limit: back on pace.'])
        self.assertEqual(self.warning(row, steady), [])

    def test_fable_log_migration_by_name_and_roundtrip(self):
        # The previous eight columns remain in their original positions.
        old_header = cu.HEADER.split('\t')[:8]
        old_row = ['2025-11-10T18:00:00', 'fake@example.test', 'active', '0.1', '0.39',
                   '1763204400', 'claude', '1762819200']
        cu.LOG.write_text('\t'.join(old_header) + '\n' + '\t'.join(old_row) + '\n')
        cu.prepare_log()
        self.assertEqual(cu.LOG.read_text().splitlines()[1].split('\t'), old_row + ['', ''])
        row = self.row(utilization=0.39, uf=0.41, rf=self.now.timestamp() + 7200)
        with cu.LOG.open('a') as out:
            out.write(cu.tsv([row], self.now))
        parsed = cu.read_log(cu.LOG)
        self.assertIsNone(parsed[0]['uf'])
        self.assertEqual((parsed[1]['uf'], parsed[1]['rf']), (0.41, row['rf']))
        # A reordered current log also migrates by field name, keeping both Fable values.
        columns = cu.LOG.read_text().splitlines()
        columns = ['\t'.join(reversed(line.split('\t'))) for line in columns]
        cu.LOG.write_text('\n'.join(columns) + '\n')
        cu.prepare_log()
        self.assertEqual(cu.read_log(cu.LOG), parsed)
        self.assertEqual(cu.LOG.stat().st_mode & 0o777, 0o600)

    def test_missing_fable_meter_does_not_change_all_models_alarm(self):
        row = self.row()
        text = cu.render([row], self.history(row), self.now)
        self.assertIn('Fable limit (7d_oi) -', text)
        self.assertIn('Fable limit (7d_oi): net observed pace: no data', text)
        lines = self.warning(row)
        self.assertEqual(len(lines), 1)
        self.assertIn('confirmed:', lines[0])

    def test_non_fable_supplied_readings_and_exact_label(self):
        self.assertEqual(cu.NON_FABLE, "non-Fable (estimate; Fable allowance is half the weekly limit, "
                         "confirmed by Anthropic's page; meters assumed to be counted alike)")
        reset = datetime(2025, 11, 15, 6, tzinfo=cu.ET).timestamp()
        for hour, minute, all_models, fable, expected in (
                (20, 41, 0.49, 0.55, 0.215), (19, 31, 0.42, 0.45, 0.195)):
            self.now = datetime(2025, 11, 10, hour, minute, tzinfo=cu.ET)
            row = self.row(utilization=all_models, uf=fable, r7=reset)
            self.assertAlmostEqual(cu.meter_value(row, 'non-fable', cu.DEFAULT_RESETS), expected)
            text = cu.render([row], now=self.now)
            self.assertIn(f'all models (7d) {all_models * 100:.0f}% | Fable limit (7d_oi) {fable * 100:.0f}%', text)
            self.assertIn(cu.NON_FABLE + f': {expected * 100:.1f}% of the week used vs', text)
            self.assertIn('UNDER pace', text)
            self.assertIn('Banked Fable resets do not reset non-Fable usage.', text)
            self.assertNotIn('Non-Fable quota/pace: unavailable', text)
            self.assertEqual(self.warning(row, []), [])
            if hour == 20:
                self.assertIn('37.3% of the week elapsed: UNDER pace (N/E 0.58)', text)

    def test_non_fable_over_pace_alarm_and_strict_threshold(self):
        row = self.row(utilization=0.6, uf=0.2, hours_to_reset=117.6)
        lines = self.warning(row, [])
        self.assertEqual(lines, [f'Claude fake@example.test {cu.NON_FABLE}: '
                                 '50.0% used, 30.0% of the week elapsed, over pace.'])
        self.assertEqual(self.desktops, lines)
        self.assertEqual(self.threads, ['For you: ' + lines[0]])
        self.state.unlink()
        threshold = self.row(utilization=0.675, uf=0.2, hours_to_reset=84)
        self.assertEqual(self.warning(threshold, []), [])  # Exactly 15% over: no alarm.
        threshold['u7'] += 0.0001
        self.assertIn('over pace', self.warning(threshold, [])[0])

    def test_non_fable_runout_no_reset_credit_and_shared_guard(self):
        row = self.row(utilization=0.8, uf=0.4)
        history = self.fable_history(row, old_all=0.6, old_fable=0.4)
        for left in (0, 3):
            self.configure_resets(left)
            if self.state.exists():
                self.state.unlink()
            lines = self.warning(row, history)
            shared = [line for line in lines if '7-day all-models window' in line]
            derived = [line for line in lines if cu.NON_FABLE in line]
            self.assertEqual(len(shared), 1)
            self.assertEqual(len(derived), 1)
            self.assertTrue(shared[0].endswith(cu.SHARED_GUARD))
            self.assertIn(cu.NON_FABLE + ': at the last 120-minute pace it runs out Mon 19:00 ET, '
                          'before its Tue 06:00 ET reset; the Fable resets do not cover this', derived[0])
            self.assertIn(shared[0], self.desktops)
            self.assertIn(derived[0], self.desktops)
            self.assertNotIn('banked reset', derived[0])

    def test_non_fable_runout_hourly_limit_and_genuine_recovery(self):
        row = self.row(utilization=0.8, uf=0.4)
        history = self.fable_history(row, old_all=0.6, old_fable=0.4)
        self.assertTrue(any(cu.NON_FABLE in line for line in self.warning(row, history)))
        self.assertEqual(self.warning(row, history, self.now + timedelta(minutes=10)), [])
        later = self.now + timedelta(hours=1)
        history = self.fable_history(row, now=later, old_all=0.6, old_fable=0.4)
        self.assertTrue(any(cu.NON_FABLE in line for line in self.warning(row, history, later)))
        steady = self.fable_history(row, now=later, old_all=0.8, old_fable=0.4)
        lines = self.warning(row, steady, later)
        self.assertIn(f'Claude fake@example.test {cu.NON_FABLE} projection: back on pace.', lines)
        self.assertEqual(self.warning(row, steady, later), [])
        self.assertIn(cu.SHARED_GUARD, lines[0])

    def test_non_fable_earlier_runout_bypasses_hourly_limit(self):
        row = self.row(utilization=0.8, uf=0.4)
        slow = self.fable_history(row, old_all=0.7, old_fable=0.4, minutes=120)
        fast = self.fable_history(row, old_all=0.6, old_fable=0.4)
        self.assertTrue(any(cu.NON_FABLE in line for line in self.warning(row, slow)))
        self.assertTrue(any(cu.NON_FABLE in line for line in self.warning(row, fast)))

    def test_non_fable_six_hour_limit_survives_quiet_and_reset_boundary(self):
        row = self.row(utilization=0.6, uf=0.2, hours_to_reset=117.6)
        self.assertTrue(self.warning(row, []))
        self.assertEqual(self.warning(row, [], self.now + timedelta(hours=1)), [])
        low = dict(row, u7=0.2)
        self.assertIn('back on pace', self.warning(low, [], self.now + timedelta(hours=2))[0])
        self.assertEqual(self.warning(low, [], self.now + timedelta(hours=2)), [])
        self.assertEqual(self.warning(row, [], self.now + timedelta(hours=3)), [])
        self.resets.parent.mkdir(parents=True, exist_ok=True)
        self.resets.write_text(json.dumps({'fable_reset_at': (self.now + timedelta(hours=4)).timestamp()}))
        self.assertEqual(self.warning(row, [], self.now + timedelta(hours=4)), [])
        self.assertEqual(len(self.warning(row, [], self.now + timedelta(hours=6))), 1)

    def test_non_fable_missing_fable_or_reset_stays_unknown(self):
        row = self.row(utilization=0.49)
        text = cu.render([row], now=self.now)
        self.assertIn('non-Fable: not derivable (Fable meter missing)', text)
        self.assertIsNone(cu.meter_value(row, 'non-fable', cu.DEFAULT_RESETS))
        self.assertEqual(self.warning(row, []), [])
        row = self.row(utilization=0.49, uf=0.55, rf=None)
        history = self.fable_history(row, old_all=0.4, old_fable=0.4)
        for window in ('fable', 'non-fable'):
            self.assertIn('reset-aware pace unknown', cu.project(row, window, history, self.now)['status'])
        lines = self.warning(row, history)
        self.assertTrue(any('7-day all-models window' in line for line in lines))
        self.assertFalse(any(cu.NON_FABLE in line or line.startswith('Fable ') for line in lines))
        text = cu.render([row], history, self.now)
        self.assertNotIn('assuming the weekly reset', text)
        row['r7'] = None
        self.assertIn('reset-aware pace unknown', cu.render([row], history, self.now))

    def test_non_fable_share_configuration_and_no_writes(self):
        self.resets.parent.mkdir(parents=True)
        config = '{"fable_resets_left": 3, "fable_share_of_total": 0.25}'
        self.resets.write_text(config)
        row = self.row(utilization=0.8, uf=0.4)
        history = self.fable_history(row, old_all=0.6, old_fable=0.2)
        self.assertAlmostEqual(cu.meter_value(row, 'non-fable', cu.reset_config()), 0.7)
        self.assertAlmostEqual(cu.project(row, 'non-fable', history, self.now)['rate'], 0.3)
        text = cu.render([row], history, self.now)
        self.assertIn(cu.NON_FABLE + ': 70.0%', text)
        self.assertIn('configured share 25% overrides 50%', text)
        self.assertTrue(any(cu.NON_FABLE in line for line in self.warning(row, history)))
        self.assertEqual(self.resets.read_text(), config)
        for share in (-0.1, 1.1, True, SECRET, float('nan'), float('inf')):
            self.resets.write_text(json.dumps({'fable_share_of_total': share}))
            output = io.StringIO()
            with redirect_stderr(output):
                self.assertEqual(cu.reset_config(), dict(cu.DEFAULT_RESETS, fable_resets_left=0))
            self.assertNotIn(SECRET, output.getvalue())

    def test_accounting_boundaries_collect_and_restore_all_affected_spans(self):
        row = self.row(utilization=0.8, uf=0.4)
        for changes, affected in (
                ({'u7': 0.9}, ('7d', 'non-fable')),
                ({'r7': row['r7'] - 3600}, ('7d', 'non-fable')),
                ({'uf': 0.8}, ('7d', 'fable', 'non-fable')),
                ({'rf': row['rf'] - 3600}, ('7d', 'fable', 'non-fable')),
                ({'r7': self.now.timestamp() - 600}, ('7d', 'non-fable')),
                ({'rf': self.now.timestamp() - 600}, ('7d', 'fable', 'non-fable'))):
            with self.subTest(changes=changes):
                old = dict(self.fable_history(row, old_all=0.6, old_fable=0.3)[0], **changes)
                history = [old]
                for window in affected:
                    p = cu.project(row, window, history, self.now)
                    self.assertEqual(p['status'], 'collecting post-reset pace')
                    self.assertEqual(p['boundary_cause'], 'unknown cause')
                    self.assertNotIn('rate', p)
                history.append(dict(row, time=self.now.timestamp()))
                later = self.now + timedelta(minutes=15)
                current = dict(row, u7=0.85, uf=0.42)
                for window in affected:
                    p = cu.project(current, window, history, later)
                    self.assertEqual(p['status'], 'burn')
                    self.assertGreater(p['rate'], 0)
                    self.assertEqual(p['boundary_at'], self.now.timestamp())

    def test_fable_drop_with_shared_rising_clears_alarms_silently(self):
        row = self.row(utilization=0.8, uf=0.4)
        history = self.fable_history(row, old_all=0.6, old_fable=0.3)
        self.assertTrue(self.warning(row, history))
        history.append(dict(row, time=self.now.timestamp()))
        later = self.now + timedelta(minutes=10)
        current = dict(row, u7=0.81, uf=0.1)
        self.assertEqual(self.warning(current, history, later), [])
        self.assertEqual(json.loads(self.state.read_text()), {})
        text = cu.render([current], history, later)
        self.assertEqual(text.count('collecting post-reset pace'), 3)
        self.assertIn('unknown cause', text)
        self.assertNotIn('back on pace', text)

    def test_explicit_reset_marker_invalidates_all_three_and_keeps_log_schema(self):
        row = self.row(utilization=0.8, uf=0.4)
        history = self.fable_history(row, old_all=0.6, old_fable=0.3)
        self.assertTrue(self.warning(row, history))
        marker = self.now.isoformat()
        self.resets.parent.mkdir(parents=True, exist_ok=True)
        config = json.dumps({'fable_resets_left': 2, 'fable_reset_at': marker})
        self.resets.write_text(config)
        self.assertEqual(self.warning(row, history), [])
        self.assertEqual(json.loads(self.state.read_text()), {})
        for window in ('7d', 'fable', 'non-fable'):
            p = cu.project(row, window, history, self.now)
            self.assertEqual(p['status'], 'collecting post-reset pace')
            self.assertEqual(p['boundary_at'], self.now.timestamp())
        history.append(dict(row, time=self.now.timestamp()))
        current = dict(row, u7=0.85, uf=0.42)
        later = self.now + timedelta(minutes=15)
        self.assertTrue(self.warning(current, history, later))
        self.assertNotIn('back on pace', self.threads[-1])
        self.assertEqual(self.resets.read_text(), config)
        state = json.loads(self.state.read_text())
        self.assertTrue(all(entry['boundary_cause'] == 'unknown cause' for entry in state.values()))
        cu.prepare_log()
        with cu.LOG.open('a') as out:
            out.write(cu.tsv([row], self.now))
        self.assertEqual(len(cu.HEADER.strip().split('\t')), 10)
        self.assertAlmostEqual(cu.meter_value(cu.read_log(cu.LOG)[0], 'non-fable', cu.DEFAULT_RESETS), 0.6)

    def test_no_forecast_eta_at_or_beyond_known_reset(self):
        row = self.row(utilization=0.8, uf=0.4, hours_to_reset=1)
        history = self.fable_history(row, old_all=0.6, old_fable=0.4)
        for reset in (row['r7'], row['r7'] - 60):
            current = dict(row, r7=reset, rf=reset)
            old = [dict(history[0], r7=reset, rf=reset)]
            p = cu.project(current, 'non-fable', old, self.now)
            self.assertEqual(p['status'], 'no exhaustion before next reset')
            self.assertNotIn('at', p)
            self.assertFalse(any(cu.NON_FABLE in line for line in self.warning(current, old)))

    def test_non_fable_projection_higher_rate_and_isolated_history(self):
        row = self.row(utilization=0.8, uf=0.4)
        history = self.fable_history(row, old_all=0.4, old_fable=0.4, minutes=120)
        history += self.fable_history(row, old_all=0.6, old_fable=0.4, minutes=30)
        self.assertEqual(cu.project(row, 'non-fable', history, self.now)['minutes'], 30)
        history[0]['u7'], history[1]['u7'] = 0.2, 0.75
        self.assertEqual(cu.project(row, 'non-fable', history, self.now)['minutes'], 120)
        for changes in ({'email': 'other@example.test'}, {'provider': 'codex'}, {'uf': None}):
            old = dict(history[0], **changes)
            self.assertEqual(cu.project(row, 'non-fable', [old], self.now)['status'], 'not enough data')

    def test_non_fable_every_percentage_projection_and_alarm_uses_label(self):
        row = self.row(utilization=0.8, uf=0.2, hours_to_reset=117.6)
        history = self.fable_history(row, old_all=0.6, old_fable=0.2)
        text = cu.render([row], history, self.now)
        lines = self.warning(row, history)
        all_lines = text.splitlines() + lines + self.threads[0].splitlines() + self.desktops
        derived = [line for line in all_lines if 'non-Fable' in line and ('%' in line or 'runs out' in line or 'projection:' in line)]
        self.assertGreaterEqual(len(derived), 8)
        self.assertTrue(all(cu.NON_FABLE in line or '7-day all-models window' in line for line in derived))
        self.assertNotIn(SECRET, '\n'.join(all_lines) + self.state.read_text())

    def test_reset_marker_validation_and_future_marker(self):
        self.assertIsNone(cu.reset_marker(True))
        self.assertIsNone(cu.reset_marker('2025-11-10T18:00:00'))
        self.assertEqual(cu.reset_marker(self.now.isoformat()), self.now.timestamp())
        row = self.row(utilization=0.8, uf=0.4)
        future = dict(cu.DEFAULT_RESETS, fable_reset_at=self.now.timestamp() + 600)
        p = cu.project(row, '7d', self.fable_history(row, old_all=0.6, old_fable=0.4), self.now, future)
        self.assertEqual(p['status'], 'burn')
        self.assertNotIn('boundary_at', p)

    def test_non_fable_clamps_and_zero_elapsed_ratio(self):
        row = self.row(utilization=0.1, uf=0.8)
        self.assertEqual(cu.meter_value(row, 'non-fable', cu.DEFAULT_RESETS), 0)
        self.assertEqual(cu.week_elapsed(self.now.timestamp() + cu.WEEK * 2, self.now), 0)
        self.assertEqual(cu.week_elapsed(self.now.timestamp() - 1, self.now), 1)
        row.update(u7=0.4, uf=0, r7=self.now.timestamp() + cu.WEEK)
        self.assertIn('N/E infinite', cu.render([row], now=self.now))
        self.assertTrue(self.warning(row, []))

    def test_non_fable_deadline_cannot_cross_either_input_reset(self):
        row = self.row(utilization=0.8, uf=0.4, rf=self.now.timestamp() + 1800)
        history = self.fable_history(row, old_all=0.6, old_fable=0.4)
        p = cu.project(row, 'non-fable', history, self.now)
        self.assertEqual(p['status'], 'no exhaustion before next reset')
        self.assertNotIn('at', p)
        self.assertFalse(any(cu.NON_FABLE in line for line in self.warning(row, history)))

    def test_non_fable_failed_delivery_remains_retryable(self):
        row = self.row(utilization=0.6, uf=0.2, hours_to_reset=117.6)
        with redirect_stdout(self.output):
            lines = cu.warn([row], [], self.now, thread_sender=lambda _: False,
                            desktop_sender=lambda _: False)
        self.assertTrue(lines)
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.assertTrue(self.warning(row, []))

    def test_non_fable_net_decrease_cannot_create_negative_burn(self):
        row = self.row(utilization=0.61, uf=0.8)
        history = self.fable_history(row, old_all=0.6, old_fable=0.4)
        p = cu.project(row, 'non-fable', history, self.now)
        self.assertEqual(p['status'], 'collecting post-reset pace')
        self.assertNotIn('rate', p)


if __name__ == '__main__':
    unittest.main()


class ModalTests(unittest.TestCase):
    SUMMARY = {'metered_cost': '11.67485571', 'billed_cost': '0E-8',
               'adjustments': {'credits': '-11.34000000', 'free_storage': '-0.33'},
               'metered_cost_breakdown': {}}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = datetime(2025, 11, 10, 23, 40, tzinfo=cu.ET)
        self.sent = []

    def check(self, summary, state=None):
        if state is not None:
            (self.root / 'state.json').write_text(json.dumps(state))
        return cu.modal_check(
            self.now, fetcher=lambda: cu.parse_modal(summary), state_path=self.root / 'state.json',
            log_path=self.root / 'modal.tsv', thread_sender=lambda t: self.sent.append(t) or True,
            desktop_sender=lambda t: True)

    def test_parse_and_render_label_what_is_unknown(self):
        m = cu.parse_modal(self.SUMMARY)
        self.assertAlmostEqual(m['metered'], 11.67485571)
        self.assertAlmostEqual(m['credits'], 11.34)
        self.assertEqual(m['billed'], 0.0)
        text = cu.render_modal(m)
        self.assertIn('no remaining-credit balance', text)
        self.assertIn('billed $0.00', text)

    def test_first_observation_is_quiet_and_logged(self):
        self.assertEqual(self.check(self.SUMMARY), [])
        self.assertEqual(self.sent, [])
        self.assertEqual((self.root / 'modal.tsv').read_text().count('\n'), 1)

    def test_jump_warns_once_per_check(self):
        self.check(self.SUMMARY)
        jumped = dict(self.SUMMARY, metered_cost='14.50')
        lines = self.check(jumped)
        self.assertEqual(len(lines), 1)
        self.assertIn('jumped $2.83', lines[0])
        self.assertTrue(self.sent[0].startswith('For you: Modal spend jumped'))
        self.assertEqual(self.check(jumped), [])  # no new jump

    def test_billed_money_is_an_alarm_and_not_repeated(self):
        self.check(self.SUMMARY)
        billed = dict(self.SUMMARY, billed_cost='1.25')
        lines = self.check(billed)
        self.assertTrue(any('BILLING real money: $1.25' in line for line in lines))
        self.assertEqual(self.check(billed), [])
        self.assertEqual(self.check(dict(self.SUMMARY, billed_cost='1.50')), [])  # under +$0.50 since the warning
        self.assertTrue(self.check(dict(self.SUMMARY, billed_cost='1.80')))  # $0.55 more: warn again

    def test_small_first_billing_still_alarms(self):
        self.check(self.SUMMARY)
        lines = self.check(dict(self.SUMMARY, billed_cost='0.03'))
        self.assertTrue(any('BILLING real money: $0.03' in line for line in lines))
        self.assertEqual(self.check(dict(self.SUMMARY, billed_cost='0.20')), [])  # repeats need +$0.50

    def test_failed_delivery_keeps_warnings_pending(self):
        self.check(self.SUMMARY)
        jumped = dict(self.SUMMARY, metered_cost='14.50', billed_cost='1.00')
        failed = cu.modal_check(self.now, fetcher=lambda: cu.parse_modal(jumped),
                                state_path=self.root / 'state.json', log_path=self.root / 'modal.tsv',
                                thread_sender=lambda t: False, desktop_sender=lambda t: True)
        self.assertEqual(len(failed), 2)
        again = self.check(jumped)  # delivery works now: both warnings are sent again
        self.assertEqual(len(again), 2)
        self.assertEqual(self.check(jumped), [])

    def test_failure_is_silent(self):
        def broken():
            raise OSError('no modal')
        self.assertEqual(cu.modal_check(self.now, fetcher=broken, state_path=self.root / 's.json',
                                         log_path=self.root / 'l.tsv'), [])
        self.assertFalse((self.root / 'l.tsv').exists())

    def test_cli_failure_and_bad_json(self):
        class R:
            returncode = 1
            stdout = ''
        with self.assertRaises(OSError):
            cu.fetch_modal(run=lambda *a, **k: R())
        R.returncode, R.stdout = 0, 'not json'
        with self.assertRaises(ValueError):
            cu.fetch_modal(run=lambda *a, **k: R())

    def test_no_token_in_command(self):
        seen = []
        class R:
            returncode, stdout = 0, json.dumps(ModalTests.SUMMARY)
        cu.fetch_modal(run=lambda cmd, **k: seen.append(cmd) or R())
        self.assertEqual(seen[0][1:], ['billing', 'summary', '--json'])
