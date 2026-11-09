#!/usr/bin/env python3
"""Spot-check detail for the week's seeded selection (dryrun/spotcheck-DATE.json).

Re-runs the dry-run's exact aggregation (same stand-in inputs) over every
published day, then writes dryrun/spotcheck-detail-DATE.json with what an
in-session check needs to compare each item against an independent source:
  airframes   the airframe row (floor, per-year departures), registrations,
              types and callsign prefixes seen, flags, top routes, last legs
  flightnums  the flight-number row, plus destinations, UTC departure hours
              and registrations of the matching legs in the same window
  routes      the exemplar row, every candidate track (from the paths files)
              and the medoid: the candidate closest on average to the rest
The checks and their verdicts are recorded in the selection file's results.

Usage: spotcheck.py [DATE]   (default: the newest selection file)
"""
import collections
import glob
import gzip
import json
import math
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aggregate import AIRLINE_PLAIN, aggregate, iter_days  # noqa: E402
from dryrun import OUT_DIR, REPO, download_all, majority_reg  # noqa: E402


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def resample(points, n=60):
    """n points evenly spaced along the polyline (by distance)."""
    if len(points) < 2:
        return points * n
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + km(a, b))
    total = cum[-1] or 1.0
    out, j = [], 0
    for i in range(n):
        target = total * i / (n - 1)
        while j < len(cum) - 2 and cum[j + 1] < target:
            j += 1
        seg = (cum[j + 1] - cum[j]) or 1.0
        f = (target - cum[j]) / seg
        a, b = points[j], points[j + 1]
        out.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
    return out


def fetch_paths(day, dest):
    name = f'paths-{day}.jsonl.gz'
    path = os.path.join(dest, name)
    for attempt in range(1, 6):
        if os.path.exists(path):
            return path
        r = subprocess.run(['gh', 'release', 'download', f'data-{day[:7]}', '-R', REPO,
                            '--pattern', name, '--dir', dest, '--clobber'])
        if r.returncode == 0:
            return path
        time.sleep(10 * attempt)
    raise RuntimeError(f'cannot download {name}')


def main():
    sel_files = sorted(glob.glob(os.path.join(OUT_DIR, 'spotcheck-2*.json')))
    if len(sys.argv) > 1:
        sel_path = os.path.join(OUT_DIR, f'spotcheck-{sys.argv[1]}.json')
    else:
        sel_path = sel_files[-1]
    sel = json.load(open(sel_path))
    date = sel['date']
    dest = os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'corpus')
    os.makedirs(dest, exist_ok=True)
    download_all(dest)
    patterns = [os.path.join(dest, 'flights-*.jsonl.gz')]

    reg_map = majority_reg(patterns)
    prefix_map = json.load(open(os.path.join(HERE, 'prefix_map_candidate.json')))
    all_days = sorted(re.search(r'(\d{4}-\d{2}-\d{2})', p).group(1) for p in glob.glob(patterns[0]))
    num_since = all_days[-60] if len(all_days) >= 60 else None
    out = aggregate(iter_days(patterns), reg_map, prefix_map, num_since=num_since)

    hexes = set(sel['airframes'])
    nums = {tuple(x) for x in sel['flightnums']}
    rows = {a['hex']: a for a in out['airframes'] if a['hex'] in hexes}
    flags = collections.defaultdict(collections.Counter)
    for f in out['flags']:
        if f['ref'] in hexes:
            flags[f['ref']][f['kind']] += 1

    # Second pass: leg-level context for the selected airframes and numbers.
    hx_legs = collections.defaultdict(list)
    num_legs = collections.defaultdict(list)
    for day, legs in iter_days(patterns):
        for r in legs:
            if r['hex'] in hexes:
                hx_legs[r['hex']].append({'day': day, 'cs': r['cs'], 'reg': r.get('reg'), 'type': r.get('type'),
                                          'org': r['org'], 'dst': r['dst'], 'tk': r['tk'], 'ld': r['ld'],
                                          't0': r['t0']})
            cs = r['cs']
            if (num_since is None or day >= num_since) and cs[:3] in prefix_map and AIRLINE_PLAIN.match(cs):
                digits = cs[3:].lstrip('0')
                key = (prefix_map[cs[:3]] + digits, r['org'])
                if key in nums:
                    num_legs[key].append({'day': day, 'cs': cs, 'reg': r.get('reg'), 'type': r.get('type'),
                                          'dst': r['dst'], 'tk': r['tk'], 'hour': r['t0'] // 3600 % 24})

    detail = {'date': date, 'seed': sel['seed'], 'days': len(all_days), 'num_since': num_since,
              'airframes': {}, 'flightnums': {}, 'routes': {}}
    for hx in sel['airframes']:
        ls = hx_legs.get(hx, [])
        detail['airframes'][hx] = {
            'row': rows.get(hx), 'majority_reg': reg_map.get(hx),
            'regs': collections.Counter(x['reg'] for x in ls).most_common(5),
            'types': collections.Counter(x['type'] for x in ls).most_common(5),
            'prefixes': collections.Counter(x['cs'][:3] for x in ls).most_common(5),
            'flags': dict(flags.get(hx, {})), 'legs': len(ls), 'days_seen': len({x['day'] for x in ls}),
            'top_routes': collections.Counter(f"{x['org']}-{x['dst']}" for x in ls).most_common(10),
            'last_legs': sorted(ls, key=lambda x: x['t0'])[-8:],
        }
    for num, org in sel['flightnums']:
        ls = num_legs.get((num, org), [])
        detail['flightnums'][f'{num} {org}'] = {
            'row': next((f for f in out['flightnums'] if f['num'] == num and f['org'] == org), None),
            'legs': len(ls), 'callsigns': collections.Counter(x['cs'] for x in ls).most_common(3),
            'destinations': collections.Counter(x['dst'] for x in ls).most_common(5),
            'utc_hours': collections.Counter(x['hour'] for x in ls).most_common(5),
            'regs': collections.Counter(x['reg'] for x in ls).most_common(8),
            'types': collections.Counter(x['type'] for x in ls).most_common(3),
            'days': sorted({x['day'] for x in ls}),
        }
    pdir = os.path.join(dest, 'paths')
    os.makedirs(pdir, exist_ok=True)
    for route, season in sel['routes']:
        ex = next((e for e in out['route_exemplars'] if e['route'] == route and e['season'] == season), None)
        tracks = {}
        if ex:
            want = {c['id']: c['day'] for c in ex['candidates']}
            for day in sorted(set(want.values())):
                for line in gzip.open(fetch_paths(day, pdir), 'rt'):
                    p = json.loads(line)
                    if p['id'] in want:
                        tracks[p['id']] = p['segs']
        flat = {i: [(pt[0], pt[1]) for s in segs for pt in s] for i, segs in tracks.items()}
        rs = {i: resample(pts) for i, pts in flat.items() if len(pts) >= 2}
        mean_dev = {i: sum(sum(km(a, b) for a, b in zip(rs[i], rs[j])) / len(rs[i])
                           for j in rs if j != i) / max(1, len(rs) - 1) for i in rs}
        medoid = min(mean_dev, key=mean_dev.get) if mean_dev else None
        detail['routes'][f'{route} {season}'] = {
            'row': ex, 'medoid': medoid,
            'mean_km_from_others': {i: round(v, 1) for i, v in mean_dev.items()},
            'tracks': tracks,
        }

    path = os.path.join(OUT_DIR, f'spotcheck-detail-{date}.json')
    with open(path, 'w') as f:
        json.dump(detail, f, indent=1, sort_keys=True)
        f.write('\n')
    print(f'wrote {path}: {len(detail["airframes"])} airframes, {len(detail["flightnums"])} numbers, '
          f'{len(detail["routes"])} routes')


if __name__ == '__main__':
    main()
