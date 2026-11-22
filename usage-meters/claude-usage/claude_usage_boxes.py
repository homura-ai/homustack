"""Category usage policy. All provider I/O belongs to the existing meter reader.

This module and the passive sampler never make provider/network requests.
"""
from datetime import datetime
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tomllib
from decimal import Decimal
from zoneinfo import ZoneInfo
import re

CATEGORIES = ('fable', 'non-fable', 'codex')
LABELS = {'fable': 'Fable', 'non-fable': 'Claude non-Fable', 'codex': 'Codex'}
ROLLING = ((1, .03), (3, .06), (9, .12), (24, .25))
# The user's notification levels are fixed here, never read from configuration.
# (window minutes, immutable notifications, default stop).
RATE_LADDER = ((5, (2., 2.5, 2.75), 3.),
               (15, (1.5, 1.75, 1.875), 2.),
               (30, (1.25, 1.35, 1.45), 1.5),
               (60, (1.1, 1.2, 1.25), 1.3))
ALERT_LOG = Path(os.environ.get('CLAUDE_USAGE_ALERT_LOG') or Path.home() / '.local/state/claude-usage/usage-alerts.log')


def stamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('dates need a UTC offset')
    return dt.timestamp()


def finite(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('amount must be a finite number')
    if value < 0 or (positive and value == 0):
        raise ValueError('amount must be positive')
    return float(value)


def load(path):
    data = tomllib.loads(path.read_text())
    if data.get('version') != 2 or not isinstance(data.get('approved'), bool):
        raise ValueError('expected version 2 and approved = true/false')
    sources = data.get('sources', [])
    if not sources:
        raise ValueError('no category sources')
    seen = set()
    for source in sources:
        identity = source['provider'], source['email']
        if identity[0] not in ('claude', 'codex') or identity in seen or ':' in identity[1]:
            raise ValueError('invalid or duplicate account')
        seen.add(identity)
        finite(source['weight'], True)
        for key in ('banked', 'fable_banked', 'weekly_banked', 'weekly_per_5h'):
            if key in source:
                finite(source[key])
        if 'weekly_banked' in source and (identity[0] != 'claude' or type(source['weekly_banked']) is not int):
            raise ValueError('weekly reset inventory must be a Claude integer count')
    for run in data.get('runs', []):
        run['start'], run['end'] = stamp(run['start']), stamp(run['end'])
        if run['end'] <= run['start'] or not isinstance(run.get('approved'), bool):
            raise ValueError('invalid run period or approval')
        if run.get('remaining', False):
            if run.get('allow'):
                raise ValueError('choose remaining capacity or explicit amounts')
        elif set(run.get('allow', {})) != set(CATEGORIES):
            raise ValueError('each run needs all three category amounts')
        else:
            for amount in run['allow'].values():
                finite(amount, True)
    runs = sorted(data.get('runs', []), key=lambda x: x['start'])
    if any(a['end'] > b['start'] for a, b in zip(runs, runs[1:])):
        raise ValueError('run periods overlap')
    for category in CATEGORIES:
        finite(data['rate_points_per_million'][category], True)
    prep = data.get('preparation')
    if prep:
        for key in ('start', 'end', 'final_end'):
            prep[key] = stamp(prep[key])
        if not prep['start'] < prep['end'] < prep['final_end']:
            raise ValueError('invalid preparation/final dates')
        finite(prep['active_hours'], True)
        if not isinstance(prep.get('rate_stops_dry_run', False), bool):
            raise ValueError('rate_stops_dry_run must be true or false')
        if not isinstance(prep.get('split_automock', False), bool):
            raise ValueError('split_automock must be true or false')
        if set(prep) - {'start', 'end', 'final_end', 'active_hours', 'rate_stops_dry_run', 'stops', 'split_automock'}:
            raise ValueError('unknown preparation option; notify levels are fixed in code')
        if set(prep.get('stops', {})) - {'5', '15', '30', '60'}:
            raise ValueError('unknown rate stop window')
        for stop in prep.get('stops', {}).values():
            finite(stop, True)
            if stop > 100:
                raise ValueError('rate stop maximum is 100 times planned rate')
        for run in data.get('runs', []):
            finite(run.get('scale', 1), True)
    return data


def capacities(policy):
    out = dict.fromkeys(CATEGORIES, 0.)
    for source in policy['sources']:
        w = source['weight']
        if source['provider'] == 'claude':
            out['fable'] += 100 * w * (1 + source.get('fable_banked', 0) + source.get('weekly_banked', 0))
            out['non-fable'] += 100 * w * (1 + source.get('weekly_banked', 0))
        else:
            out['codex'] += 100 * w * (1 + source.get('banked', 0))
    return out


def gauges(row):
    """Own-half Claude points; Codex weekly points. Missing is never zero."""
    a, f = row.get('u7'), row.get('uf')
    if row.get('provider', 'claude') == 'codex':
        return {'codex': None if a is None else float(a) * 100}
    non_fable = (float(row['nf_points']) if row.get('nf_points') is not None else
                 None if a is None or f is None else max(0., 200 * float(a) - 100 * float(f)))
    return {'fable': None if f is None else float(f) * 100, 'non-fable': non_fable}


def reset_changed(old, new, key):
    a, b = old.get(key), new.get(key)
    # Cached Codex reset timestamps jitter by one second. That is not a reset.
    return a is not None and b is not None and abs(float(a) - float(b)) > 60


def empty_ledger():
    return dict(sources={}, events=[], gaps=[], starts={}, forgiven={}, runs={}, meter_intervals=[])


def observe(ledger, rows, now, policy):
    """Append conservative meter increments, retaining a high-water mark per epoch.

    A Fable-only reset makes cross-meter subtraction ambiguous. Mark non-Fable
    incomplete rather than manufacture usage. Human release starts fresh evidence.
    Resets preserve already recorded spend; an unobserved tail is a gap.
    """
    # Old v2 histories have no response-time provenance. They stay available
    # for total boxes, but cannot prove a short rate interval.
    if ledger.get('meter_interval_version') != 2:
        ledger['meter_intervals'] = []
        ledger['meter_interval_version'] = 2
    configured = {(s['provider'], s['email']): s for s in policy['sources']}
    for row in rows:
        identity = row.get('provider', 'claude'), row['email']
        source = configured.get(identity)
        if source is None:
            continue
        key = ':'.join(identity)
        previous = ledger['sources'].get(key)
        if previous and now <= previous['time']:
            continue
        row = dict(row)
        row.pop('limit', None)  # category status reads fresh rows; the ledger keeps meters only
        # A missing model-specific Fable snapshot must not hide account-wide
        # non-Fable consumption. Hold HISTORICAL Fable spend constant, rather
        # than setting its cumulative gauge to zero and charging it again.
        previous_row = previous['row'] if previous else {}
        if row.get('fable_excluded') is True:
            row['fable_excluded_since'] = (previous_row.get('fable_excluded_since', now)
                if previous and previous_row.get('fable_excluded') is True and now - previous['time'] <= 120 else now)
        else:
            row['fable_excluded_since'] = None
        row['fable_epoch_at'] = previous_row.get('fable_epoch_at')
        account_at = row.get('observed_at')
        paired_at = row.get('nf_observed_at')
        fallback = (identity[0] == 'claude' and row.get('u7') is not None
                    and (row.get('fable_excluded') or row.get('fable_no_traffic'))
                    and (row.get('nf_points') is None
                         or (paired_at is not None and account_at is not None and paired_at < account_at)))
        if fallback:
            reset = (reset_changed(previous_row, row, 'r7') or
                     (previous_row.get('u7') is not None and
                      float(row['u7']) < float(previous_row['u7']) - .0201))
            if reset:
                row['fable_epoch_at'] = account_at if account_at is not None else now
            fable_at = row.get('fable_observed_at')
            previous_fable_at = previous_row.get('fable_observed_at')
            real_fable = (row.get('uf') is not None and not row.get('nf_estimated')
                          and fable_at is not None
                          and (previous_fable_at is None or fable_at >= previous_fable_at)
                          and (row['fable_epoch_at'] is None or fable_at >= row['fable_epoch_at']))
            if not real_fable:
                # Synthetic cumulative Fable belongs to its historical epoch.
                # A changed reset timestamp alone cannot recharge that history.
                row['uf'] = 0. if reset else float(previous_row.get('uf') or 0.)
                row['fable_observed_at'] = previous_fable_at
                if not reset:
                    row['rf'] = previous_row.get('rf')
            # A newer real model snapshot may predate the account snapshot;
            # retain its measured Fable increment, estimate only the remainder.
            row['nf_points'] = max(0., 200 * float(row['u7']) - 100 * float(row['uf']))
            row['nf_observed_at'] = account_at
            row['nf_estimated'] = True
        values = gauges(row)
        if not previous:
            ledger['sources'][key] = dict(time=now, row=dict(row), high=values, evidence_high=dict(values), step_times={c: now for c in values}, raw_step_times={m: now for m in ('weekly', 'Fable', '5-hour')})
            for category in values:
                ledger['starts'].setdefault(key + ':' + category, now)
            continue
        old = previous['row']
        step_times = dict(previous.get('step_times', {}))
        for category in values:
            step_times.setdefault(category, max((e[1] for e in ledger.get('meter_intervals', [])
                                                if e[2] == category and e[4] == key), default=previous['time']))
        for meter, field in (('weekly', 'u7'), ('Fable', 'uf'), ('5-hour', 'u5')):
            value, prior = row.get(field), old.get(field)
            if value is not None and prior is not None and abs(float(value) - float(prior)) > 1e-8:
                prior_step = previous.get('raw_step_times', {}).get(meter, previous['time'])
                ledger.setdefault('steps', []).append(dict(id=f'{key}:{meter}:{now}:{value}', first_seen=now, account=row['email'],
                    provider=identity[0], meter=meter, old=float(prior)*100, new=float(value)*100, previous_step=prior_step, routing=None))
        raw_step_times = dict(previous.get('raw_step_times', {}))
        for step in ledger.get('steps', [])[-3:]:
            if step['account'] == row['email'] and step['first_seen'] == now:
                raw_step_times[step['meter']] = now
        carried = dict(previous.get('carried', {}))
        before = gauges(old)
        fable_reset = (reset_changed(old, row, 'rf') or
                       (row.get('uf') is not None and old.get('uf') is not None
                        and float(row['uf']) < float(old['uf']) - .0201))
        weekly_reset = (reset_changed(old, row, 'r7') or
                        (not fable_reset and row.get('u7') is not None and old.get('u7') is not None
                         and float(row['u7']) < float(old['u7']) - .0201))
        # A weekly reset invalidates older model snapshots. Keep their previous
        # accounting epoch until a genuine post-reset snapshot arrives; never
        # charge the old gauge again as consumption in the new week.
        reset_pending = dict(previous_row.get('weekly_reset_pending', {}))
        if weekly_reset and identity[0] == 'claude' and account_at is not None:
            for category, field in (('fable', 'fable_observed_at'), ('non-fable', 'nf_observed_at')):
                if row.get(field) is not None and row[field] < account_at:
                    reset_pending[category] = account_at
        row['weekly_reset_pending'] = reset_pending
        high = previous['high']
        evidence_high = dict(previous.get('evidence_high', before))
        for category, value in values.items():
            field = ('nf_observed_at' if category == 'non-fable' else
                     'fable_observed_at' if category == 'fable' else 'observed_at')
            pending_reset = category in reset_pending
            if pending_reset:
                if value is None or row.get(field) is None or row[field] < reset_pending[category]:
                    carried.setdefault(category, now)
                    if weekly_reset:
                        ledger['gaps'].append([now, category, key])
                    continue
                reset_pending.pop(category)
            if value is None:
                carried.setdefault(category, now)
                continue
            carried.pop(category, None)
            pending = ledger.setdefault('pending_rebase', {}).get(key, [])
            if category in pending:
                field = ('nf_observed_at' if category == 'non-fable' else
                         'fable_observed_at' if category == 'fable' else 'observed_at')
                if row.get(field) is not None and row[field] <= ledger['forgiven'].get(category, -math.inf):
                    continue
                high[category] = evidence_high[category] = value
                step_times[category] = now
                ledger.setdefault('estimate_epochs', {})[key + ':' + category] = now
                pending.remove(category)
                continue
            bad = before.get(category) is None
            reset = weekly_reset or pending_reset or (fable_reset and category == 'fable')
            if reset:
                step_times[category] = now
                ledger.setdefault('estimate_epochs', {})[key + ':' + category] = now
            ambiguous = fable_reset and not weekly_reset and not pending_reset and category == 'non-fable'
            if bad or reset or ambiguous:
                ledger['gaps'].append([now, category, key])
            if bad or ambiguous:
                # Rebase the gauge, retaining a durable gap until it leaves every
                # relevant window or a human explicitly releases this category.
                high[category] = value
                evidence_high[category] = value
                continue
            prior = high.get(category)
            delta = value if reset else max(0., value - (value if prior is None else prior))
            if not reset and prior is not None and value < prior - 2.01:
                ledger['gaps'].append([now, category, key])
            if delta > 1e-10:
                ledger['events'].append([now, category, delta * source['weight']])
                if category == 'non-fable' and fallback:
                    ledger.setdefault('estimated_meter_updates', []).append(dict(
                        source=key, at=now, points=delta * source['weight'],
                        basis='account-wide change; zero Fable consumption in missing-signal interval'))
                    ledger['estimated_meter_updates'] = ledger['estimated_meter_updates'][-2000:]
                begin = step_times.get(category, previous['time'])
                if not reset and begin < now:
                    ledger.setdefault('spread_events', []).append([begin, now, category, delta * source['weight'], key, source['weight'] * (2 if category == 'non-fable' else 1)])
                step_times[category] = now
            field = ('nf_observed_at' if category == 'non-fable' else
                     'fable_observed_at' if category == 'fable' else 'observed_at')
            old_time, new_time = old.get(field), row.get(field)
            aligned = category != 'non-fable' or (old.get('nf_points') is not None and row.get('nf_points') is not None)
            prior_evidence = evidence_high.get(category)
            if reset or not aligned or prior_evidence is None or old_time is None:
                evidence_high[category] = value
            else:
                confirmed_delta = max(0., value - prior_evidence)
                if (confirmed_delta > 1e-10 and new_time is not None and old_time < new_time <= now + 5
                        and not (category == 'non-fable' and fallback)):
                    ledger['meter_intervals'].append(
                        [old_time, new_time, category, confirmed_delta * source['weight'], key])
                evidence_high[category] = max(prior_evidence, value)
            high[category] = value if reset or prior is None else max(prior, value)
        # Preserve the entire last good paired snapshot when its gauge vanishes.
        merged = dict(row)
        if carried:
            for field in ('u7', 'uf', 'nf_points', 'u5', 'r7', 'rf', 'r5',
                          'observed_at', 'fable_observed_at', 'nf_observed_at'):
                if merged.get(field) is None and old.get(field) is not None:
                    merged[field] = old[field]
        for category in carried:
            field = ('nf_observed_at' if category == 'non-fable' else
                     'fable_observed_at' if category == 'fable' else 'observed_at')
            merged[field] = old.get(field)
        ledger['sources'][key] = dict(time=now, row=merged, high=dict(high),
                                      evidence_high=evidence_high, carried=carried, step_times=step_times, raw_step_times=raw_step_times)
    # Keep whole final/rolling history; prune only evidence older than 8 days.
    ledger['events'] = [e for e in ledger['events'] if e[0] >= now - 8 * 86400]
    ledger['gaps'] = [e for e in ledger['gaps'] if e[0] >= now - 8 * 86400]
    ledger['meter_intervals'] = [e for e in ledger.get('meter_intervals', []) if e[1] >= now - 86400]


def initialize(history, now, policy):
    ledger = empty_ledger()
    for row in sorted((r for r in history if now - 8 * 86400 <= r['time'] <= now), key=lambda r: r['time']):
        observe(ledger, [row], row['time'], policy)
    return ledger


def active_run(policy, now):
    return next((r for r in policy.get('runs', []) if r['start'] <= now < r['end']), None)


def run_id(run):
    return f"{run['name']}:{run['start']}:{run['end']}"


def remaining_capacity(policy, ledger, end, now):
    out = dict.fromkeys(CATEGORIES, 0.)
    for source in policy['sources']:
        current = ledger['sources'].get(':'.join((source['provider'], source['email'])))
        if not current or now - current['time'] > 1200:
            raise ValueError('fresh readings required to size remaining capacity')
        row = current['row']
        g = gauges(row)
        for category, used in g.items():
            if used is None:
                raise ValueError('missing category meter')
            banked = source.get('fable_banked' if category == 'fable' else 'banked', 0) if category != 'non-fable' else 0
            weekly_banked = source.get('weekly_banked', 0) if source['provider'] == 'claude' else 0
            banked += weekly_banked
            available = max(0., 100 - used) + 100 * banked
            # Weekly and 5h constrain the SAME spend; never add their capacities.
            if row.get('u5') is not None:
                ratio = source.get('weekly_per_5h')
                if not ratio:
                    raise ValueError('short-window conversion missing')
                r5 = float(row.get('r5') or end)
                fresh_windows = 0 if r5 >= end else 1 + int((end - r5 - 1e-6) // 18000)
                short = ratio * (max(0., 1 - float(row['u5'])) + fresh_windows)
                available = min(available, short * (2 if category in ('fable', 'non-fable') else 1))
            if category == 'fable':
                # Fable also needs shared all-models headroom. Banked refunds
                # are a maximum-capacity scenario, not an automatic redemption.
                available = min(available, max(0., 200 * (1 - float(row['u7']))) +
                                100 * banked + 100 * weekly_banked)
            out[category] += available * source['weight']
    return out


def countable(ledger, event):
    return not any(c == event[1] and begin < event[0] <= end
                   for c, begin, end in ledger.get('override_periods', []))



def meter_estimate(ledger, category, start, now):
    """Linear meter steps, plus at most one step of continued last-step rate."""
    spread = [e for e in ledger.get('spread_events', []) if e[2] == category]
    replaced = Counter((e[1], e[2], e[3]) for e in spread)
    total = 0.
    for event in ledger['events']:
        identity = tuple(event[:3])
        if replaced[identity]:
            replaced[identity] -= 1
        elif event[1] == category and start < event[0] <= now and countable(ledger, event):
            total += event[2]
    latest = {}
    for interval in spread:
        begin, end, cat, amount, key = interval[:5]
        quantum = interval[5] if len(interval) > 5 else amount
        if end <= begin or not countable(ledger, [end, cat, amount]):
            continue
        overlap = max(0., min(now, end) - max(start, begin))
        total += amount * overlap / (end - begin)
        epoch = max(ledger.get('estimate_epochs', {}).get(key + ':' + cat, -math.inf),
                    ledger['forgiven'].get(cat, -math.inf))
        if end > epoch and (key not in latest or latest[key][1] < end):
            latest[key] = (begin, end, amount, quantum)
    for key, (begin, end, amount, quantum) in latest.items():
        # One completed step is the extrapolation ceiling, never unbounded pace.
        duration = end - begin
        tail_end = min(now, end + duration * min(1., quantum / amount))
        total += amount * max(0., tail_end - max(start, end)) / duration
    return total


def box_estimate(policy, ledger, category, start, now):
    if not policy.get('linear_meter_steps', False):
        return sum(e[2] for e in ledger['events'] if e[1] == category and start < e[0] <= now and countable(ledger, e))
    if not policy.get('linear_meter_steps', False):
        return sum(e[2] for e in ledger['events'] if e[1] == category and start < e[0] <= now and countable(ledger, e))
    meter = meter_estimate(ledger, category, start, now)
    second = token_estimate(policy, ledger, category, start, now)
    return meter if second is None else min(meter, second)


def token_estimate(policy, ledger, category, start, now):
    """Use only supported, fully sampled/account-attributed local evidence."""
    try:
        import token_cost_model as fitted
        model = json.loads(Path(__file__).with_name('token_cost_model.json').read_text())
        meter_name = 'weekly_fable' if category == 'fable' else 'weekly_non_fable' if category == 'non-fable' else 'weekly_all'
        supported = any(account.get('meters', {}).get(meter_name, {}).get('usable') is True
                        for account in model['accounts'].values()
                        if (account['provider'] == 'codex') == (category == 'codex'))
        sample = ledger.get('token_sample', {})
        if (not supported or start < sample.get('since', math.inf) or
                now - sample.get('at', -math.inf) > 90 or not sample.get('complete')):
            return None
        records = [entry['usage'] for entry in sample.get('records', [])
                   if entry['category'] == category and start < entry['time'] <= now]
        return fitted.estimate_points(records, model=model, policy=policy).get(category)
    except Exception:
        # An absent, unreadable or invalid calibration disables this estimate.
        return None


def log_steps(host, ledger):
    path = host.LOG.parent / 'meter-steps.jsonl'
    known = set()
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                known.add(json.loads(line)['id'])
            except (ValueError, KeyError):
                pass
    try:
        routes = json.loads((host.LOG.parent / 'passive-rate.json').read_text()).get('routes', [])
    except (OSError, ValueError):
        routes = []
    with path.open('a') as out:
        for step in ledger.get('steps', []):
            if step['id'] not in known:
                candidates = [route for route in routes if route[1] == step.get('provider')
                              and step['first_seen'] - 180 <= route[0] <= step['first_seen'] + 2]
                if candidates:
                    newest = max(route[0] for route in candidates)
                    accounts = {route[3] for route in candidates if route[0] == newest}
                    if len(accounts) == 1:
                        step['routing'] = next(iter(accounts))
                out.write(json.dumps(step, sort_keys=True) + '\n')



def spent(ledger, category, start, now):
    start = max(start, ledger['forgiven'].get(category, -math.inf))
    return sum(e[2] for e in ledger['events'] if e[1] == category and start < e[0] <= now and countable(ledger, e))


def soft_credit(ledger, category, start=-math.inf, end=math.inf):
    """Fixed human grants in category meter points; never rescale after a grant.

    Each grant is negative usage at its own moment (they count as negative
    usage at a single point): it offsets only a window (start, end] that contains
    its time, like a usage event, and leaves the window as the window moves past it.
    """
    return finite(sum(finite(item['points']) for item in ledger.get('soft_credits', [])
                      if item['category'] == category and start < finite(item['at']) <= end))


def readings(policy, ledger, now):
    run = active_run(policy, now)
    totals = capacities(policy)
    output = []
    for category in CATEGORIES:
        if run:
            identity = run_id(run)
            if identity not in ledger['runs']:
                ledger['runs'][identity] = dict(
                    allow=(remaining_capacity(policy, ledger, run['end'], now) if run.get('remaining') else dict(run['allow'])),
                    baseline=now)
            saved = ledger['runs'][identity]
            # The first run reading is its baseline, taken before agents start.
            # This explicitly excludes consumption before the run's opening meter.
            windows = [(run['name'], saved['baseline'], run['end'], saved['allow'][category])]
        else:
            windows = ([] if preparation_active(policy, now) else
                       [(f'last {hours}h', now - hours * 3600, now + hours * 3600, round(totals[category] * part, 12))
                        for hours, part in ROLLING])
        for name, start, end, allowance in windows:
            effective = max(start, ledger['forgiven'].get(category, -math.inf))
            # Preserve logged pre-install spend. Old missing/ambiguous evidence
            # remains visible but does not retroactively stop work on deployment.
            coverage_start = max(effective, ledger.get('activated_at', -math.inf))
            relevant = [s for s in policy['sources'] if (s['provider'] == 'codex') == (category == 'codex')]
            incomplete = any(e[1] == category and coverage_start < e[0] <= now for e in ledger['gaps'])
            for source in relevant:
                key = ':'.join((source['provider'], source['email']))
                current = ledger['sources'].get(key)
                # Startup has explicit partial coverage. Never silently call it zero.
                first = ledger['starts'].get(key + ':' + category, math.inf)
                if not current or now - current['time'] > 1200 or gauges(current['row']).get(category) is None:
                    incomplete = True
                if first > coverage_start + 1200:
                    incomplete = True
            used = box_estimate(policy, ledger, category, effective, now)
            expires = [e[0] + (end - now) for e in ledger['events'] if e[1] == category and effective < e[0] <= now]
            historical_partial = (not run and any(e[1] == category and effective < e[0] <= coverage_start for e in ledger['gaps']))
            output.append(dict(category=category, name=name, start=start, end=end,
                               allowed=allowance + soft_credit(ledger, category, effective, now), used=used,
                               soft_credit=soft_credit(ledger, category, effective, now), incomplete=incomplete,
                               historical_partial=historical_partial,
                               oldest_expires=min(expires) if expires else None,
                               run=bool(run), approved=policy['approved'] and (run is None or run['approved'])))
    return output + (prep_readings(policy, ledger, now) if preparation_active(policy, now) else [])


def message(reading, now, blocked=False):
    label = LABELS[reading['category']]
    left = max(0., reading['end'] - now)
    timing = (f'{left / 3600:.1f}h left' if reading['run'] else
              f"{max(0, reading['oldest_expires'] - now) / 60:.0f}m until oldest use leaves the window"
              if reading.get('oldest_expires') else 'rolling window; no spending recorded yet')
    action = ('Over the box: music is playing; nothing is blocked.' if blocked else
              'Earlier readings are missing; this is counted spending so far. Reporting accounts keep counting.' if reading['incomplete'] else
              'At the cap, music plays; nothing is blocked.' if reading['approved'] else
              'Proposed only; enforcement awaits approval.')
    coverage = ' Logged use is a lower bound across old missing readings.' if reading.get('historical_partial') else ''
    coverage += f" Soft credit: {reading.get('soft_credit', 0):.4f} points." if reading.get('soft_credit', 0) else ''
    return f"{label}, {reading['name']}: {reading['used']:.2f} of {reading['allowed']:.2f} points used; {timing}. {action}{coverage}"


def notice_rate(policy, ledger, category, now):
    """Read the installed rate for display without creating/changing baselines."""
    if not policy or not ledger or not preparation_active(policy, now):
        return None
    prep = policy['preparation']
    run = active_run(policy, now)
    if now >= prep['end']:
        opening = ledger.get('runs', {}).get(run_id(run), {}) if run else {}
        allowance = opening.get('allow', {}).get(category)
        return allowance / ((run['end'] - run['start']) / 3600) if allowance is not None else None
    saved = ledger.get('preparation', {}).get(str(prep['start']), {})
    planned = saved.get('P', {}).get(category)
    return planned / prep['active_hours'] * (run.get('scale', 1.) if run else 1.) if planned is not None else None


def notice_message(reading, now, blocked=False, policy=None, ledger=None):
    import claude_usage_messages as human
    display = dict(reading)
    display['estimated'] = bool(reading['category'] == 'non-fable' and ledger and any(
        source.get('row', {}).get('nf_estimated') for source in ledger.get('sources', {}).values()))
    window = re.search(r'(\d+)-minute rate$', reading['name'])
    rate = notice_rate(policy, ledger, reading['category'], now)
    if window and rate is not None:
        display['rate_points'] = rate * int(window.group(1)) / 60
    return human.box_notice(display, now, blocked)


def pools_for(category, policy):
    pools = []
    for source in policy['sources']:
        provider = source['provider']
        if (provider == 'codex') != (category == 'codex'):
            continue
        suffix = 'week' if category == 'codex' else category
        pools.append(':'.join((provider, source['email'], suffix)))
    return pools


def enforce(host, policy, ledger, items, now):
    events = []
    with host.guard_lock(host.GUARD_STATE):
        guard = host.guard_state(host.GUARD_STATE)
        disabled = override_state(host).get('disabled', {})
        for reading in items:
            if reading['category'] in disabled:
                continue
            # Incomplete readings no longer block a category (they deadlocked Codex twice, and --uncap
            # could not clear it). Stops fire on confirmed spend only. Emergency fix; revisit.
            if not reading['approved'] or not (reading['used'] >= reading['allowed'] - 1e-8):
                continue
            category = reading['category']
            for pool in pools_for(category, policy):
                key = 'category:' + category + ':' + pool
                existing = guard['latches'].get(key)
                if existing and re.search(r'\d+-minute rate$', existing.get('box', '')) and not re.search(r'\d+-minute rate$', reading['name']):
                    # A real allowance stop must survive later pacing changes.
                    guard['latches'].pop(key)
                guard['latches'].setdefault(key, dict(pool=pool, category=category, box=reading['name'],
                    reason='allowance reached',
                    used=reading['used'], allowed=reading['allowed'], end=reading['end'], at=now,
                    start=reading['start'], soft_credit=reading.get('soft_credit', 0), planned=reading.get('planned')))
        # Keep each latch's current reading, so the spoken percentage is live.
        for reading in items:
            for latch in guard['latches'].values():
                if latch.get('category') == reading['category'] and latch.get('box') == reading['name']:
                    latch['used_now'] = reading['used']
        host.atomic_write(host.GUARD_STATE, json.dumps(guard, sort_keys=True) + '\n')
        host.apply_hard_stops(guard, host.GUARD_STATE)
        for category in CATEGORIES:
            latches = [v for v in guard['latches'].values() if v.get('category') == category]
            if latches:
                v = latches[0]
                reading = dict(category=category, name=v['box'], used=v['used'], allowed=v['allowed'], end=v['end'], run=True, soft_credit=v.get('soft_credit', 0))
                events.append(('trip:' + category + ':' + str(v['at']), 4, notice_message(reading, now, True, policy, ledger), 1800, True))
    return events


def warnings(items, ledger, now, policy=None):
    import claude_usage_messages as human
    events = []
    for reading in items:
        used, allowed = reading['used'], reading['allowed']
        fraction = used / allowed if allowed else 1
        level = 3 if fraction >= .9 else 2 if fraction >= .75 else 1 if fraction >= .5 else 0
        # Preparation P/H budgets ACTIVE hours. Never extrapolate their spend
        # over the wall-clock time to final; the fixed rate ladder owns pace.
        prep_total = reading.get('prep_total') or reading['name'].startswith('Preparation total')
        if prep_total and level == 1:
            continue  # Routine budget NOTE has no action; accounting/guards stay active.
        rate = (0. if prep_total else spent(ledger, reading['category'], max(reading['start'], now - 3600), now)
                / min(3600, max(1, now - reading['start'])))
        projected = rate > 0 and used < allowed and used + rate * (reading['end'] - now) > allowed
        if projected:
            level = max(level, 2)
        if level:
            seconds = min(3600, max(1, now - reading['start']))
            installed_rate = notice_rate(policy, ledger, reading['category'], now)
            planned_rate = (installed_rate / 3600 if installed_rate is not None else
                            allowed / max(1, reading['end'] - reading['start']))
            pace = (rate * seconds, planned_rate * seconds, seconds / 60) if projected else None
            line = human.box_notice(reading, now, pace=pace, level='WARNING' if level >= 2 else 'NOTE',
                                    forecast=now + (allowed-used)/rate if projected else None)
            events.append((reading['category'] + ':' + reading['name'], level, line, 600 if level >= 3 else 1800, False))
    return events


def alert_path(host):
    """Only the real state directory writes the final alert log; tests with a fake LOG write beside it."""
    real = Path.home() / '.local/state/claude-usage/log.tsv'
    return ALERT_LOG if Path(host.LOG) == real else Path(host.LOG).parent / 'usage-alerts.log'


def append_alert(line, path=ALERT_LOG):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as target:
            target.write(datetime.now().astimezone().isoformat(timespec='seconds') + ' ' + ' '.join(line.splitlines()) + '\n')
        return True
    except OSError:
        return False


def thread_notice(host, line):
    """Explicit steer first. Idle-only fallback; never request queue mode."""
    try:
        target = (host.LOG.parent / 'notify-thread').read_text().strip()
        if not target or len(target.splitlines()) != 1 or len(target) > 256:
            return False
        for mode in ('steer', 'auto'):
            result = subprocess.run([str(host.T3), 'thread', 'send', target, '-', '--mode', mode, '--json'],
                                    input=line if getattr(host, 'MATRIX_COPY_ALL', False) else ' '.join(line.splitlines()), text=True, capture_output=True, timeout=20, check=False)
            if result.returncode == 0:
                reply = json.loads(result.stdout)
                delivery = reply.get('delivery', (reply.get('result') or {}).get('delivery'))
                # The user accepts the rare auto/Stop race that queues
                # for the immediately following turn. Never REQUEST queue mode.
                return delivery if delivery in ('steered', 'started', 'queued') else False
            error = (result.stdout + result.stderr).lower()
            if mode != 'steer' or not any(word in error for word in ('idle', 'not running', 'no active run', 'no running')):
                break
        return False
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def deliver(host, events, now, path=None, senders=None):
    path = path or host.LOG.parent / 'category-notices.json'
    receipts = host.read_state(path)
    active = set()
    for key, level, line, interval, trip in events:
        active.add(key)
        previous = receipts.setdefault(key, {})
        channels = senders if senders is not None else {'desktop': host.desktop_notify, 'thread': lambda text: thread_notice(host, text), 'file': lambda line: append_alert(line, alert_path(host))}
        thread_attempted = False
        delivery_result = previous.get('thread', {}).get('delivery', 'not sent')
        for channel, sender in channels.items():
            if channel == 'thread' and not (trip or key.startswith(('rate:', 'category-out:', 'tool:'))):
                continue
            receipt = previous.get(channel, {})
            if (now - receipt.get('at', -math.inf) < interval and level <= receipt.get('level', 0)
                    and not (channel == 'file' and thread_attempted)):
                continue
            if channel == 'thread':
                thread_attempted = True
            result = sender(line + f' [thread delivery={delivery_result}]' if channel == 'file' else line)
            if result:
                previous[channel] = dict(at=now, level=level)
                if channel == 'thread':
                    delivery_result = result if isinstance(result, str) else 'delivered'
                    previous[channel]['delivery'] = delivery_result
            else:
                if channel == 'thread':
                    delivery_result = 'FAILED; retry next check'
                print(f'claude-usage: {channel} alert failed; retry next check')
    # Clearing risk must not allow another five-minute warning inside 15m.
    keep = {k: v for k, v in receipts.items() if k in active or
            (k.startswith('rate:') and any(now - r.get('at', -math.inf) < 900 for r in v.values()))}
    host.atomic_write(path, json.dumps(keep, sort_keys=True) + '\n')


def override_state(host):
    return host.read_state(host.LOG.parent / 'hard-continue.json')


def override_command(host, resume=None, confirmer=None, now=None):
    """Human override. No meter or policy reads; ledger then guard lock order."""
    now = datetime.now().timestamp() if now is None else now
    if resume is None:
        if confirmer is None:
            def confirmer():
                try:
                    with open('/dev/tty', 'r') as reader, open('/dev/tty', 'w') as writer:
                        writer.write('Release ALL categories and suspend stops? Type CONTINUE: ')
                        writer.flush()
                        return reader.readline().strip() == 'CONTINUE'
                except OSError:
                    return False
        if not confirmer():
            print('No change; human terminal confirmation required.')
            return 2
    path = host.LOG.parent / 'category-ledger.json'
    with host.guard_lock(path), host.guard_lock(host.GUARD_STATE):
        saved = override_state(host)
        disabled = saved.setdefault('disabled', {})
        before_disabled = set(disabled)
        try:
            ledger = json.loads(path.read_text())
        except (OSError, ValueError):
            # The emergency release must survive unavailable accounting.
            # Keep broken evidence in place; fresh windows can be rebuilt later.
            ledger = empty_ledger()
            ledger['override_recovered_ledger'] = True
        guard = host.guard_state(host.GUARD_STATE)
        if resume is None:
            for category in CATEGORIES:
                disabled.setdefault(category, now)
            guard['latches'] = {}
        else:
            for category in CATEGORIES if resume == 'all' else (resume,):
                begin = disabled.pop(category, None)
                if begin is not None:
                    ledger.setdefault('override_periods', []).append([category, begin, now])
                    ledger['forgiven'][category] = now
                    ledger.setdefault('cap_restart', {})[category] = now
                    for key in ledger['sources']:
                        if key.startswith('codex:') == (category == 'codex'):
                            pending = ledger.setdefault('pending_rebase', {}).setdefault(key, [])
                            if category not in pending:
                                pending.append(category)
        if ledger.pop('override_recovered_ledger', False) and path.exists():
            host.atomic_write(path.with_name('category-ledger.before-override.json'), path.read_text())
        host.atomic_write(path, json.dumps(ledger, sort_keys=True) + '\n')
        host.atomic_write(host.LOG.parent / 'hard-continue.json', json.dumps(saved, sort_keys=True) + '\n')
        host.atomic_write(host.GUARD_STATE, json.dumps(guard, sort_keys=True) + '\n')
        host.apply_hard_stops(guard, host.GUARD_STATE)
    line = ('Hard-continue is on; all category stops suspended. Notifications continue.' if resume is None else
            'Hard-continue ended for ' + resume + '; caps enforce again with fresh windows.')
    print(line)
    changed = before_disabled != set(disabled)
    if changed:
        import claude_usage_messages as human
        body = human.render([('override', 1, human.pack('NOTE', 'Usage tool',
            'hard-continue is on' if resume is None else 'caps resumed for ' + resume,
            f'{len(disabled)} of 3 categories uncapped', 'changed ' + human.end_text(now, now),
            'run claude-usage --un-hard-continue to restore caps' if resume is None else 'nothing needed'))], now)
        append_alert(line, alert_path(host))
        host.desktop_notify(body)
        matrix_notice(body)
        if getattr(host, 'MATRIX_COPY_ALL', False):
            thread_notice(host, 'For you:\n' + body)
    return 0


def rate_latch_math(policy, ledger, item, context):
    """New math over original evidence; missing history cannot erase a stop."""
    match = re.search(r'(\d+)-minute rate$', item.get('box', ''))
    if not match or item.get('category') not in CATEGORIES:
        return None
    minutes, category = int(match.group(1)), item['category']
    default = next((d for m, _, d in RATE_LADDER if m == minutes), None)
    if default is None:
        return None
    at = item['at']
    lower = max(item.get('start', at - minutes * 60), at - minutes * 60)
    used = confirmed_window(ledger, category, lower, at, policy)
    if policy.get('linear_meter_steps', False):
        estimate = box_estimate(policy, ledger, category, lower, at)
        used = min(used, estimate) if category == 'non-fable' else estimate
    # The stored confirmed amount protects the original window even after
    # partial pruning. A new pace/threshold/credit can clear it; lost evidence
    # cannot. This also covers legacy latches without an evidence snapshot.
    used = max(used, item['used'])
    limit = context['R'][category] * minutes / 60 * policy['preparation'].get('stops', {}).get(str(minutes), default)
    # Credits granted inside or after the latched window were granted for this stop.
    return used, limit + soft_credit(ledger, category, lower)


def soft_uncap(host, policy, category, minutes, confirmer=None, now=None, only_if_capped=False):
    """Add a fixed points credit without forgiving or rebasing any spending.

    only_if_capped: credit goes only to categories that are capped right now;
    a category with no current stop gets no credit and nothing is written.
    """
    if category not in CATEGORIES:
        raise ValueError('choose fable, non-fable or codex')
    minutes = finite(minutes, True)
    if confirmer is None:
        def confirmer():
            try:
                with open('/dev/tty', 'r') as reader, open('/dev/tty', 'w') as writer:
                    writer.write(f'Add {minutes:g} minutes of soft credit to {LABELS[category]} (including prep reserve)? Type SOFT: ')
                    writer.flush()
                    return reader.readline().strip() == 'SOFT'
            except OSError:
                return False
    if not confirmer():
        print('No change; human terminal confirmation required.')
        return 2
    now = datetime.now().timestamp() if now is None else now
    path = host.LOG.parent / 'category-ledger.json'
    rate_path = host.LOG.parent / 'rate-ladder.json'
    with host.guard_lock(rate_path), host.guard_lock(path), host.guard_lock(host.GUARD_STATE):
        if only_if_capped and not any(category in latch_categories(item)
                                      for item in host.guard_state(host.GUARD_STATE)['latches'].values()):
            print(f'{LABELS[category]}: not capped; skipped, no credit added.')
            return 0
        ledger = json.loads(path.read_text())
        context = rate_context(policy, ledger, now)
        per_minute = context['R'][category] / 60
        points = finite(per_minute * minutes, True)
        ledger.setdefault('soft_credits', []).append(dict(category=category, points=points, minutes=minutes,
                                                         rate=context['R'][category], at=now))
        credit = soft_credit(ledger, category)
        guard = host.guard_state(host.GUARD_STATE)
        items = [item for item in readings(policy, ledger, now) if item['category'] == category]
        _, rate_trips = ladder_evaluate(policy, ledger, {}, {}, now, all_trips=True)
        items += [item for item in rate_trips if item['category'] == category]
        short = max([0.] + [item['used'] - item['allowed'] + 1e-8 for item in items])
        for key, item in list(guard['latches'].items()):
            if item.get('category') != category:
                continue
            rate_math = rate_latch_math(policy, ledger, item, context)
            if rate_math is not None:
                used, allowed = rate_math
            elif re.search(r'\d+-minute rate$', item.get('box', '')):
                continue  # Unknown stops cannot be released by guessed math.
            else:
                current = next((row for row in items if row['name'] == item['box']), None)
                used = current['used'] if current else item['used']
                allowed = (current['allowed'] if current else item['allowed'] - item.get('soft_credit', 0)
                           + soft_credit(ledger, category, item.get('start', -math.inf)))
            short = max(short, used - allowed + 1e-8)
            if used < allowed - 1e-8:
                guard['latches'].pop(key)
            else:
                # soft_credit records the credit already inside this latch's allowed figure.
                item.update(used=used, allowed=allowed, soft_credit=(
                    current['soft_credit'] if rate_math is None and current else
                    soft_credit(ledger, category, item.get('start', -math.inf))))
        # Enforce other currently exceeded caps too; credit does not erase them.
        for reading in items:
            if reading['used'] >= reading['allowed'] - 1e-8:
                for pool in pools_for(category, policy):
                    key = 'category:' + category + ':' + pool
                    guard['latches'].setdefault(key, dict(pool=pool, category=category, box=reading['name'],
                        reason='allowance reached', used=reading['used'], allowed=reading['allowed'],
                        start=reading['start'], end=reading['end'], at=now, soft_credit=reading.get('soft_credit', 0)))
        blocked = any(item.get('category') == category for item in guard['latches'].values())
        state = host.read_state(rate_path)
        if not blocked:
            state.setdefault('trip_epochs', {}).pop(category, None)
        host.atomic_write(path, json.dumps(ledger, sort_keys=True) + '\n')
        host.atomic_write(rate_path, json.dumps(state, sort_keys=True) + '\n')
        host.atomic_write(host.GUARD_STATE, json.dumps(guard, sort_keys=True) + '\n')
        host.apply_hard_stops(guard, host.GUARD_STATE)
    if blocked:
        extra = math.ceil(max(short, 1e-8) / per_minute * 100) / 100
        detail = f'Still blocked: {max(0., short):.4f} points short; at least {extra:g} more minutes (rounded up to 0.01 minute).'
    else:
        detail = 'Released; counted spending is unchanged and stops apply again after the credit is used.'
    line = f'{LABELS[category]}: added {points:.4f} points soft credit ({minutes:g} minutes); total {credit:.4f}. {detail}'
    print(line)
    import claude_usage_messages as human
    status = 'STOPPED' if blocked else 'RELEASED'
    body = human.render([('soft-credit', 4 if blocked else 1, human.pack(status, LABELS[category],
        'soft credit added, still blocked' if blocked else 'soft credit added, requests resumed',
        f'{credit:.4f} soft-credit points ({points:.4f} added)',
        'stays blocked until credit covers overage' if blocked else 'released ' + human.end_text(now, now),
        human.release_action(extra) if blocked else 'nothing needed'))], now)
    append_alert(line, alert_path(host))
    host.desktop_notify(body)
    matrix_notice(body)
    if getattr(host, 'MATRIX_COPY_ALL', False):
        thread_notice(host, 'For you: ' + body)
    return 1 if blocked else 0


def latch_categories(item):
    if item.get('category') in CATEGORIES:
        return (item['category'],)
    parts = item.get('pool', '').split(':')
    if parts[0] == 'codex':
        return ('codex',)
    if parts[0] == 'claude':
        return (parts[-1],) if parts[-1] in ('fable', 'non-fable') else ('fable', 'non-fable')
    return ()


def hard_uncap_all(host, stdin=None, stdout=None, now=None):
    """The no-prompt human command: release all guards, keep caps enabled."""
    stdin = host.sys.stdin if stdin is None else stdin
    stdout = host.sys.stdout if stdout is None else stdout
    if not stdin.isatty() or not stdout.isatty():
        print('uncap: refused; run it directly in an interactive terminal.', file=stdout)
        return 2
    now = datetime.now().timestamp() if now is None else now
    path = host.LOG.parent / 'category-ledger.json'
    with host.guard_lock(path), host.guard_lock(host.GUARD_STATE):
        guard = host.guard_state(host.GUARD_STATE)
        before = list(guard['latches'].values())
        if path.exists():
            ledger = json.loads(path.read_text())
            for category in CATEGORIES:
                ledger.setdefault('forgiven', {})[category] = now
            for key in ledger.get('sources', {}):
                categories = ('codex',) if key.startswith('codex:') else ('fable', 'non-fable') if key.startswith('claude:') else ()
                pending = ledger.setdefault('pending_rebase', {}).setdefault(key, [])
                pending.extend(category for category in categories if category not in pending)
            host.atomic_write(path, json.dumps(ledger, sort_keys=True) + '\n')
        # Keep legacy box tombstones as --uncap <pool> does; new categories
        # reopen their windows via forgiven timestamps rather than exemptions.
        released = guard.setdefault('released', [])
        released.extend(key for key in guard['latches'] if key not in released)
        guard['latches'] = {}
        # A previous rate-restore retry must not emit an obsolete release later.
        guard.pop('rate_reconcile_messages', None)
        guard.pop('rate_reconcile_pending', None)
        host.atomic_write(host.GUARD_STATE, json.dumps(guard, sort_keys=True) + '\n')
        host.apply_hard_stops(guard, host.GUARD_STATE)
    import claude_usage_messages as human
    events = []
    for category in CATEGORIES:
        affected = [item for item in before if category in latch_categories(item)]
        print(LABELS[category] + (': released on every account.' if affected else ': already open; windows restarted.'), file=stdout)
        if affected:
            used = human.pack('RELEASED', LABELS[category], 'hard stops cleared on every account',
                f'0 of {len(affected)} previous stops remain', 'released ' + human.end_text(now, now),
                'nothing needed; caps stay active, preparation spending stays counted')
            events.append((f'hard-release:{category}:{now}', 1, used, math.inf, False))
    known = sum(bool(latch_categories(item)) for item in before)
    if known < len(before):
        print(f'Other usage guards: {len(before)-known} released.', file=stdout)
        line = human.pack('RELEASED', 'Usage tool', 'other hard stops cleared',
            f'0 of {len(before)-known} previous stops remain', 'released ' + human.end_text(now, now), 'nothing needed')
        events.append((f'hard-release:other:{now}', 1, line, math.inf, False))
    if events:
        deliver_current(host, events, now, host.LOG.parent / 'hard-release-notices.json')
    return 0


def uncap_category(target):
    """Accept category names and the previously advertised account-pool keys."""
    if target in CATEGORIES:
        return target
    parts = target.split(':')
    if len(parts) == 3 and parts[1]:
        if parts[0] == 'claude' and parts[2] in ('fable', 'non-fable'):
            return parts[2]
        if parts[0] == 'codex' and parts[2] in ('week', '7d', '5h'):
            return 'codex'
    return None


def uncap(host, category, ledger_path, policy, confirmer=None, reader=None):
    if category not in CATEGORIES:
        raise ValueError('choose fable, non-fable or codex')
    if confirmer is None:
        def confirmer(label):
            try:
                with open('/dev/tty', 'r') as reader, open('/dev/tty', 'w') as writer:
                    writer.write(f'Reopen {LABELS[label]} with a fresh box allowance? Type UNCAP: ')
                    writer.flush()
                    return reader.readline().strip() == 'UNCAP'
            except OSError:
                return False
    if not confirmer(category):
        return False
    # Capture the current cached meter while the category is still blocked.
    # Forgiving timestamps alone would charge pre-release work at the next poll.
    reader = reader or (lambda: host.parse_codex(host.fetch(host.CODEX_URL, host.CODEX_KEY))
                        if category == 'codex' else host.parse(host.fetch()))
    try:
        rows = reader()
    except (OSError, ValueError, KeyError):
        rows = []
    now = datetime.now().timestamp()
    relevant = [s for s in policy['sources'] if (s['provider'] == 'codex') == (category == 'codex')]
    identities = {(r.get('provider', 'claude'), r['email']) for r in rows if gauges(r).get(category) is not None}
    # Emergency fix: a blocked account exposes no meter, so requiring a
    # fresh reading made release impossible. Release proceeds from the ledger's last values.
    missing_baseline = [s['email'] for s in relevant if (s['provider'], s['email']) not in identities]
    if missing_baseline:
        print('Note: no fresh meter for ' + ', '.join(missing_baseline) + '; releasing from the last known reading.')
    # All v2 operations acquire ledger lock before guard lock.
    with host.guard_lock(ledger_path), host.guard_lock(host.GUARD_STATE):
        guard = host.guard_state(host.GUARD_STATE)
        ledger = json.loads(ledger_path.read_text())
        observe(ledger, rows, now, policy)
        ledger['forgiven'][category] = now
        for source in relevant:
            key = ':'.join((source['provider'], source['email']))
            # Even a visible cached gauge may predate the block. Rebase the
            # first returning observation, never charge blocked in-flight use.
            pending = ledger.setdefault('pending_rebase', {}).setdefault(key, [])
            if category not in pending:
                pending.append(category)
        host.atomic_write(ledger_path, json.dumps(ledger, sort_keys=True) + '\n')
        removed = [key for key,item in guard['latches'].items()
                   if item.get('category') == category or uncap_category(item.get('pool', '')) == category]
        for key in removed:
            item = guard['latches'].pop(key)
            if not item.get('category') and key not in guard.setdefault('released', []):
                guard['released'].append(key)
        host.atomic_write(host.GUARD_STATE, json.dumps(guard, sort_keys=True) + '\n')
        host.apply_hard_stops(guard, host.GUARD_STATE)
    return True


def account_events(host, rows, items, ledger, now):
    """Individual meters remain accounting inputs, never human notifications.

    Notify only on categories, never on single accounts. Whole-category exhaustion
    remains in process(); this must not change account metering or guards.
    """
    return []


RECOVERY_CONFIRM = 2  # consecutive same readings (meter check runs every 60 s) before a transition


def _epoch(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def account_limit(row, category, now):
    """-> (out, return) for one account in one category from provider usage-limit errors.

    Out means the proxy holds the account in an active quota cooldown after a
    provider usage-limit error. A whole-credential cooldown counts when the account
    is unavailable with the provider's limit error (Codex `usage_limit_reached`,
    Claude `rate_limit_error`). A Claude model cooldown in this category counts when
    the provider rejected that model's window and it has not reset yet (the shared
    7-day window for non-Fable models); the proxy clears the account error while other models still run, so it
    is not required there. A meter at 100% alone is not out. Return is when the last
    of those cooldowns ends. (None, None) when the row has no cooldown view.
    """
    limit = row.get('limit')
    if not isinstance(limit, dict):
        return None, None
    codex = row.get('provider', 'claude') == 'codex'
    credential_out = limit['unavailable'] and ('usage_limit_reached' if codex else 'rate_limit_error') in limit['errors']
    fable = category == 'fable'

    def covers(c):
        if c.get('scope') == 'credential':
            return credential_out
        model = str(c.get('model') or '')
        if codex or c.get('scope') != 'model' or model.startswith('claude-fable') != fable:
            return False
        # The proxy keeps the last quota headers, so only a rejection whose window has not reset yet counts.
        live = [w for w, reset in limit['rejected'].get(model, {}).items() if reset is not None and reset > now]
        return bool(live) if fable else '7d' in live
    ends = [c['retry_at'] for c in limit['cooldowns'] if covers(c) and c.get('retry_at') and c['retry_at'] > now]
    return (True, max(ends)) if ends else (False, None)


def category_status(policy, rows, now):
    """{category: (out, earliest_return)}; out is None when any source is unreadable.

    A category is out only when every account in it is out by account_limit().
    """
    current = {(r.get('provider', 'claude'), r['email']): r for r in rows}
    result = {}
    for category in CATEGORIES:
        relevant = [s for s in policy['sources'] if (s['provider'] == 'codex') == (category == 'codex')]
        if category == 'fable':  # an account that never serves Fable cannot keep Fable alive
            relevant = [s for s in relevant
                        if (current.get((s['provider'], s['email'])) or {}).get('fable_excluded') is not True]
        out, returns = bool(relevant), []
        for source in relevant:
            row = current.get((source['provider'], source['email']))
            account_out, back = (None, None) if row is None else account_limit(row, category, now)
            if account_out is None:
                out = None  # no cooldown evidence: unknown, never proof of availability
                break
            if not account_out:
                out = False
            elif out is not False:
                returns.append(back)
        result[category] = (out, min(returns) if out and returns else None)
    return result

def recovery_transitions(host, status, now):
    """Append category limit-hit / available-again events for the external recovery agent.

    It fires on limits hit and on limits coming back, per category. Category scope only; the first reading is a silent baseline,
    unreadable sources never count as a transition, and each change needs RECOVERY_CONFIRM
    consecutive readings. Event ids are stable, so a crash between append and state
    save cannot produce a second distinct event (the reader dedups by id).
    """
    path = host.LOG.parent / 'category-availability.json'
    events_path = host.LOG.parent / 'recovery-category-events.jsonl'
    with host.guard_lock(path):
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            state = {}
        appended = []
        for category in CATEGORIES:
            out, earliest = status.get(category, (None, None))
            entry = state.get(category)
            if out is None:
                if entry:
                    entry.update(pending=None, count=0)
                continue
            observed = 'out' if out else 'available'
            if entry is None:
                state[category] = dict(state=observed, since=now, pending=None, count=0, pending_since=None)
                continue
            if observed == entry['state']:
                entry.update(pending=None, count=0, pending_since=None)
                continue
            if entry.get('pending') == observed:
                entry['count'] = entry.get('count', 0) + 1
            else:
                entry.update(pending=observed, count=1, pending_since=now)
            if entry['count'] < RECOVERY_CONFIRM:
                continue
            first = entry['pending_since']
            event = dict(id=f"{category}:{'limit-hit' if out else 'available-again'}:{int(first)}",
                         category=category, label=LABELS[category],
                         transition='limit-hit' if out else 'available-again',
                         first_seen=first, confirmed_at=now, previous_since=entry.get('since'),
                         earliest_return=earliest if out else None)
            appended.append(event)
            entry.update(state=observed, since=first, pending=None, count=0, pending_since=None)
        death_queue(host, appended, now)
        _append_events(events_path, appended)
        host.atomic_write(path, json.dumps(state, sort_keys=True) + '\n')
    return appended


def _append_events(events_path, appended):
    if appended:
        with open(events_path, 'a') as out_file:
            for event in appended:
                out_file.write(json.dumps(event, sort_keys=True) + '\n')
            out_file.flush()
            os.fsync(out_file.fileno())
        os.chmod(events_path, 0o600)


def cap_transitions(host, now):
    """Category capped / cap-cleared events from our own guard latches.

    A category death is steered to the notice thread, all running agents and the recovery
    agent. Death includes both our own usage cap boxes and provider category limits
    running out. Only category latches count (a per-account pool latch is not a
    category death). Latches are durable, so no debounce; first reading is a silent
    baseline. Ids are stable across a crash between append and state save.
    """
    path = host.LOG.parent / 'category-capped.json'
    events_path = host.LOG.parent / 'recovery-category-events.jsonl'
    latches = list(host.guard_state(host.GUARD_STATE)['latches'].values())
    with host.guard_lock(path):
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            state = {}
        appended = []
        for category in CATEGORIES:
            items = [item for item in latches if item.get('category') == category]
            capped = bool(items)
            entry = state.get(category)
            if entry is None and not capped:
                state[category] = dict(capped=False, since=now)
                continue
            entry = entry or dict(capped=False, since=None)  # already capped when first seen: announce it
            if entry.get('capped') == capped:
                continue
            if capped:
                first = min((_epoch(item.get('at')) for item in items if _epoch(item.get('at')) is not None), default=now)
                ends = [_epoch(item.get('end')) for item in items]
                event = dict(id=f'{category}:capped:{int(first)}', category=category, label=LABELS[category],
                             transition='capped', cause='usage box stop',
                             boxes=sorted({str(item.get('box')) for item in items}),
                             first_seen=first, confirmed_at=now, previous_since=entry.get('since'),
                             box_end=max(ends) if ends and None not in ends else None)
                since = first
            else:
                event = dict(id=f"{category}:cap-cleared:{int(entry.get('since') or now)}", category=category,
                             label=LABELS[category], transition='cap-cleared', first_seen=now,
                             confirmed_at=now, previous_since=entry.get('since'))
                since = now
            appended.append(event)
            state[category] = dict(capped=capped, since=since)
        # A recovery host started before recovery.py learned these transitions would word
        # "capped" as AVAILABLE AGAIN: hold the lines until the running host is current.
        death_queue(host, appended, now)
        held = state.pop('recovery_held', []) + appended
        if held and recovery_host_current():
            _append_events(events_path, held)
            held = []
        if held:
            state['recovery_held'] = held
        host.atomic_write(path, json.dumps(state, sort_keys=True) + '\n')
    return appended


RECOVERY_SCRIPT = Path.home() / '.local/lib/t3-external-recovery/recovery.py'


def recovery_host_current():
    """True when every running recovery host process started after recovery.py was installed."""
    try:
        installed = RECOVERY_SCRIPT.stat().st_mtime
        boot = next(float(line.split()[1]) for line in open('/proc/stat') if line.startswith('btime'))
        tick = os.sysconf('SC_CLK_TCK')
        starts = []
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                cmd = (proc / 'cmdline').read_bytes().split(b'\0')
                if not any(part.endswith(b'recovery.py') for part in cmd) or b'run' not in cmd:
                    continue
                stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                starts.append(boot + int(stat[19]) / tick)
            except OSError:
                continue
        return all(start > installed for start in starts)
    except (OSError, ValueError, StopIteration):
        return True  # cannot tell: deliver rather than lose the event


# Category death fan-out: notice thread + all running agents (steer)
# + Discord. Return events (available-again, cap-cleared) go to the recovery agent only.
DEATHS = ('limit-hit',)  # our own box no longer stops a category, so 'capped' is not a death
DEATH_RETRY_SECONDS = 3600
DEATH_DISCORD_TRIES = 3  # then the dashboard intake gets it directly
T3_CLI = Path.home() / '.t3/bin/t3'
T3_STATE_DB = Path.home() / '.t3/userdata/statev2.sqlite'
NOTIFY_DISCORD = Path.home() / '.local/lib/discord-notify/notify_discord.py'
NOTIFY_DASHBOARD = Path.home() / '.local/lib/matrix-relay/notify_mirror.py'
NOTICE_THREAD = os.environ.get('CLAUDE_USAGE_NOTICE_THREAD', '')  # T3 thread for category-death notices; empty: none
ET = ZoneInfo('America/New_York')


def _et_time(value):
    value = _epoch(value)
    return None if value is None else datetime.fromtimestamp(value, ET).strftime('%a %-I:%M %p ET')


def death_text(event):
    label = event['label']
    if event['transition'] == 'capped':
        boxes = ', '.join(event.get('boxes') or []) or 'usage box'
        what = (f'Our usage box stopped the whole {label} category ({boxes}; since {_et_time(event["first_seen"])}). '
                f'Only a human uncap reopens it' + (f'; box period ends {_et_time(event["box_end"])}. ' if event.get('box_end') else '. '))
    else:
        back = _et_time(event.get('earliest_return'))
        what = (f'Every {label} account is at its provider usage limit (since {_et_time(event["first_seen"])}). '
                + (f'Earliest scheduled return {back}. ' if back else 'Return time unknown. '))
    return (f'USAGE CATEGORY DEATH: {label}. ' + what +
            f'Requests on {label} models will fail' +
            (' (the proxy says "unknown provider for model ...")' if event['transition'] == 'capped' else '') +
            ' until it returns. '
            f'Avoid retry loops; keep going on work that does not need {label}. '
            'Category notice only: it grants no uncap, stop, model or provider switch.')


def _run(argv, stdin=None, timeout=20):
    try:
        return subprocess.run([str(a) for a in argv], input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def running_threads(projects=None):
    """-> (running thread ids, projects whose listing failed). Read-only."""
    import sqlite3
    from contextlib import closing
    from concurrent.futures import ThreadPoolExecutor
    if projects is None:
        with closing(sqlite3.connect(f'file:{T3_STATE_DB}?mode=ro', uri=True, timeout=5)) as db:
            # Only projects with a thread touched in the last day: a CLI call costs ~1.3 s each.
            projects = [r[0] for r in db.execute(
                "select distinct t.project_id from orchestration_v2_projection_threads t "
                "join projection_projects p on p.project_id = t.project_id "
                "where t.deleted_at is null and t.archived_at is null and p.deleted_at is null "
                "and t.updated_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda project: _run([T3_CLI, 'thread', 'list', '--project', project, '--json'],
                                                     timeout=15), projects))
    found, failed = [], []
    for project, result in zip(projects, results):
        try:
            if result is None or result.returncode != 0:
                raise ValueError
            found += [t['threadId'] for t in json.loads(result.stdout) if t.get('status') == 'running']
        except (ValueError, TypeError, KeyError):
            failed.append(project)
    return list(dict.fromkeys(found)), failed


def thread_running(thread):
    """True/False from T3; None when T3 cannot say (keep retrying)."""
    result = _run([T3_CLI, 'thread', 'show', thread, '--json', '--messages', '0'], timeout=15)
    try:
        if result is None or result.returncode != 0:
            return None
        status = json.loads(result.stdout).get('thread', {}).get('status')
        if status == 'running':
            return True
        return False if status in ('idle', 'completed', 'interrupted', 'failed', 'cancelled', 'rolled_back') else None
    except (AttributeError, ValueError, TypeError):
        return None


DEATH_EXPIRE_SECONDS = 86400
DEATH_PASS_SECONDS = 420  # stop starting sends well before RuntimeMaxSec=600


def _outbox_path(host):
    return host.LOG.parent / 'category-death-outbox.json'


def _outbox_load(path):
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        state = {}
    state.setdefault('outbox', {})
    state.setdefault('done', [])
    return state


def _outbox_save(host, path, state):
    host.atomic_write(path, json.dumps(state, sort_keys=True) + '\n')
    os.chmod(path, 0o600)


def death_queue(host, events, now):
    """Durably queue deaths. Producers call this BEFORE saving their transition state,
    so a crash re-detects the same stable id and the queue dedups it."""
    deaths = [e for e in events if e.get('transition') in DEATHS]
    if not deaths:
        return
    path = _outbox_path(host)
    with host.guard_lock(path):
        state = _outbox_load(path)
        for event in deaths:
            if event['id'] not in state['outbox'] and event['id'] not in state['done']:
                state['outbox'][event['id']] = dict(text=death_text(event), at=now,
                                                    dests=['discord', 'mock', 'agents'], tries={})
        _outbox_save(host, path, state)


def death_spawn(host):
    """Deliver outside the meter run and its locks: one transient user unit at a time."""
    path = _outbox_path(host)
    try:
        if not _outbox_load(path)['outbox']:
            return
    except (OSError, ValueError):
        return
    code = ('import sys; sys.path.insert(0, %r); import claude_usage_boxes as b; b.death_deliver_main()'
            % str(Path(__file__).resolve().parent))
    _run(['systemd-run', '--user', '--collect', '--quiet', '--unit=claude-usage-death-deliver',
          '--property=RuntimeMaxSec=600', 'python3', '-c', code], timeout=10)  # fails if one is running


def death_deliver_main():
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader('claude_usage_host', str(Path(__file__).resolve().parent / 'claude-usage'))
    spec = importlib.util.spec_from_loader('claude_usage_host', loader)
    host = importlib.util.module_from_spec(spec)
    loader.exec_module(host)
    death_deliver(host)


def death_deliver(host, now=None, sender=None, lister=None, running=None):
    """At-least-once delivery. Never holds the outbox lock while sending; a single
    deliverer at a time (non-blocking flock). Required destinations are retried until
    delivered (expired only after a day, kept visible in state['expired'])."""
    import fcntl
    sender = sender or _death_send
    lister = lister or running_threads
    running = running or thread_running
    path = _outbox_path(host)
    with open(str(path) + '.deliver.lock', 'a') as single:
        try:
            fcntl.flock(single, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        now = datetime.now().timestamp() if now is None else now
        started = datetime.now().timestamp()
        with host.guard_lock(path):
            work = copy_json(_outbox_load(path)['outbox'])

        def checkpoint(key, item):
            # Short lock per step: progress survives a kill at RuntimeMaxSec.
            with host.guard_lock(path):
                state = _outbox_load(path)
                if key in state['outbox']:
                    state['outbox'][key] = item
                    _outbox_save(host, path, state)
        for key, item in work.items():
            if 'agents' in item['dests'] or item.get('projects_pending'):
                try:
                    found, failed = lister(item.get('projects_pending') or None) if item.get('projects_pending') else lister()
                except Exception:
                    found, failed = None, None
                if found is not None:
                    known = set(item['dests']) | set(item.get('receipts', {})) | set(item.get('skipped', []))
                    item['dests'] = [d for d in item['dests'] if d != 'agents'] + [
                        'agent:' + t for t in found if t != NOTICE_THREAD and 'agent:' + t not in known]
                    item['projects_pending'] = failed
                    checkpoint(key, item)
            for dest in list(item['dests']):
                if dest == 'agents' or datetime.now().timestamp() - started > DEATH_PASS_SECONDS:
                    continue
                tries = item['tries'][dest] = item['tries'].get(dest, 0) + 1
                if sender(dest, item['text'], key):
                    item['dests'].remove(dest)
                    item.setdefault('receipts', {})[dest] = now
                elif dest == 'discord' and tries == DEATH_DISCORD_TRIES and 'dashboard' not in item['dests']:
                    item['dests'].append('dashboard')  # supplement; Discord keeps retrying
                elif dest.startswith('agent:') and running(dest[6:]) is False:
                    item['dests'].remove(dest)
                    item.setdefault('skipped', []).append(dest)
                checkpoint(key, item)
        with host.guard_lock(path):
            state = _outbox_load(path)
            for key, item in work.items():
                if key not in state['outbox']:
                    continue
                if not item['dests'] and not item.get('projects_pending'):
                    state['outbox'].pop(key)
                    state['done'] = (state['done'] + [key])[-200:]
                elif now - item['at'] > DEATH_EXPIRE_SECONDS:
                    state['outbox'].pop(key)
                    state.setdefault('expired', []).append(dict(id=key, undelivered=item['dests'],
                                                                projects_pending=item.get('projects_pending')))
                else:
                    state['outbox'][key] = item
            _outbox_save(host, path, state)
        return state


def copy_json(value):
    return json.loads(json.dumps(value))


def _death_send(dest, text, key):
    if dest == 'discord':
        result = _run(['python3', NOTIFY_DISCORD, text], timeout=30)
        return bool(result and result.returncode == 0 and result.stdout.strip())
    if dest == 'dashboard':
        result = _run(['python3', NOTIFY_DASHBOARD, '--event-key', 'category-death-' + key], stdin='For you: ' + text)
        return bool(result and result.stdout.strip() == 'queued')
    if dest == 'mock':
        if not NOTICE_THREAD:
            return True  # no notice thread configured: nothing to send
        result = _run([T3_CLI, 'thread', 'send', NOTICE_THREAD, '-', '--mode', 'auto'], stdin='[Usage] ' + text)
        return bool(result and result.returncode == 0)
    if dest.startswith('agent:'):
        result = _run([T3_CLI, 'thread', 'send', dest[6:], '-', '--mode', 'steer'], stdin='[Usage] ' + text)
        return bool(result and result.returncode == 0)
    return False


FINAL_PREFLIGHT_SECONDS = 900


def final_preflight(host, policy, now):
    """In the 15 minutes before preparation ends: name every category our own allowance stop still blocks.

    Allowance stops never release
    themselves, so one set before the final window carries into the final runs until a
    human uncap. Rate stops are left out: they are re-checked against the final figures.
    Category-only and notify-only: it sets, clears and grants nothing; uncap stays human-only.
    """
    import claude_usage_messages as human
    prep = (policy or {}).get('preparation')
    if not prep or not prep['end'] - FINAL_PREFLIGHT_SECONDS <= now < prep['end']:
        return []
    latches = host.guard_state(host.GUARD_STATE)['latches'].values()
    found = {}
    for item in latches:
        if re.search(r'\d+-minute rate$', item.get('box', '')):
            continue
        for category in latch_categories(item):
            found.setdefault(category, item)
    events = []
    for category in CATEGORIES:
        item = found.get(category)
        if not item:
            continue
        box = item.get('box') or 'usage box'
        if isinstance(item.get('used'), (int, float)) and isinstance(item.get('allowed'), (int, float)):
            used = f"{item['used']:.2f} of {item['allowed']:.2f} points ({box})"
        elif isinstance(item.get('at'), (int, float)):  # the notice parser needs a digit in Used
            used = f"amount not recorded; stopped since {datetime.fromtimestamp(item['at'], human.ET):%a %H:%M} ET ({box})"
        else:
            used = f'amount not recorded; 1 stop ({box})'
        at = f"{datetime.fromtimestamp(prep['end'], human.ET):%H:%M}"
        line = human.pack('STOPPED', LABELS[category],
                          f'our usage stop will still block the final runs at {at}; it never releases itself',
                          used, human.end_text(prep['end'], now),
                          f'if the final should have it, at a terminal run  uncap  before {at}')
        events.append((f'final-preflight:{category}:{int(prep["end"])}', 4, line, 1800, False))
    return events


SPOKEN_CATEGORY = {'fable': 'Fable', 'non-fable': 'Opus', 'codex': 'Codex'}


def decile_items(items):
    """Before preparation ends, the all-usage prep total; from the final run, its own box from zero."""
    run = [r for r in items if not r.get('prep_total') and not r['name'].startswith('last ')]
    prep = [r for r in items if r['name'].startswith('Preparation total, all usage')]
    return run or prep


def decile_voice(host, items, now, speak=None):
    """Speak once each time a category crosses another 10% of its box. First sight seeds silently."""
    path = host.LOG.parent / 'decile-voice.json'
    with host.guard_lock(path):
        state = host.read_state(path)
        parts, seen = [], set()
        for reading in decile_items(items):
            if not reading['allowed']:
                continue
            key = f"{reading['category']}:{reading['name']}:{reading['start']}"
            seen.add(key)
            decile = min(10, int(10 * reading['used'] / reading['allowed'] + 1e-9))
            last = state.get(key)
            if last is not None and decile > last:
                where = '' if reading.get('prep_total') else ', final run'
                parts.append(f"{SPOKEN_CATEGORY[reading['category']]}, {decile * 10} percent{where}.")
            state[key] = decile
        state = {k: v for k, v in state.items() if k in seen}
        host.atomic_write(path, json.dumps(state, sort_keys=True) + '\n')
    if parts:
        (speak or host.say_once)(' '.join(parts))
    return parts


def process(host, policy, rows, history, now, warn=False):
    ledger_path = host.LOG.parent / 'category-ledger.json'
    with host.guard_lock(ledger_path):
        try:
            ledger = json.loads(ledger_path.read_text())
        except FileNotFoundError:
            ledger = initialize(history, now, policy)
        observe(ledger, rows, now, policy)
        log_steps(host, ledger)
        if warn and policy['approved']:
            # Mark where strict completeness enforcement starts. Logged
            # pre-install consumption remains part of rolling spending.
            ledger.setdefault('activated_at', now)
        items = readings(policy, ledger, now)
        host.atomic_write(ledger_path, json.dumps(ledger, sort_keys=True) + '\n')
        if warn:
            events = enforce(host, policy, ledger, items, now)
            events += warnings(items, ledger, now, policy)
            events += account_events(host, rows, items, ledger, now)
            # Whole-category exhaustion is actionable on the orchestrator thread;
            # Individual meters remain accounting inputs only, even near 100%.
            status = category_status(policy, rows, now)
            for producer in (lambda: recovery_transitions(host, status, now), lambda: cap_transitions(host, now)):
                try:
                    producer()  # each queues its deaths durably before saving its own state
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # Recovery/death triggers are advisory; never block metering or guards.
            try:
                events += final_preflight(host, policy, now)
            except (OSError, ValueError, KeyError, TypeError):
                pass  # advisory; never block metering or guards
            for category in CATEGORIES:
                if status[category][0]:
                    line = f'{LABELS[category]}: OUT OF USAGE on every account. A human must redeem a reset or wait for replenishment; box stops still need UNCAP.'
                    events.append(('category-out:' + category, 4, line, 1800, False))
            deliver_current(host, events, now)
            try:
                decile_voice(host, items, now)
            except Exception:
                pass  # a spoken reminder never blocks metering or guards
    if warn:
        try:
            death_spawn(host)  # after the ledger lock; delivery runs in its own transient unit
        except Exception:
            pass
    return items


def historical_test(host, policy, history, rows, path, now):
    """Explicit user-authorized real-category test; never spends new usage."""
    spec = tomllib.loads(path.read_text())
    category = spec['category']
    if category != 'non-fable':
        raise ValueError('the authorized live test is non-Fable only')
    start, end = stamp(spec['start']), stamp(spec['end'])
    amount = finite(spec['allow'], True)
    if not start < now < end:
        raise ValueError('test must include historical usage and the present')
    ledger_path = host.LOG.parent / 'category-ledger.json'
    with host.guard_lock(ledger_path):
        historical = initialize(history, now, policy)
        observe(historical, rows, now, policy)
        used = spent(historical, category, start, now)
        if used < amount:
            raise ValueError('not enough verified historical spending to trip the test')
        try:
            ledger = json.loads(ledger_path.read_text())
        except FileNotFoundError:
            ledger = historical
            host.atomic_write(ledger_path, json.dumps(ledger, sort_keys=True) + '\n')
        reading = dict(category=category, name=spec['name'], start=start, end=end,
                       used=used, allowed=amount, incomplete=False, approved=True, run=True)
        events = enforce(host, policy, ledger, [reading], now)
        events = [(key, level, line + ' Release: claude-usage --uncap non-fable; type UNCAP.', interval, trip)
                  for key, level, line, interval, trip in events]
        deliver_current(host, events, now)
        print(message(reading, now, True))
        print("Human release: claude-usage --uncap non-fable (type UNCAP in the terminal).")
    return 0


def usage_record(record, provider, previous):
    """Extract counters only. Never retain prompts, tools, paths or credentials."""
    if provider == 'claude':
        message = record.get('message') or {}
        usage = message.get('usage')
        if record.get('type') != 'assistant' or not usage or not message.get('id'):
            return None
        model = message.get('model', '').lower()
        if not model.startswith('claude-'):
            return None
        category = 'fable' if 'fable' in model else 'non-fable'
        # Historical fitted signal, not a provider billing formula. Rate alerts
        # are estimates only and NEVER feed hard-stop spending.
        hour_write = (usage.get('cache_creation') or {}).get('ephemeral_1h_input_tokens', 0) or 0
        score = (usage.get('input_tokens', 0) or 0) + 1.25 * (usage.get('cache_creation_input_tokens', 0) or 0) + .75 * hour_write
        score += .1 * (usage.get('cache_read_input_tokens', 0) or 0) + 5 * (usage.get('output_tokens', 0) or 0)
        identity = 'claude:' + message['id']
    else:
        payload = record.get('payload') or {}
        if payload.get('type') != 'token_count':
            return None
        info = payload.get('info') or {}
        usage = info.get('total_token_usage')
        if not usage:
            return None
        category, identity = 'codex', 'codex-total'
        score = ((usage.get('input_tokens', 0) or 0) - .9 * (usage.get('cached_input_tokens', 0) or 0)
                 + 5 * (usage.get('output_tokens', 0) or 0))
    if not math.isfinite(score) or score < 0:
        return None
    old = previous.get(identity)
    if provider == 'codex' and (old is None or score < old):
        # For a newly discovered/truncated/compacted session, only the last
        # actual response is new evidence; never charge its lifetime total.
        usage = info.get('last_token_usage') or {}
        delta = max(0., (usage.get('input_tokens', 0) or 0) - .9 * (usage.get('cached_input_tokens', 0) or 0)
                    + 5 * (usage.get('output_tokens', 0) or 0))
    else:
        delta = max(0., score - (old or 0))
    previous[identity] = max(old or 0, score) if provider == 'claude' else score
    if not delta:
        return None
    return stamp(record['timestamp'].replace('Z', '+00:00')), category, delta


def sample_routes(state, policy, now, roots=None):
    """Read selection metadata only; account preference never labels a request."""
    roots = roots or {'claude': Path.home() / '.cli-proxy-api-claude/auth/logs/main.log',
                      'codex': Path.home() / '.cli-proxy-api/auth/logs/main.log'}
    routes = state.setdefault('routes', [])
    for provider, path in roots.items():
        try:
            stat = path.stat()
            cursor = state.setdefault('route_files', {}).setdefault(provider, dict(offset=max(0, stat.st_size-2097152), inode=stat.st_ino))
            if cursor['inode'] != stat.st_ino or cursor['offset'] > stat.st_size:
                cursor.update(offset=0, inode=stat.st_ino)
            with path.open('rb') as stream:
                stream.seek(cursor['offset'])
                for _ in range(20000):
                    raw = stream.readline(16384)
                    if not raw or not raw.endswith(b'\n'):
                        break
                    cursor['offset'] = stream.tell()
                    text = raw.decode('utf-8', errors='replace')
                    model = re.search(r'\bmodel=([^\s|]+)', text)
                    auth = re.search(r'\bauth=([^\s|]+)', text)
                    stamp_text = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', text)
                    if not model or not auth or not stamp_text:
                        continue
                    accounts = [source['email'] for source in policy['sources'] if source['provider'] == provider and source['email'] in auth.group(1)]
                    if len(accounts) != 1:
                        continue
                    at = datetime.strptime(stamp_text.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('America/New_York')).timestamp()
                    if now-7200 <= at <= now+5:
                        routes.append([at, provider, model.group(1), accounts[0]])
        except (OSError, ValueError):
            continue
    state['routes'] = [entry for entry in routes if now-7200 <= entry[0] <= now+5]


def sample_token_record(record, provider, cursor, state, now):
    """Token types only, deduplicated by message or session cumulative totals."""
    import token_cost_model as fitted
    if provider == 'claude':
        message = record.get('message') or {}
        if record.get('type') != 'assistant' or not message.get('id') or not message.get('usage'):
            return
        usage, model = message['usage'], str(message.get('model', '')).lower()
        key = provider + ':' + message['id']
        previous = state.setdefault('token_counters', {})
    else:
        payload = record.get('payload') or {}
        if record.get('type') == 'turn_context':
            cursor['token_model'] = payload.get('model')
            return
        if payload.get('type') != 'token_count':
            return
        info = payload.get('info') or {}
        usage, model = info.get('total_token_usage'), cursor.get('token_model', '')
        if not usage:
            return
        key, previous = 'codex', cursor.setdefault('token_previous', {})
    if not model.startswith('claude-' if provider == 'claude' else 'gpt-'):
        state['token_since'] = now
        return
    counts = fitted._counts(usage, provider)
    old = previous.get(key)
    if provider == 'codex' and (old is None or any(counts[k] < old[k] for k in counts)):
        delta = fitted._counts(info.get('last_token_usage') or {}, provider)
    else:
        delta = {k:max(0., value-(old or {}).get(k,0.)) for k,value in counts.items()}
    previous[key] = {k:max(value,(old or {}).get(k,0.)) for k,value in counts.items()} if provider == 'claude' else counts
    if not any(delta.values()):
        return
    at = stamp(record['timestamp'].replace('Z', '+00:00'))
    category = 'codex' if provider == 'codex' else 'fable' if 'fable' in model else 'non-fable'
    candidates = {entry[3] for entry in state.get('routes', [])
                  if entry[1] == provider and entry[2] == model and at-180 <= entry[0] <= at+2}
    account = next(iter(candidates)) if len(candidates) == 1 else None
    safe = dict(provider=provider, account=account, model=model, input=delta['input'], output=delta['output'],
                cache_read=delta['cache_read'], cache_write=delta['cache_write']+delta['cache_write_1h'],
                cache_write_1h=delta['cache_write_1h'])
    state.setdefault('token_records', []).append(dict(time=at, category=category, usage=safe))


def sample_sessions(state, roots, now):
    state.setdefault('token_since', now)
    if now - state.get('token_last_sampled', now) > 90:
        state['token_since'] = now  # resumed sampler cannot certify its unseen gap
    state['token_last_sampled'] = now
    state['token_complete'] = True
    files = state.setdefault('files', {})
    events = state.setdefault('events', [])
    for provider, root in roots.items():
        if not root.is_dir():
            state['token_complete'] = False
            state['token_since'] = now
            continue
        for path in root.rglob('*.jsonl'):
            stat = path.stat()
            if stat.st_mtime < now - 7200:
                old_id = hashlib.sha256(str(path).encode()).hexdigest()
                signature = [stat.st_ino, stat.st_size]
                ignored = state.setdefault('token_ignored_old', {})
                if ignored.get(old_id) != signature and old_id not in files:
                    state['token_since'] = now
                ignored[old_id] = signature
                continue
            # Hashed paths are enough to resume; state contains usage metadata only.
            identity = hashlib.sha256(str(path).encode()).hexdigest()
            new_file = identity not in files
            cursor = files.setdefault(identity, dict(offset=max(0, stat.st_size - 1048576), inode=stat.st_ino, previous={}))
            if new_file and cursor['offset'] > 0:
                state['token_since'] = now  # skipped history cannot certify an older window
            if cursor['inode'] != stat.st_ino or cursor['offset'] > stat.st_size:
                state['token_since'] = now
                cursor.update(offset=0, inode=stat.st_ino, previous={})
                cursor.pop('skipping', None)
            with path.open('rb') as source:
                source.seek(cursor['offset'])
                # One bounded MB per file per pass. Partial JSONL records are retried.
                limit = source.tell() + 1048576
                while source.tell() < limit:
                    raw = source.readline(1048576)
                    if not raw:
                        break
                    if not raw.endswith(b'\n'):
                        state['token_since'] = now
                        state['token_complete'] = False
                        if len(raw) == 1048576 or cursor.get('skipping'):
                            cursor['offset'] = source.tell()
                            cursor['skipping'] = True
                        break
                    cursor['offset'] = source.tell()
                    if cursor.pop('skipping', False):
                        state['token_since'] = now
                        continue
                    if b'"usage"' not in raw and b'"token_count"' not in raw and b'"turn_context"' not in raw:
                        continue
                    try:
                        record = json.loads(raw)
                        try:
                            sample_token_record(record, provider, cursor, state, now)
                        except (ImportError, ValueError, TypeError, KeyError, OverflowError):
                            state['token_since'] = now
                        previous = state.setdefault('claude_counters', {}) if provider == 'claude' else cursor['previous']
                        point = usage_record(record, provider, previous)
                    except (ValueError, TypeError, KeyError, OverflowError):
                        state['token_since'] = now
                        continue
                    if point and now - 7200 <= point[0] <= now + 5:
                        events.append(list(point))
            if cursor['offset'] < stat.st_size:
                state['token_complete'] = False
            cursor['seen'] = now
            # Claude message IDs only need to survive the recent active tail.
            if len(cursor['previous']) > 5000:
                cursor['previous'] = dict(list(cursor['previous'].items())[-2500:])
    state['token_records'] = [entry for entry in state.get('token_records', []) if now-8*86400 <= entry['time'] <= now+5]
    state['files'] = {k: v for k, v in files.items() if now - v.get('seen', now) < 86400}
    state['events'] = [e for e in events if now - 7200 <= e[0] <= now + 5]
    if len(state.get('claude_counters', {})) > 10000:
        state['claude_counters'] = dict(list(state['claude_counters'].items())[-5000:])
    return state


def rolling_eta(reading, ledger, extra, rate, now):
    """Earliest cap crossing, subtracting historical use as it rolls out."""
    if rate <= 0:
        return None
    horizon = reading['end'] - now
    base = max(reading['start'], ledger['forgiven'].get(reading['category'], -math.inf))
    points = [(e[0], e[2]) for e in ledger['events']
              if e[1] == reading['category'] and base < e[0] <= now]
    points += extra
    used = sum(amount for _, amount in points)
    elapsed = 0.
    expirations = sorted((at + horizon - now, amount) for at, amount in points)
    for boundary, amount in expirations + [(horizon, 0.)]:
        if boundary < elapsed:
            continue
        wait = max(0., (reading['allowed'] - used) / rate)
        if elapsed + wait < boundary - 1e-8:
            return elapsed + wait
        used += rate * (boundary - elapsed) - amount
        elapsed = boundary
    return None


def rate_events(policy, ledger, samples, now):
    run = active_run(policy, now)
    candidates = {}
    for reading in readings(policy, ledger, now):
        if reading.get('prep_total') or reading['name'].startswith('Preparation total'):
            continue  # Preparation-total notices are total-budget levels only.
        category = reading['category']
        factor = policy['rate_points_per_million'][category] / 1e6
        begin = max(ledger['forgiven'].get(category, -math.inf),
                    reading['start'] if run else -math.inf)
        recent = sum(e[2] for e in samples['events'] if e[1] == category and max(begin, now - 300) < e[0] <= now) * factor
        elapsed = min(300, max(1., now - begin))
        rate = recent / elapsed
        fresh = [s['time'] for key, s in ledger['sources'].items()
                 if key.startswith('codex:' if category == 'codex' else 'claude:')]
        metered_at = min(fresh) if fresh else -math.inf
        extra = [(e[0], e[2] * factor) for e in samples['events']
                 if e[1] == category and max(begin, metered_at) < e[0] <= now]
        used = reading['used'] + sum(amount for _, amount in extra)
        remaining = max(0., reading['allowed'] - used)
        crossing = (remaining / rate if run and rate > 0 else
                    rolling_eta(reading, ledger, extra, rate, now) if not run else None)
        if crossing is None or crossing >= reading['end'] - now or (not run and crossing > 3600):
            continue
        previous = candidates.get(category)
        if previous is None or crossing < previous[0]:
            candidates[category] = crossing, reading, used, recent, elapsed
    events = []
    for category, (crossing, reading, used, recent, elapsed) in candidates.items():
        import claude_usage_messages as human
        installed_rate = notice_rate(policy, ledger, category, now)
        planned_rate = (installed_rate / 3600 if installed_rate is not None else
                        reading['allowed'] / max(1, reading['end'] - reading['start']))
        window_name = reading['name'] + ('' if run else ' rolling window')
        display = dict(reading, used=used, name=window_name + ': Five-minute rate projection')
        line = human.box_notice(display, now, pace=(recent, planned_rate * elapsed, elapsed / 60), forecast=now+crossing)
        line += ' | Passive warning; stops nothing'
        events.append(('rate:' + category, 2, line, 900, False))
    return events


def passive(host, policy, now, roots=None):
    path = host.LOG.parent / 'passive-rate.json'
    ledger_path = host.LOG.parent / 'category-ledger.json'
    with host.guard_lock(path):
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            state = dict(files={}, events=[])
        roots = roots or {'claude': host.HOME / '.claude/projects', 'codex': host.HOME / '.codex/sessions'}
        sample_routes(state, policy, now)
        sample_sessions(state, roots, now)
        host.atomic_write(path, json.dumps(state, sort_keys=True) + '\n')
        # Subordinate test-mock counters; parent usage/enforcement is unchanged.
        try:
            import test_mock_usage
            test_mock_usage.collect(host, policy, now)
        except Exception:
            # A subordinate display must never skip the parent warning/guard.
            print('claude-usage: test-mock counters unavailable; parent guards continue')
        if preparation_active(policy, now):
            ladder_tick(host, policy, state, now)
            return
        with host.guard_lock(ledger_path):
            try:
                ledger = json.loads(ledger_path.read_text())
            except FileNotFoundError:
                return
            events = rate_events(policy, ledger, state, now)
        deliver_current(host, events, now, host.LOG.parent / 'rate-current-notices.json')



def billing_notice(host, line, now):
    """Keep the existing Modal check, using the same human destinations."""
    import claude_usage_messages as human
    path = host.LOG.parent / 'modal-human-notices.json'
    if line is None:
        saved = host.read_state(path)
        events = [(key, item['level'], item['line'], math.inf, False)
                  for key, item in saved.get('records', {}).items() if now-item['at'] <= 1800]
        if events or path.exists():
            deliver_current(host, events, now, path)
        return True
    amount = re.search(r'\$([0-9.]+)', line)
    if amount is None:
        append_alert(line, alert_path(host))
        return False
    date = datetime.fromtimestamp(now, ZoneInfo('America/New_York'))
    end = date.replace(year=date.year+(date.month==12), month=date.month % 12+1,
                       day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    body = human.pack('WARNING', 'Usage tool', 'Modal billing needs attention',
                      '$' + amount.group(1) + ' (' + ' '.join(line.splitlines()) + ')',
                      'billing month ends ' + human.end_text(end, now),
                      'review Modal billing; spending over $2 needs approval')
    identity = hashlib.sha256(line.encode()).hexdigest()[:16]
    deliver_current(host, [('billing:modal:' + identity, 3, body, math.inf, False)], now, path)
    return True


def check_health(host, kind, error, now):
    """Two consecutive failed checks before human alert; one recovery."""
    import claude_usage_messages as human
    path = host.LOG.parent / 'check-health.json'
    notice_path = host.LOG.parent / ('health-' + kind + '-notices.json')
    with host.guard_lock(path):
        saved = host.read_state(path)
        status = saved.setdefault(kind, dict(failures=0, reported=False))
        event = None
        if error is not None:
            status['failures'] += 1
            status['recovering'] = False
            if status['failures'] >= 2:
                description = ('box file could not be read' if kind == 'configuration' else 'usage totals could not be checked')
                detail = type(error).__name__
                if isinstance(error, tomllib.TOMLDecodeError):
                    detail += ': ' + str(error)
                event = human.pack('WARNING', 'Usage tool', description + ' (' + detail + ')',
                    f"{status['failures']} consecutive failed checks", 'last check ' + human.end_text(now, now),
                    'nothing needed unless this repeats; existing stops stay as they were')
                status['reported'] = True
        else:
            if status['reported'] or status.get('recovering'):
                status['recovering'] = True
                event = human.pack('NOTE', 'Usage tool', 'usage checks recovered', '0 failed checks',
                                   'checked ' + human.end_text(now, now), 'nothing needed')
            status.update(failures=0, reported=False)
        host.atomic_write(path, json.dumps(saved, sort_keys=True) + '\n')
    if event:
        deliver_current(host, [('tool:' + kind + (':failed' if error else ':recovered'), 3 if error else 1,
                               event, math.inf, False)], now, notice_path)


def cli(host, args, clock=None):
    try:
        if getattr(args, 'hard_continue', False) or getattr(args, 'un_hard_continue', None):
            return override_command(host, getattr(args, 'un_hard_continue', None))
        policy = load(args.boxes)
        now_dt = clock() if clock else datetime.now(host.ET)
        now = now_dt.timestamp()
        if getattr(args, 'soft_uncap', None):
            category, minutes = args.soft_uncap
            return soft_uncap(host, policy, category, float(minutes), now=now)
        if args.uncap:
            category = uncap_category(args.uncap)
            ledger_path = host.LOG.parent / 'category-ledger.json'
            legacy_only = ':' in args.uncap and not ledger_path.exists()
            ok = (uncap(host, category, ledger_path, policy) if category and not legacy_only else host.uncap_pool(args.uncap))
            print('Category reopened; rate windows restart. Preparation total remains spent.' if ok and policy.get('preparation') else
                  'Category reopened with a fresh allowance.' if ok else 'No category reopened; human terminal confirmation required.')
            return 0 if ok else 2
        if args.accept_defaults:
            # This only approves normal policy. Each run retains separate approval.
            text = args.boxes.read_text()
            host.atomic_write(args.boxes, text.replace('approved = false', 'approved = true', 1))
            policy = load(args.boxes)
        if args.warn or getattr(args, 'meter_check', False) or args.passive:
            check_health(host, 'configuration', None, now)
        if args.passive:
            try:
                passive(host, policy, now)
            except (OSError, ValueError, KeyError, TypeError) as error:
                check_health(host, 'passive', error, now)
                return 2
            check_health(host, 'passive', None, now)
            return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('claude-usage: category configuration/state unavailable; check local file.', file=host.sys.stderr)
        if args.warn or args.passive or getattr(args, 'meter_check', False):
            check_health(host, 'configuration', error, datetime.now().timestamp())
        return 2
    rows, problems = [], []
    for name, getter in (('claude', lambda: host.parse(host.fetch())),
                         ('codex', lambda: host.parse_codex(host.fetch(host.CODEX_URL, host.CODEX_KEY)))):
        try:
            rows += getter()
        except (OSError, ValueError):
            problems.append(name)
    meter_check = getattr(args, 'meter_check', False)
    if args.log or args.warn:
        host.prepare_log()
        with open(host.LOG, 'a') as out:
            out.write(host.tsv(rows, now_dt))
    history = host.read_log(host.LOG)
    if args.test_box:
        try:
            return historical_test(host, policy, history, rows, args.test_box, now)
        except (OSError, ValueError, KeyError, TypeError):
            print('claude-usage: historical test could not be completed; inspect the local guard.', file=host.sys.stderr)
            return 2
    failed = False
    try:
        items = process(host, policy, rows, history, now, args.warn or meter_check)
        if args.warn or meter_check:
            check_health(host, 'accounting', None, now)
            billing_notice(host, None, now)
    except (OSError, ValueError, KeyError, TypeError) as error:
        failed = True
        # Preserve/reapply durable guards. Do not stop unrelated categories just
        # because a provider's management reader or a report is unavailable.
        with host.guard_lock(host.GUARD_STATE):
            guard = host.guard_state(host.GUARD_STATE)
            host.apply_hard_stops(guard, host.GUARD_STATE)
        items = []
        if args.warn or meter_check:
            check_health(host, 'accounting', error, now)
        print('claude-usage: category accounting unavailable', file=host.sys.stderr)
    if args.warn or meter_check:
        release_path = host.LOG.parent / 'hard-release-notices.json'
        if release_path.exists():
            deliver_current(host, None, now, release_path)
        for name in problems:
            deliver_current(host, [('proxy:' + name, 3, f'{name.title()} readings unavailable; check the local usage guard.', 600, False)], now,
                    host.LOG.parent / ('proxy-' + name + '-notices.json'))
        if args.warn:
            host.modal_check(now_dt, thread_sender=lambda line: billing_notice(host, line, now))
    elif not args.log:
        for reading in items:
            print(message(reading, now))
        ledger = json.loads((host.LOG.parent / 'category-ledger.json').read_text())
        if policy.get('preparation'):
            print('\n'.join(preparation_summary(policy, ledger, now)))
        override = override_state(host).get('disabled', {})
        for category in CATEGORIES:
            credit = soft_credit(ledger, category)
            if credit:
                print(f'{LABELS[category]}: soft credit {credit:.4f} points added to category limits; counted spending retained.')
        for run in policy.get('runs', []):
            if not run.get('remaining'):
                print(f"Mock multiplier: {run.get('scale', 1):g} ({run['name']}); choose explicitly before each mock.")
        print('Hard-continue: ' + (', '.join(LABELS[c] for c in override) if override else 'off'))
        ledger = json.loads((host.LOG.parent / 'category-ledger.json').read_text())
        for key, source in ledger['sources'].items():
            if source.get('carried'):
                print(key + ': using last known meter for ' + ', '.join(source['carried']))
        guard = host.guard_state(host.GUARD_STATE)
        blocked = {v.get('category') for v in guard['latches'].values() if v.get('category')}
        for category in sorted(blocked):
            print(f'{LABELS[category]}: BLOCKED on every account until a human runs claude-usage --uncap {category}.')
        for row in rows:
            for window in ('7d', 'fable', '5h'):
                value = host.pool_points(row, window)
                if value is not None:
                    name = 'Fable' if window == 'fable' else 'weekly' if window == '7d' else '5-hour'
                    print(f'{row.get("provider", "claude").title()} {row["email"]} {name} meter: {value:g}% used; resets {host.when(host.meter_reset(row, window))}.')
        try:
            print(host.render_modal(host.fetch_modal()))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            print('Modal: unavailable')
    return 1 if failed or problems else 0


def preparation_active(policy, now):
    prep = policy.get('preparation')
    return bool(prep and prep['start'] <= now < prep['final_end'])


def preparation_snapshot(policy, ledger, now):
    """Freeze C and P once. Human UNCAP never refunds the preparation total."""
    prep = policy['preparation']
    identity = str(prep['start'])
    saved = ledger.setdefault('preparation', {}).get(identity)
    if saved is None:
        if now >= prep['end']:
            raise ValueError('preparation capacity must be captured before final')
        capacity = remaining_capacity(policy, ledger, prep['end'], now)
        saved = dict(at=now, C=capacity, P={c: 2 * capacity[c] / 3 for c in CATEGORIES},
                     resets={c: sum(s.get('fable_banked' if c == 'fable' else 'banked', 0)
                                    for s in policy['sources'] if (s['provider'] == 'codex') == (c == 'codex'))
                             if c != 'non-fable' else 0 for c in CATEGORIES})
        ledger['preparation'][identity] = saved
    return saved


def add_weekly_reset_capacity(policy, ledger, email, count, approval_id, now):
    """Apply one explicitly approved grant to frozen C/P, never recorded spend."""
    if type(count) is not int or count <= 0:
        raise ValueError('positive whole weekly reset count required')
    source = next(s for s in policy['sources'] if s['provider'] == 'claude' and s['email'] == email)
    if source.get('weekly_banked', 0) < count:
        raise ValueError('weekly inventory does not cover approved grant')
    saved = ledger.get('preparation', {}).get(str(policy['preparation']['start']))
    if saved is None:
        raise ValueError('existing frozen preparation snapshot required; no rebaseline')
    grants = saved.setdefault('capacity_adjustments', {})
    grant = dict(email=email, weekly_resets=count, category_points=100 * source['weight'] * count)
    if approval_id in grants:
        if grants[approval_id] != grant:
            raise ValueError('approval already applied with different capacity')
        return saved
    for category in ('fable', 'non-fable'):
        saved['C'][category] += grant['category_points']
        saved['P'][category] = saved['C'][category] * 2 / 3
    grants[approval_id] = grant
    return saved


# The remaining boxes count only usage that is neither automock nor the real final run, and
# are sized to 100% minus the automock share minus the final run's share. With [preparation] split_automock = true the preparation
# total and rate ladder size to (1 - automock share) of P (P already excludes the final
# third) and count only non-automock usage. Automock usage is the test-mock collector's
# token estimate; it is subtracted conservatively (never more than measured mock use).
AUTOMOCK_CONFIG = Path.home() / '.config/claude-usage/test-mocks.json'
AUTOMOCK_LEDGER = Path.home() / '.local/state/claude-usage/test-mock-ledger.json'
# Meter/token calibration from mocks 3-5: Codex meters ran 1.7-3.5x the token estimate
# (subtracting 1x undercounts mocks: safe); Fable meters ran 0.6-0.74x (subtract 0.6x).
AUTOMOCK_SUBTRACT = {'fable': .6, 'non-fable': 1., 'codex': 1.}
_automock_cache = {}
AUTOMOCK_APPROVED_SHARE = .25  # test mocks use 25%


def automock_split(policy):
    if not policy.get('preparation', {}).get('split_automock'):
        return None
    try:
        config = json.loads(AUTOMOCK_CONFIG.read_text())
        share = finite(config['share'], True)
        if not 0 < share < 1:
            raise ValueError('automock share')
        return dict(share=share, start=stamp(config['start']), end=stamp(config['end']),
                    ppm=config.get('points_per_million', policy['rate_points_per_million']))
    except (OSError, ValueError, KeyError, TypeError):
        # Never enlarge limits on a broken config: keep the approved share, subtract nothing.
        return dict(share=AUTOMOCK_APPROVED_SHARE, start=math.inf, end=-math.inf, ppm=None)


def automock_points(policy, category, lo, hi):
    """Conservative automock points in (lo, hi]; 0 when unknown (counts against other)."""
    split = automock_split(policy)
    if not split:
        return 0.
    try:
        stat = AUTOMOCK_LEDGER.stat()
        key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if _automock_cache.get('key') != key:
            events = json.loads(AUTOMOCK_LEDGER.read_text()).get('events', [])
            _automock_cache.clear()
            _automock_cache.update(key=key, events=[e for e in events if isinstance(e, list) and len(e) == 3])
        lo, hi = max(lo, split['start']), min(hi, split['end'])
        if split['ppm'] is None or hi <= lo:
            return 0.
        tokens = sum(float(e[2]) for e in _automock_cache['events'] if e[1] == category and lo < float(e[0]) <= hi)
        return max(0., tokens * float(split['ppm'][category]) / 1e6 * AUTOMOCK_SUBTRACT[category])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.


def other_share(policy):
    split = automock_split(policy)
    return 1. - split['share'] if split else 1.


def prep_readings(policy, ledger, now):
    prep = policy['preparation']
    if not prep['start'] <= now < prep['end']:
        return []
    saved = preparation_snapshot(policy, ledger, now)
    items = []
    for category in CATEGORIES:
        # Do not use spent(): rate/window forgiveness must not eat the reserve.
        total_start = max(saved['at'], ledger.get('cap_restart', {}).get(category, -math.inf))
        used = box_estimate(policy, ledger, category, total_start, now)
        used = max(0., used - automock_points(policy, category, total_start, now))
        relevant = [s for s in policy['sources'] if (s['provider'] == 'codex') == (category == 'codex')]
        coverage = max(saved['at'], ledger['forgiven'].get(category, -math.inf))
        incomplete = any(e[1] == category and coverage < e[0] <= now for e in ledger['gaps'])
        for source in relevant:
            current = ledger['sources'].get(':'.join((source['provider'], source['email'])))
            if not current or now - current['time'] > 1200:
                incomplete = True
        if automock_split(policy):
            # The aggregate guard stays: all usage, mocks included, against all of P.
            items.append(dict(category=category, name='Preparation total, all usage (final third reserved)',
                              start=saved['at'], end=prep['end'], allowed=saved['P'][category] + soft_credit(ledger, category, total_start, now),
                              used=box_estimate(policy, ledger, category, total_start, now),
                              soft_credit=soft_credit(ledger, category, total_start, now),
                              run=True, approved=policy['approved'], incomplete=incomplete, prep_total=True))
        items.append(dict(category=category, name='Preparation total (final third reserved)',
                          start=saved['at'], end=prep['end'], allowed=saved['P'][category] * other_share(policy) + soft_credit(ledger, category, total_start, now), used=used,
                          soft_credit=soft_credit(ledger, category, total_start, now),
                          run=True, approved=policy['approved'], incomplete=incomplete, prep_total=True))
    return items


def rate_context(policy, ledger, now):
    prep = policy['preparation']
    saved = preparation_snapshot(policy, ledger, now)
    run = active_run(policy, now)
    final = now >= prep['end']
    if final:
        if not run or not run.get('remaining'):
            raise ValueError('final run box missing')
        readings(policy, ledger, now)  # freeze final opening capacity
        opening = ledger['runs'][run_id(run)]
        rates = {c: opening['allow'][c] / ((run['end'] - run['start']) / 3600) for c in CATEGORIES}
        start = opening['baseline']
        scale = 1.
    else:
        scale = run.get('scale', 1.) if run else 1.
        rates = {c: saved['P'][c] * other_share(policy) / prep['active_hours'] * scale for c in CATEGORIES}
        # A mock's scaled ladder starts at its opening baseline, never charges
        # earlier unscaled prep at the mock's new rate.
        if run:
            readings(policy, ledger, now)
            start = ledger['runs'][run_id(run)]['baseline']
        else:
            last_end = max((r['end'] for r in policy.get('runs', []) if r['end'] <= now), default=saved['at'])
            start = max(saved['at'], last_end)
    approved = policy['approved'] and (run is None or run['approved'])
    return dict(R=rates, start=start, final=final, scale=scale, approved=approved,
                name=run['name'] if run else 'Preparation')


def notify_levels(minutes, stop):
    _, fixed, default = next(item for item in RATE_LADDER if item[0] == minutes)
    levels = list(fixed)
    # Decimal keeps e.g. the0.05 step exact and never emits the new stop itself.
    step = Decimal(str(default)) - Decimal(str(fixed[-1]))
    value, target = Decimal(str(default)), Decimal(str(stop))
    while value < target:
        levels.append(float(value))
        value += step
    return tuple(levels)


def confirmed_window(ledger, category, start, now, policy=None):
    """A lower bound from complete meter intervals wholly inside the window.

    Never assign a10-minute interval's spending to its final minute, and never
    use interpolated tokens or a reset discontinuity for a rate hard stop.
    """
    totals = {}
    for interval in ledger.get('meter_intervals', []):
        if interval[2] == category and start <= interval[0] and interval[1] <= now:
            totals[interval[4]] = totals.get(interval[4], 0.) + interval[3]
    if policy is None:
        return sum(totals.values())
    weights = {':'.join((s['provider'], s['email'])): s['weight'] for s in policy['sources']}
    # Endpoint rounding can inflate a gauge change by one meter point. For
    # 2A-F its range is three points. Never hard-stop a small NF box because
    # the shared meter rounded upward before the Fable meter caught up.
    uncertainty = 3 if category == 'non-fable' else 1
    return sum(max(0., amount - uncertainty * weights[source]) for source, amount in totals.items() if source in weights)


def meter_quantum(policy, category):
    weights = [s['weight'] for s in policy['sources'] if (s['provider'] == 'codex') == (category == 'codex')]
    return max(weights, default=0.) * (2 if category == 'non-fable' else 1)


def five_minute_stop(policy, context, category):
    stop = policy['preparation'].get('stops', {}).get('5', 3.)
    threshold = context['R'][category] * 5 / 60 * stop
    return threshold >= meter_quantum(policy, category) - 1e-10


def level_crossing(state, key, multiple, threshold, lowest, minutes, now, epoch):
    """One crossing; re-arm only after a full window continuously below lowest."""
    saved = state.setdefault(key, dict(fired=False, below_since=None, epoch=epoch))
    if saved.get('epoch') != epoch:
        saved.update(fired=False, below_since=None, epoch=epoch)
    if multiple < lowest - 1e-10:
        if saved['below_since'] is None:
            saved['below_since'] = now
        if now - saved['below_since'] >= minutes * 60:
            saved['fired'] = False
    else:
        saved['below_since'] = None
    if multiple >= threshold - 1e-10 and not saved['fired']:
        saved['fired'] = True
        return True
    return False


def target_rate_window(policy, ledger, samples, context, category, minutes, fixed, now):
    """Pure shared window math for warning ladder and read-only dashboard data."""
    rate = context['R'][category]
    begin = max(context['start'], ledger['forgiven'].get(category, -math.inf))
    lower = max(begin, now - minutes * 60)
    linear = policy.get('linear_meter_steps', False)
    metered = box_estimate(policy, ledger, category, lower, now) if linear else confirmed_window(ledger, category, lower, now)
    confirmed = metered if linear else confirmed_window(ledger, category, lower, now, policy)
    if linear and category == 'non-fable':
        # 2A-F can jump on shared-meter rounding while F catches up.
        # A rate stop needs a conservative paired-meter lower bound.
        confirmed = min(confirmed, confirmed_window(ledger, category, lower, now, policy))
    sample_points = sum(e[2] for e in samples.get('events', [])
                        if e[1] == category and lower < e[0] <= now) * policy['rate_points_per_million'][category] / 1e6
    denominator = rate * minutes / 60
    meaningful_meter = denominator * fixed[0] >= meter_quantum(policy, category)
    credit = soft_credit(ledger, category, lower, now)

    def basis(metered, confirmed, sample_points, denominator):
        estimated = metered if linear else max(metered, sample_points) if meaningful_meter else sample_points
        return (estimated, confirmed, denominator,
                max(0., estimated - credit) / denominator if denominator else 0.,
                max(0., confirmed - credit) / denominator if denominator else 0.)
    if automock_split(policy):
        # Other box: mock usage subtracted, at the split rate. Total: everything at the
        # pre-split rate. Report whichever is higher, so stops are never looser than before.
        mock = automock_points(policy, category, lower, now)
        other = basis(max(0., metered - mock), max(0., confirmed - mock), max(0., sample_points - mock), denominator)
        total = basis(metered, confirmed, sample_points, denominator / other_share(policy))
        chosen = max(other, total, key=lambda b: (b[4], b[3]))
        estimated, confirmed, denominator = chosen[:3]
        estimate_multiple, confirmed_multiple = max(other[3], total[3]), max(other[4], total[4])
    else:
        estimated, confirmed, denominator, estimate_multiple, confirmed_multiple = basis(
            metered, confirmed, sample_points, denominator)
    return dict(lower=lower, linear=linear, confirmed=confirmed, estimated=estimated,
                denominator=denominator, meaningful_meter=meaningful_meter, credit=credit,
                multiple=estimate_multiple, confirmed_multiple=confirmed_multiple)


def ladder_evaluate(policy, ledger, samples, state, now, all_trips=False):
    """Return new level messages and confirmed-rate trip readings, no I/O."""
    import claude_usage_messages as human
    context = rate_context(policy, ledger, now)
    prep = policy['preparation']
    messages, trips, current = [], [], []
    category_blocked = state.setdefault('trip_epochs', {})
    for category in CATEGORIES:
        rate = context['R'][category]
        begin = max(context['start'], ledger['forgiven'].get(category, -math.inf))
        epoch = f'{begin}:{context["scale"]}:{context["final"]}'
        for minutes, fixed, default in RATE_LADDER:
            window = target_rate_window(policy, ledger, samples, context, category, minutes, fixed, now)
            lower, linear = window['lower'], window['linear']
            confirmed, estimated = window['confirmed'], window['estimated']
            denominator, meaningful_meter = window['denominator'], window['meaningful_meter']
            credit = window['credit']
            estimate_multiple, confirmed_multiple = window['multiple'], window['confirmed_multiple']
            credit_text = f' Soft credit: {credit:.4f} points.' if credit else ''
            rate_text = human.rate_annotation(context['name'], estimated, denominator)
            confirmed_text = human.rate_annotation(context['name'], confirmed, denominator)
            configured_stop = prep.get('stops', {}).get(str(minutes), default)
            levels = notify_levels(minutes, configured_stop)
            reached = [level for level in levels if estimate_multiple >= level - 1e-10]
            if reached:
                current.append(f'{LABELS[category]} {minutes}m {max(reached):g}× crossed ({estimate_multiple:.2f}×; {estimated:.2f} points / {denominator:.2f} planned).{rate_text}{credit_text}')
            for level in levels:
                if configured_stop < default and level >= configured_stop and not context['final']:
                    continue
                key = f'{category}:{minutes}:notify:{level}'
                prior_level = state.get('levels', {}).get(key, {})
                was_fired = prior_level.get('fired', False) and prior_level.get('epoch') == epoch
                if level_crossing(state.setdefault('levels', {}), key, estimate_multiple, level, fixed[0], minutes, now, epoch):
                    messages.append(f'{LABELS[category]} {minutes}m {level:g}× crossed ({estimate_multiple:.2f}×; {estimated:.2f} points / {denominator:.2f} planned).{rate_text}{credit_text}')
                if level == fixed[0] and was_fired and not state['levels'][key]['fired']:
                    messages.append(f'{LABELS[category]} {minutes}m rate back below warning ({estimated:.2f} points / {denominator:.2f} planned). Nothing needed.{rate_text}{credit_text}')
            eligible = (not context['final'] and context['approved'] and
                        (linear or minutes != 5 or five_minute_stop(policy, context, category)))
            # Unknown or missing intervals never become permission to infer a stop.
            if category == 'non-fable':
                changes = [e for e in ledger.get('meter_intervals', [])
                           if e[2] == category and lower <= e[0] and e[1] <= now and e[3] > 0]
                eligible = eligible and len(changes) >= 2 and (linear or meaningful_meter)
            eligible = eligible and not any(e[1] == category and lower < e[0] <= now for e in ledger['gaps'])
            marker = f'{category}:{minutes}:stop'
            crossed = level_crossing(state.setdefault('stops', {}), marker, confirmed_multiple, configured_stop,
                                     fixed[0], minutes, now, epoch)
            if not eligible or confirmed_multiple < configured_stop - 1e-10:
                continue
            if prep.get('rate_stops_dry_run', False):
                if crossed:
                    messages.append(f'WOULD STOP {LABELS[category]}: {minutes}m confirmed {confirmed_multiple:.2f}× ≥ {configured_stop:g}× ({confirmed:.2f} points / {denominator * configured_stop + credit:.2f} limit); dry run blocks nothing.{confirmed_text}{credit_text}')
            elif all_trips or category_blocked.get(category) != epoch:
                # A failed application is retried from the durable latch. Do not
                # emit another trip for the same category/window release epoch.
                category_blocked[category] = epoch
                trips.append(dict(category=category, name=f'{context["name"]}: {minutes}-minute rate',
                                  start=lower, end=now + minutes * 60, used=confirmed,
                                  allowed=denominator * configured_stop + credit, soft_credit=credit, planned=denominator, incomplete=False, approved=True, run=True))
                messages.append(f'STOP {LABELS[category]}: {minutes}m confirmed {confirmed_multiple:.2f}× ≥ {configured_stop:g}× ({confirmed:.2f} points / {denominator * configured_stop + credit:.2f} limit); blocked on every account until human UNCAP.{confirmed_text}{credit_text}')
    state['current'] = current
    return messages, trips


def ladder_delivery(host, state, lines, now, senders=None):
    """Aggregate one steer per tick, retaining independent destination receipts."""
    lines = [' '.join(line.splitlines()) for line in lines]
    pending = state.setdefault('pending', [])
    if lines:
        pending.extend(dict(at=now, line=line, lines=[line], receipts={}) for line in lines)
    expired = [item for item in pending if now - item['at'] > 1800]
    state['pending'] = pending = [item for item in pending if now - item['at'] <= 1800]
    if expired and not pending and state.get('current'):
        delivered = set.intersection(*(set(item['receipts']) for item in expired))
        receipts = {channel: expired[-1]['receipts'][channel] for channel in delivered}
        pending.append(dict(at=now, line=' '.join(state['current']), lines=list(state['current']), receipts=receipts))
    channels = senders or {'desktop': host.desktop_notify, 'matrix': matrix_notice, 'thread': lambda line: thread_notice(host, line), 'file': lambda line: append_alert(line, alert_path(host))}
    required=set(channels)
    if senders is None:
        try:
            import claude_usage_category
            if claude_usage_category.enabled():
                required.add('category')
                for item in pending:
                    if 'category' not in item['receipts']:
                        rows=[(str(i),2,line) for i,line in enumerate(item.get('lines',[item['line']]))]
                        if claude_usage_category.notices(rows,item['at']):
                            item['receipts']['category']=now
        except Exception:
            pass  # CATEGORY transport cannot break existing notification paths.
    for channel, sender in channels.items():
        targets = [item for item in pending if channel not in item['receipts']]
        if not targets:
            continue
        combined = ' '.join(item['line'] for item in targets)
        retry_summary = any(now - item['at'] > 30 for item in targets)
        summary_lines = state.get('current') if retry_summary else None
        if retry_summary and not summary_lines and 'current' in state:
            import claude_usage_messages as human
            originals = [line for item in targets for line in item.get('lines', [item['line']])]
            labels = sorted({human.category(line) for line in originals} - {None})
            summary_lines = []
            for label in labels:
                original = next((human.event(line, now) for line in originals if human.category(line) == label), None)
                if original:
                    summary_lines.append(human.pack('NOTE', label, 'recovered from previous usage warning: ' + original['headline'],
                        original['used'] + ' (last reported)', 'Recovered: ' + human.end_text(now, now), 'nothing needed'))
        def active_transition(line):
            import claude_usage_messages as human
            event = human.event(line, now) or {}
            if event.get('level') == 'RELEASED':
                return True
            if event.get('level') != 'STOPPED':
                return False
            if not retry_summary or 'current' not in state:
                return True
            return any((human.event(current, now) or {}).get('level') == 'STOPPED'
                       and human.category(current) == event['category'] for current in state['current'])
        if retry_summary:
            import claude_usage_messages as human
            urgent = [line for item in targets for line in item.get('lines', [item['line']]) if active_transition(line)]
            if urgent:
                labels = {human.category(line) for line in urgent}
                summary_lines = urgent + [line for line in summary_lines or []
                    if not ((human.event(line,now) or {}).get('level') == 'NOTE' and human.category(line) in labels)]
        if summary_lines:
            combined = ' '.join(summary_lines)
        if channel == 'file':
            deliveries = sorted({item['receipts'].get('thread', 'FAILED; retry next check') for item in targets})
            combined += ' [thread delivery=' + ','.join(deliveries) + ']'
        if (channel in ('desktop', 'matrix') or (channel == 'thread' and getattr(host, 'MATRIX_COPY_ALL', False))) and senders is None:
            import claude_usage_messages as human
            pieces = ([(str(i), 2, line) for i, line in enumerate(summary_lines)] if summary_lines else
                      [(str(i), 2, line) for i, item in enumerate(targets) for line in item.get('lines', [item['line']])])
            combined = human.render(pieces, now)
            if channel == 'thread' and combined:
                combined = 'For you:\n' + combined
        if channel == 'matrix' and senders is None:
            import claude_usage_messages as human
            urgent_items = [item for item in targets if any(active_transition(line) for line in item.get('lines', [item['line']]))]
            urgent_items.sort(key=lambda item: 0 if any((human.event(line,now) or {}).get('level') == 'STOPPED' for line in item.get('lines',[item['line']])) else 1)
            groups = [[item] for item in urgent_items]
            regular = [item for item in targets if item not in urgent_items]
            if regular:
                groups.append(regular)
            for group in groups:
                pieces = [(str(i), 2, line) for i,item in enumerate(group) for line in item.get('lines',[item['line']])]
                text = human.render(pieces, now) if group[0] in urgent_items or not retry_summary else human.render(
                    [(str(i),2,line) for i,line in enumerate(summary_lines or []) if not (human.event(line,now) or {}).get('level') in ('STOPPED','RELEASED')], now)
                if not text or sender(text):
                    for item in group:
                        item['receipts'][channel] = 'delivered' if text else 'suppressed incomplete event'
            continue
        result = sender(combined) if combined else 'suppressed incomplete event'
        if result:
            for item in targets:
                item['receipts'][channel] = result if isinstance(result, str) else 'delivered'
                if channel == 'thread':
                    # A retry that finally steers must also leave its actual
                    # success beside the earlier failed-delivery log line.
                    item['receipts'].pop('file', None)
        else:
            print(f'claude-usage: ladder {channel} delivery failed; retry next check')
    state['pending'] = [item for item in pending if any(c not in item['receipts'] for c in required)]


def reconcile_rate_latches(host, policy, ledger, state, now):
    """Re-check rate-stop latches against the current math; release any the
    current pace and stop levels no longer support. Allowance latches untouched."""
    try:
        context = rate_context(policy, ledger, now)
    except ValueError:
        return []
    exceeded = {item['category'] for item in readings(policy, ledger, now)
                if item['used'] >= item['allowed'] - 1e-8 and item['approved']}
    released = []
    with host.guard_lock(host.GUARD_STATE):
        guard = host.guard_state(host.GUARD_STATE)
        for key, item in list(guard['latches'].items()):
            match = re.search(r'(\d+)-minute rate$', item.get('box', ''))
            category = item.get('category')
            if not match or category not in CATEGORIES or category in exceeded:
                continue
            minutes = int(match.group(1))
            result = rate_latch_math(policy, ledger, item, context)
            if result is None:
                continue
            used, limit = result
            if used < limit - 1e-8:
                guard['latches'].pop(key)
                state.setdefault('trip_epochs', {}).pop(category, None)
                import claude_usage_messages as human
                line = human.pack('RELEASED', LABELS[category], f'{minutes}-minute stop cleared by new math',
                    f'{used:.2f} of {limit:.2f} points (original {minutes}-minute window)',
                    'released ' + human.end_text(now, now), 'nothing needed; requests can continue')
                if line not in released:
                    released.append(line)
        if released:
            guard['rate_reconcile_pending'] = True
            guard.setdefault('rate_reconcile_messages', []).extend(released)
            host.atomic_write(host.GUARD_STATE, json.dumps(guard, sort_keys=True) + '\n')
        if guard.get('rate_reconcile_pending'):
            host.apply_hard_stops(guard, host.GUARD_STATE)
            released = guard.pop('rate_reconcile_messages', released)
            guard.pop('rate_reconcile_pending', None)
            host.atomic_write(host.GUARD_STATE, json.dumps(guard, sort_keys=True) + '\n')
    return released


def ladder_tick(host, policy, samples, now):
    path = host.LOG.parent / 'rate-ladder.json'
    ledger_path = host.LOG.parent / 'category-ledger.json'
    with host.guard_lock(path), host.guard_lock(ledger_path):
        state = host.read_state(path)
        ledger = json.loads(ledger_path.read_text())
        ledger['token_sample'] = dict(since=samples.get('token_since', now), at=now,
                                      complete=samples.get('token_complete', False), records=samples.get('token_records', []))
        lines = reconcile_rate_latches(host, policy, ledger, state, now)
        more, trips = ladder_evaluate(policy, ledger, samples, state, now)
        lines += more
        disabled = override_state(host).get('disabled', {})
        trips = [item for item in trips if item['category'] not in disabled]
        lines = [line for line in lines if not (line.startswith('STOP ') and any(LABELS[c] in line for c in disabled))]
        if trips:
            enforce(host, policy, ledger, trips, now)
        with host.guard_lock(host.GUARD_STATE):
            guard = host.guard_state(host.GUARD_STATE)
            blocked = {}
            for item in guard['latches'].values():
                if item.get('category'):
                    blocked.setdefault(item['category'], item)
            for category, item in blocked.items():
                reading = dict(category=category, name=item['box'], used=item['used'], allowed=item['allowed'], end=item['end'], run=True, soft_credit=item.get('soft_credit', 0))
                state.setdefault('current', []).append(notice_message(reading, now, True, policy, ledger))
        host.atomic_write(ledger_path, json.dumps(ledger, sort_keys=True) + '\n')
        # Durable outbox before delivery, so a failed channel never loses a level.
        if lines:
            state.setdefault('pending', []).extend(dict(at=now, line=line, lines=[line], receipts={}) for line in lines)
        host.atomic_write(path, json.dumps(state, sort_keys=True) + '\n')
        ladder_delivery(host, state, [], now)
        host.atomic_write(path, json.dumps(state, sort_keys=True) + '\n')


def preparation_summary(policy, ledger, now):
    saved = preparation_snapshot(policy, ledger, now)
    prep = policy['preparation']
    context = rate_context(policy, ledger, now) if preparation_active(policy, now) else None
    lines = [('Revised meter/token estimate: ON.' if policy.get('linear_meter_steps', False) else 'Revised meter/token estimate: OFF; set linear_meter_steps to turn it on.'), f'Preparation: H={prep["active_hours"]:g} active hours; final reserve one third; rate-stop dry run={prep.get("rate_stops_dry_run", False)}.']
    for category in CATEGORIES:
        rate = saved['P'][category] / prep['active_hours']
        actual = context['R'][category] if context else rate
        five = 'live on cached meters' if context and five_minute_stop(policy, context, category) and not context['final'] else 'notify-only: meter step is too coarse or final mode'
        if policy.get('linear_meter_steps', False) and context and not context['final']:
            five = 'linear meter estimate; paired rounding guard also required' if category == 'non-fable' else 'linear meter estimate, reduced by a usable token estimate'
        banked = (sum(source.get('fable_banked' if category == 'fable' else 'banked', 0)
                      for source in policy['sources'] if (source['provider'] == 'codex') == (category == 'codex'))
                  if category != 'non-fable' else 0)
        weekly = sum(s.get('weekly_banked', 0) for s in policy['sources'] if s['provider'] == 'claude') if category != 'codex' else 0
        resets = f'banked resets={banked:g}' + (f' plus weekly={weekly:g}' if category != 'codex' else '')
        lines.append(f'{LABELS[category]}: C={saved["C"][category]:.2f}, P={saved["P"][category]:.2f}, reserve={saved["C"][category]/3:.2f} points; R={rate:.3f} points/hour (current {actual:.3f}); {resets}; five-minute stop {five}.')
    return lines


def matrix_notice(line):
    """Queue local messages; give stops/releases a dedicated notification preview."""
    import claude_usage_messages as human
    if not line:
        return True
    success = True
    for body in human.matrix_batches(line):
        try:
            result = subprocess.run(['python3', str(Path.home() / '.local/lib/matrix-relay/notify_mirror.py'), '--producer', 'usage'],
                input='For you: ' + body, text=True, capture_output=True, timeout=5,
                env=dict(os.environ, USAGEBOX_COPY_HANDLED='1'))
            success = result.returncode == 0 and result.stdout.strip() == 'queued' and success
        except (OSError, subprocess.SubprocessError):
            success = False
    return success


def deliver_current(host, events, now, path=None):
    """State-change delivery, one human message/check, no historical backlog."""
    import claude_usage_messages as human
    def suppressed(key, level, line, trip):
        if key.startswith(('account:', 'missing:')):
            return True
        item = human.event(line, now) or {}
        return (level == 1 and not trip and item.get('level') == 'NOTE'
                and re.fullmatch(r'Preparation total(?: .*?)?: \d+% budget level', item.get('headline', ''))
                and item.get('action') == 'nothing needed')
    def ended_record(record, key):
        old = human.event(record['line'], now)
        recovery = human.pack('NOTE', human.category(record['line']),
            'Previous warning ended' + (': ' + old['headline'] if old else ''),
            old['used'] + ' (last reported)' if old else '',
            'Recovered: ' + human.end_text(now, now), 'nothing needed')
        return dict(level=1, line=recovery, at=now, receipts={}, trip=False, ended_from=key)
    path = path or host.LOG.parent / 'current-notices.json'
    with host.guard_lock(path):
        saved = host.read_state(path)
        records = saved.setdefault('records', {})
        # Remove retired notices before retries or recovery synthesis.
        # Keep category boxes/rates/exhaustion and their delivery state intact.
        for key in list(records):
            record = records[key]
            if suppressed(key, record['level'], record['line'], record['trip']):
                records.pop(key)
        if events is not None:
            events = [entry for entry in events if not suppressed(entry[0], entry[1], entry[2], entry[4])]
        if events is None:
            guard = host.guard_state(host.GUARD_STATE)
            blocked = {LABELS[category] for item in guard['latches'].values() for category in latch_categories(item)}
            unknown_blocked = any(not latch_categories(item) for item in guard['latches'].values())
            events = [(key, record['level'], record['line'], math.inf, record['trip'])
                      for key, record in records.items() if now - record['at'] <= 1800
                      and not (key.startswith('hard-release:') and (human.category(record['line']) in blocked
                          or (key.startswith('hard-release:other:') and unknown_blocked)))]
        active = set()
        if getattr(host, 'MATRIX_COPY_ALL', False) and not saved.get('copy_all_started'):
            for record in records.values():
                if 'matrix' in record['receipts']:
                    record['receipts'].setdefault('thread', 'mirrored before copy rule')
            saved['copy_all_started'] = now
        for key, level, line, interval, trip in events:
            active.add(key)
            line = ' '.join(line.splitlines())
            record = records.get(key)
            if (record and (human.event(record['line'], now) or {}).get('level') == 'WARNING'
                    and (human.event(line, now) or {}).get('level') == 'NOTE'
                    and any(c in record['receipts'] for c in ('desktop', 'matrix'))):
                records[key + ':ended'] = ended_record(record, key)
            if not record or record['level'] != level:
                record = dict(level=level, line=line, at=now, receipts={}, trip=trip)
                records[key] = record
            record['line'] = line  # Retry a current reading, never an old amount.
            if now - record['at'] > 1800 and any(c not in record['receipts'] for c in ('desktop', 'matrix', 'file')):
                record.update(at=now)  # refresh only undelivered destinations
        category_required=False
        try:
            import claude_usage_category
            category_required=claude_usage_category.enabled()
            if category_required:
                for key,record in records.items():
                    if (key in active or record.get('ended_from')) and 'category' not in record['receipts']:
                        if claude_usage_category.notices([(key,record['level'],record['line'])],record['at']):
                            record['receipts']['category']=now
        except Exception:
            pass  # Never gate local stops or the user's delivery on a remote notice.
        for key in list(records):
            if key not in active:
                if records[key].get('ended_from'):
                    base = records[key]['ended_from']
                    base_level = (human.event(records.get(base, {}).get('line', ''), now) or {}).get('level')
                    if (base not in active or base_level == 'NOTE') and now - records[key]['at'] <= 1800:
                        continue
                record = records.pop(key)
                label = human.category(record['line'])
                previous = human.event(record['line'], now) or {}
                if label and (key.startswith(('rate:', 'trip:')) or previous.get('level') == 'WARNING') and any(c in record['receipts'] for c in ('desktop', 'matrix')) and not record.get('ended_from'):
                    records[key + ':ended'] = ended_record(record, key)
        targets = [(key, record['level'], record['line']) for key, record in records.items() if 'desktop' not in record['receipts']]
        rendered = human.render(targets, now) if targets else ''
        if targets and (not rendered or host.desktop_notify(rendered)):
            for key, _, _ in targets:
                if key in records:
                    records[key]['receipts']['desktop'] = now
        matrix_targets = [(key, record['level'], record['line']) for key, record in records.items() if 'matrix' not in record['receipts']]
        urgent = [row for row in matrix_targets if (human.event(row[2], now) or {}).get('level') in ('STOPPED', 'RELEASED')]
        urgent.sort(key=lambda row: 0 if (human.event(row[2],now) or {}).get('level') == 'STOPPED' else 1)
        groups = [[row] for row in urgent]
        regular = [row for row in matrix_targets if row not in urgent]
        if regular:
            groups.append(regular)
        for group in groups:
            if matrix_notice(human.render(group, now)):
                for key, _, _ in group:
                    records[key]['receipts']['matrix'] = now
        thread_targets = [(key, record) for key, record in records.items()
                          if (getattr(host, 'MATRIX_COPY_ALL', False) or record['trip'] or key.startswith(('rate:', 'category-out:', 'tool:')))
                          and 'thread' not in record['receipts']]
        if thread_targets:
            thread_text = (human.render([(key, record['level'], record['line']) for key, record in thread_targets], now)
                           if getattr(host, 'MATRIX_COPY_ALL', False) else ' '.join(record['line'] for _, record in thread_targets))
            if thread_text and getattr(host, 'MATRIX_COPY_ALL', False):
                thread_text = 'For you:\n' + thread_text
            result = thread_notice(host, thread_text) if thread_text else 'suppressed incomplete event'
            if result:
                for _, record in thread_targets:
                    record['receipts']['thread'] = result
                    record['receipts'].pop('file', None)
        for record in records.values():
            if 'file' not in record['receipts']:
                delivery = record['receipts'].get('thread', 'not sent')
                if delivery == 'not sent' and record in [item for _, item in thread_targets]:
                    delivery = 'FAILED; retry next check'
                if append_alert(record['line'] + f' [thread delivery={delivery}]', alert_path(host)):
                    record['receipts']['file'] = now
        required_receipts=('desktop','matrix','file','thread') if getattr(host,'MATRIX_COPY_ALL',False) else ('desktop','matrix','file')
        if category_required:required_receipts+=('category',)
        records = {key: record for key, record in records.items()
                   if not record.get('ended_from') or not all(c in record['receipts'] for c in
                       required_receipts)}
        saved['records'] = records
        host.atomic_write(path, json.dumps(saved, sort_keys=True) + '\n')
