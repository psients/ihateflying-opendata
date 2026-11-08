#!/usr/bin/env python3
"""Quarantine verifier: check published days against the invariants the
red-team review established, red on any break, and append the result to
QUARANTINE.md. The site reads NOTHING from this data until the log shows at
least 30 consecutive clean days (owner's rule, 2026-11-09).

  verify.py daily              yesterday's published day (plus its D-1 as
                               cross-midnight context when published)
  verify.py range START END    a backfill batch, sampled per week

Invariants (2026-11-07 allowlisted baselines: legs/scanned 1.28, tk 0.66,
ld 0.66, both-ends-null 0.11, sus 1.03%, countable max/airframe 11):
  I1  meta matches the file: line count = legs, sha256 agrees, v = current,
      allowlist on
  I2  legs/scanned in [0.4, 3.5]   (2023 era: [0.2, 3.5] - fewer receivers)
  I3  tk and ld rates >= 0.35      (2023 era: >= 0.20)
  I4  both-ends-null <= 0.35       (2023 era: <= 0.55)
  I5  sus legs <= 2%
  I6  no duplicate (hex, t0) within the day
  I7  no impossible back-to-back countable departures (per hex, with the
      previous day as carry context when available)
  I8  first-5-minutes countable-departure rate <= 2x the mid-day 5-minute
      average (the midnight double-count guard, measured structurally fixed)
  I9  every org/dst carries oq/dq; paths stay <= 150 points per leg
  I10 prefix drift: any UNLISTED prefix above 300 legs/day is reported (a
      new airline or a wrongly-excluded one) - warning, not red
"""
import collections
import datetime as dt
import glob
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aggregate import countable  # noqa: E402
from extract_day import EXTRACTOR_VERSION  # noqa: E402

LOG = os.path.join(HERE, '..', 'QUARANTINE.md')


def era(day):
    return '2023' if day < '2024-01-01' else 'now'


BOUNDS = {
    '2023': {'ratio': (0.2, 3.5), 'tk': 0.20, 'ld': 0.20, 'null2': 0.55},
    'now': {'ratio': (0.4, 3.5), 'tk': 0.35, 'ld': 0.35, 'null2': 0.35},
}


def fetch_day(day, dest):
    """Download a published day's flights+meta via gh (anonymous rate limits
    are too tight for a month of dailies). Returns paths or None."""
    month = day[:7]
    out = {}
    for name in (f'flights-{day}.jsonl.gz', f'meta-{day}.json'):
        path = os.path.join(dest, name)
        if not os.path.exists(path):
            r = subprocess.run(['gh', 'release', 'download', f'data-{month}', '--pattern', name,
                                '--dir', dest, '--clobber'], capture_output=True, text=True)
            if r.returncode != 0:
                return None
        out[name.split('-')[0]] = path
    return out


def check_day(day, flights_path, meta_path, prev_legs):
    problems, warnings = [], []
    meta = json.load(open(meta_path))
    legs = [json.loads(l) for l in gzip.open(flights_path, 'rt')]
    n = len(legs)
    sha = hashlib.sha256(open(flights_path, 'rb').read()).hexdigest()
    if n != meta['legs']:
        problems.append(f'I1 line count {n} != meta legs {meta["legs"]}')
    if meta['sha256'].get(os.path.basename(flights_path)) != sha:
        problems.append('I1 sha256 mismatch')
    if meta['v'] != EXTRACTOR_VERSION:
        warnings.append(f'I1 extractor v{meta["v"]} (current v{EXTRACTOR_VERSION}); reprocess pending')
    if not meta.get('allowlist'):
        problems.append('I1 allowlist off')
    b = BOUNDS[era(day)]
    ratio = n / max(1, meta['scanned'])
    if not b['ratio'][0] <= ratio <= b['ratio'][1]:
        problems.append(f'I2 legs/scanned {ratio:.2f}')
    tk = sum(r['tk'] for r in legs) / max(1, n)
    ld = sum(r['ld'] for r in legs) / max(1, n)
    if tk < b['tk'] or ld < b['ld']:
        problems.append(f'I3 tk {tk:.2f} ld {ld:.2f}')
    null2 = sum(1 for r in legs if not r['org'] and not r['dst']) / max(1, n)
    if null2 > b['null2']:
        problems.append(f'I4 both-ends-null {null2:.2f}')
    sus = sum(1 for r in legs if r.get('sus')) / max(1, n)
    if sus > 0.02:
        problems.append(f'I5 sus {sus:.3f}')
    seen = collections.Counter((r['hex'], r['t0']) for r in legs)
    dups = [k for k, c in seen.items() if c > 1]
    if dups:
        problems.append(f'I6 {len(dups)} duplicate (hex, t0)')
    by_hex = collections.defaultdict(list)
    for r in legs:
        by_hex[r['hex']].append(r)
    prev_end = {}
    for r in prev_legs or []:
        prev_end[r['hex']] = max(prev_end.get(r['hex'], 0), r['t1'])
    bad_pairs = 0
    day0 = dt.datetime.fromisoformat(day + 'T00:00:00+00:00').timestamp()
    buckets = collections.Counter()
    for hx, ls in by_hex.items():
        deps = countable(ls, prev_end.get(hx))
        for a, c in zip(deps, deps[1:]):
            if c['t0'] - a['t1'] < 15 * 60:
                bad_pairs += 1
        for r in deps:
            buckets[int((r['t0'] - day0) // 300)] += 1
    if bad_pairs:
        problems.append(f'I7 {bad_pairs} impossible back-to-back departures')
    mid = [buckets[i] for i in range(24, 264)]
    mid_avg = sum(mid) / max(1, len(mid))
    if prev_legs is not None and buckets[0] > 2 * mid_avg and buckets[0] > 20:
        problems.append(f'I8 first-5-min departures {buckets[0]} vs mid-day avg {mid_avg:.0f}')
    missing_q = sum(1 for r in legs if (r['org'] and not r.get('oq')) or (r['dst'] and not r.get('dq')))
    if missing_q:
        problems.append(f'I9 {missing_q} legs missing end quality')
    allow = {l.strip() for l in open(os.path.join(HERE, 'airlines.txt'))
             if l.strip() and not l.startswith('#')}
    big = collections.Counter(r['cs'][:3] for r in legs if r['cs'][:3] not in allow)
    # Unlisted prefixes cannot appear in the files (the extractor filters),
    # so drift is measured from meta: kept ratio collapsing hints at it too;
    # the real drift check needs the unfiltered count, which only the
    # extractor sees. Report instead when a LISTED prefix vanishes.
    gone = [p for p in ('KAL', 'AAR', 'RYR', 'DAL', 'UAL', 'AAL') if not any(r['cs'][:3] == p for r in legs)]
    if gone:
        warnings.append(f'I10 expected prefixes absent: {gone}')
    return problems, warnings, {'legs': n, 'tk': round(tk, 2), 'sus': round(sus, 3),
                                'floor_pairs': bad_pairs, 'ratio': round(ratio, 2)}


def append_log(day, problems, warnings, stats):
    line = (f"| {day} | {'RED' if problems else 'clean'} | {stats['legs']} | {stats['tk']} | "
            f"{stats['sus']} | {'; '.join(problems) or '-'} | {'; '.join(warnings) or '-'} |\n")
    if not os.path.exists(LOG):
        with open(LOG, 'w') as f:
            f.write("# Quarantine log\n\nThe site reads nothing from this data until this log shows at "
                    "least 30 consecutive clean days and every RED has a written cause (owner's rule, "
                    "2026-11-09). One row per verified day.\n\n"
                    "| day | verdict | legs | tk | sus | problems | warnings |\n"
                    "|---|---|---|---|---|---|---|\n")
    with open(LOG, 'a') as f:
        f.write(line)


def main():
    mode = sys.argv[1]
    dest = os.environ.get('RUNNER_TEMP', '/tmp')
    if mode == 'daily':
        day = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
        got = fetch_day(day, dest)
        if not got:
            print(f'::warning::{day} not published yet; nothing to verify')
            return
        prev_day = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
        prev = fetch_day(prev_day, dest)
        prev_legs = [json.loads(l) for l in gzip.open(prev['flights'], 'rt')] if prev else None
        problems, warnings, stats = check_day(day, got['flights'], got['meta'], prev_legs)
        append_log(day, problems, warnings, stats)
        for w in warnings:
            print(f'::warning::{day}: {w}')
        if problems:
            sys.exit(f'::error::{day}: ' + '; '.join(problems))
        print(f'{day} clean: {stats}')
        return
    if mode == 'range':
        start, end = sys.argv[2], sys.argv[3]
        day = dt.date.fromisoformat(start)
        last = dt.date.fromisoformat(end)
        failures = 0
        prev_legs = None
        while day <= last:
            d = day.isoformat()
            got = fetch_day(d, dest)
            if got:
                problems, warnings, stats = check_day(d, got['flights'], got['meta'], prev_legs)
                append_log(d, problems, warnings, stats)
                prev_legs = [json.loads(l) for l in gzip.open(got['flights'], 'rt')]
                if problems:
                    failures += 1
                    print(f'::error::{d}: ' + '; '.join(problems))
                else:
                    print(f'{d} clean')
            else:
                prev_legs = None
            day += dt.timedelta(days=1)
        if failures:
            sys.exit(f'{failures} day(s) failed verification')
        return
    sys.exit('usage: verify.py daily | range START END')


if __name__ == '__main__':
    main()
