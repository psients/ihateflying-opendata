#!/usr/bin/env python3
"""Weekly quarantine dry-run: exercise the REAL aggregation over every
published day, so the month proves the counting chain and not just file
production. Writes dryrun/summary-<date>.json and, once a week, the seeded
spot-check selection. RED (exit 1) when:
  - any airframe's floor DECREASED since the previous dry-run (floors are
    monotone by construction; a decrease means double-counting was present
    before, or instability now), except across a recorded extractor version
    change, which restarts the comparison like the streak;
  - the aggregation crashes or breaches the memory budget (6 GB).

Inputs are public stand-ins, clearly labelled: the hex->registration map is
the corpus majority (production will use the site's registry; the gate
mechanics are identical), and the prefix map is scripts/
prefix_map_candidate.json (production's list is a separate owner approval).

Spot-check selection: seeded by the sha256 of MANIFEST.json, so neither the
assistant nor anyone else can cherry-pick: 5 airframes, 3 flight numbers,
2 route exemplars per week, written to dryrun/spotcheck-<date>.json. The
checks themselves (registry agreement, schedule cross-source, route plots)
run in-session with results committed to quarantine/days explanations or
the spotcheck file.

Usage: dryrun.py [--local DIR]   (--local skips downloading; test mode)
"""
import collections
import datetime as dt
import glob
import gzip
import hashlib
import json
import os
import random
import re
import resource
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aggregate import aggregate, iter_days  # noqa: E402
from extract_day import EXTRACTOR_VERSION  # noqa: E402

ROOT = os.path.normpath(os.path.join(HERE, '..'))
OUT_DIR = os.path.join(ROOT, 'dryrun')
REPO = os.environ.get('GITHUB_REPOSITORY', 'psients/ihateflying-opendata')
MEM_BUDGET_MB = 6144


def download_all(dest):
    rels = subprocess.run(['gh', 'release', 'list', '-R', REPO, '--limit', '200',
                           '--json', 'tagName', '-q', '.[].tagName'],
                          capture_output=True, text=True, check=True).stdout.split()
    for tag in rels:
        if not tag.startswith('data-'):
            continue
        # Retried: one HTTP 500 on one asset (2026-11-09, the first run)
        # otherwise kills a run that downloads ~1,350 files.
        for attempt in range(1, 6):
            r = subprocess.run(['gh', 'release', 'download', tag, '-R', REPO,
                                '--pattern', 'flights-*.jsonl.gz', '--dir', dest, '--clobber'])
            if r.returncode == 0:
                break
            if attempt == 5:
                raise RuntimeError(f'download of {tag} failed 5 times')
            time.sleep(30 * attempt)


def majority_reg(patterns):
    votes = collections.defaultdict(collections.Counter)
    for day, legs in iter_days(patterns):
        for r in legs:
            if r.get('reg'):
                votes[r['hex']][r['reg']] += 1
    return {hx: c.most_common(1)[0][0] for hx, c in votes.items()}


def main():
    local = None
    if len(sys.argv) > 2 and sys.argv[1] == '--local':
        local = sys.argv[2]
    dest = local or os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'corpus')
    os.makedirs(dest, exist_ok=True)
    if not local:
        download_all(dest)
    patterns = [os.path.join(dest, 'flights-*.jsonl.gz')]

    reg_map = majority_reg(patterns)
    prefix_map = json.load(open(os.path.join(HERE, 'prefix_map_candidate.json')))
    # Flight numbers over the trailing 60 days, as the site will show them.
    all_days = sorted(re.search(r'(\d{4}-\d{2}-\d{2})', p).group(1) for p in glob.glob(patterns[0]))
    num_since = all_days[-60] if len(all_days) >= 60 else None
    out = aggregate(iter_days(patterns), reg_map, prefix_map, num_since=num_since)
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // (1024 * 1024 if sys.platform == 'darwin' else 1024)

    floors = {a['hex']: a['departures'] for a in out['airframes']}
    flags = collections.Counter(f['kind'] for f in out['flags'])
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    summary = {
        'date': today, 'extractor_v': EXTRACTOR_VERSION,
        'days': len(set(glob.glob(patterns[0]))),
        'airframes': len(floors), 'floor_total': sum(floors.values()),
        'flightnum_rows': len(out['flightnums']),
        'exemplar_rows': len(out['route_exemplars']),
        'flag_counts': dict(flags), 'peak_rss_mb': peak_mb,
        'floors': floors,
        'inputs': 'hex->reg = corpus majority (stand-in); prefix map = candidate',
    }

    problems = []
    if peak_mb > MEM_BUDGET_MB:
        problems.append(f'peak memory {peak_mb} MB over the {MEM_BUDGET_MB} MB budget')
    prev_files = sorted(glob.glob(os.path.join(OUT_DIR, 'summary-*.json')))
    if prev_files:
        prev = json.load(open(prev_files[-1]))
        if prev.get('extractor_v') == EXTRACTOR_VERSION:
            drops = {h: (prev['floors'][h], floors.get(h, 0))
                     for h in prev['floors'] if floors.get(h, 0) < prev['floors'][h]}
            if drops:
                sample = dict(list(drops.items())[:5])
                problems.append(f'{len(drops)} airframe floor(s) DECREASED, e.g. {sample}')
        else:
            summary['note'] = f"extractor changed ({prev.get('extractor_v')} -> {EXTRACTOR_VERSION}); monotone comparison restarted"

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f'summary-{today}.json'), 'w') as f:
        json.dump(summary, f, indent=1, sort_keys=True)

    # Seeded, un-cherry-pickable spot-check selection.
    manifest_path = os.path.join(ROOT, 'MANIFEST.json')
    seed = hashlib.sha256(open(manifest_path, 'rb').read()).hexdigest() if os.path.exists(manifest_path) else today
    rng = random.Random(seed)
    busy_hexes = sorted(h for h, d in floors.items() if d >= 10)
    nums = sorted({(f['num'], f['org']) for f in out['flightnums']})
    routes = sorted({(e['route'], e['season']) for e in out['route_exemplars']})
    selection = {
        'date': today, 'seed': seed[:16],
        'airframes': rng.sample(busy_hexes, min(5, len(busy_hexes))),
        'flightnums': [list(x) for x in rng.sample(nums, min(3, len(nums)))],
        'routes': [list(x) for x in rng.sample(routes, min(2, len(routes)))],
        'results': None,
    }
    with open(os.path.join(OUT_DIR, f'spotcheck-{today}.json'), 'w') as f:
        json.dump(selection, f, indent=1, sort_keys=True)

    print(json.dumps({k: summary[k] for k in ('days', 'airframes', 'floor_total',
                                              'flightnum_rows', 'exemplar_rows', 'flag_counts', 'peak_rss_mb')}))
    if problems:
        for p in problems:
            print(f'::error::{p}')
        sys.exit(1)


if __name__ == '__main__':
    main()
