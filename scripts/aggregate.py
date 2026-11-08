#!/usr/bin/env python3
"""The method by which ihateflying.org derives its aggregates from the daily
files (published per ODbL 4.6(b): the method of making the alterations).

Input: flights-*.jsonl.gz files from this repository's releases.
Output: one JSON document with three sections and a flags list.

Counting rule for departures ("the floor"): a leg counts as one departure iff
  - its takeoff was observed (tk = true), and
  - it is not a same-airport circuit (org = dst with under 60 km flown), and
  - at least 15 minutes passed since the same airframe's previous leg ended
    (go-arounds and touch-and-goes re-approach within minutes; real
    turnarounds do not), and
  - the airframe has not exceeded 14 counted departures that day (the most a
    real airliner was observed to fly is 12; anything above 14 is flagged and
    not counted - crowd-sourced data can be spoofed).
Every count is a floor: receivers miss flights, so true numbers are higher.

An aircraft's identity: when a registration map is supplied (hex -> expected
registration), legs whose broadcast registration disagrees are excluded and
flagged - transponder addresses are reassigned when aircraft change
registries, and a floor must never carry another airframe's flights.

Route exemplar: for each directional route and season, the exemplar is the
most recent fully-observed real track (takeoff and landing seen, coverage of
at least 0.8, length within the middle half of candidates), from at least 5
distinct observed days. An actual flown path, never an average: averages draw
lines no aircraft ever flew.

Usage:
  aggregate.py --flights 'out/flights-*.jsonl.gz' --out agg.json
      [--registry hex_to_reg.json] [--prefix-map icao_to_iata.json]
      [--window-start YYYY-MM-DD --window-end YYYY-MM-DD]
"""
import argparse
import collections
import datetime as dt
import glob
import gzip
import json
import re

TURN_MIN_S = 15 * 60
CIRCUIT_KM = 60
DAILY_CAP = 14
MISMATCH_FLAG_DAYS = 3
EXEMPLAR_MIN_DAYS = 5
EXEMPLAR_MIN_COV = 0.8
NUM_MIN_DAYS = 3


def season_of(day):
    """IATA season label: summer from the last Sunday of March, winter from
    the last Sunday of October."""
    d = dt.date.fromisoformat(day)
    def last_sunday(y, month):
        x = dt.date(y, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(y, 12, 31)
        return x - dt.timedelta(days=(x.weekday() + 1) % 7)
    s, w = last_sunday(d.year, 3), last_sunday(d.year, 10)
    if d < s:
        return f'{d.year - 1}W'
    return f'{d.year}S' if d < w else f'{d.year}W'


def load_days(patterns):
    days = collections.defaultdict(list)
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            m = re.search(r'flights-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$', path)
            if not m:
                continue
            for line in gzip.open(path, 'rt'):
                days[m.group(1)].append(json.loads(line))
    return days


def countable(legs):
    """The day's countable departures for one airframe (rule in the header)."""
    legs = sorted(legs, key=lambda x: x['t0'])
    out = []
    for i, r in enumerate(legs):
        if not r['tk']:
            continue
        if r['org'] and r['org'] == r['dst'] and r['km'] < CIRCUIT_KM:
            continue
        if i > 0 and r['t0'] - legs[i - 1]['t1'] < TURN_MIN_S:
            continue
        out.append(r)
    return out


def aggregate(days, registry=None, prefix_map=None, window=None):
    flags = []
    airframes = {}
    numdays = collections.defaultdict(lambda: collections.defaultdict(set))
    numtypes = {}
    numlast = {}
    routes = collections.defaultdict(dict)

    for day in sorted(days):
        if window and not (window[0] <= day <= window[1]):
            continue
        by_hex = collections.defaultdict(list)
        for r in days[day]:
            by_hex[r['hex']].append(r)
        for hx, legs in by_hex.items():
            expected = registry.get(hx) if registry else None
            if expected:
                ok, bad = [], 0
                for r in legs:
                    reg = (r.get('reg') or '').replace('-', '')
                    if reg and reg != expected.replace('-', ''):
                        bad += 1
                    else:
                        ok.append(r)
                if bad:
                    flags.append({'kind': 'reg-mismatch', 'ref': hx, 'day': day, 'n': bad})
                legs = ok
            deps = countable(legs)
            if len(deps) > DAILY_CAP:
                flags.append({'kind': 'daily-cap', 'ref': hx, 'day': day, 'n': len(deps)})
                deps = deps[:DAILY_CAP]
            if deps and (registry is None or hx in registry):
                a = airframes.setdefault(hx, {'hex': hx, 'departures': 0, 'by_year': {},
                                              'first_day': day, 'last_day': day})
                a['departures'] += len(deps)
                a['by_year'][day[:4]] = a['by_year'].get(day[:4], 0) + len(deps)
                a['last_day'] = day
            for r in deps:
                # Flight numbers: the operating callsign, mapped to the
                # marketing form only where a prefix map says so.
                cs = r['cs']
                num = None
                if prefix_map and cs[:3] in prefix_map:
                    digits = cs[3:].lstrip('0')
                    if digits.isdigit():
                        num = prefix_map[cs[:3]] + digits
                if num and r.get('reg'):
                    numdays[num][r['reg']].add(day)
                    numtypes[(num, r['reg'])] = r.get('type')
                    numlast[num] = max(numlast.get(num, day), day)
                # Route exemplar candidates: complete, well-observed tracks.
                if (r['org'] and r['dst'] and r['org'] != r['dst']
                        and r['tk'] and r['ld'] and r['cov'] >= EXEMPLAR_MIN_COV):
                    route = f"{r['org']}-{r['dst']}"
                    cand = routes[(route, season_of(day))]
                    best = cand.get(day)
                    if best is None or r['cov'] > best['cov']:
                        cand[day] = {'id': r['id'], 'day': day, 'km': r['km'], 'cov': r['cov']}

    flightnums = []
    for num, regs in numdays.items():
        entries = [{'reg': reg, 'type': numtypes.get((num, reg)), 'days': len(ds)}
                   for reg, ds in regs.items() if len(ds) >= NUM_MIN_DAYS]
        entries.sort(key=lambda e: -e['days'])
        if entries:
            flightnums.append({'num': num, 'window_days': len({d for ds in regs.values() for d in ds}),
                               'entries': entries[:5], 'last_seen': numlast[num]})

    exemplars = []
    for (route, season), by_day in routes.items():
        if len(by_day) < EXEMPLAR_MIN_DAYS:
            continue
        cands = sorted(by_day.values(), key=lambda c: c['km'])
        q1, q3 = cands[len(cands) // 4]['km'], cands[(3 * len(cands)) // 4]['km']
        mid = [c for c in cands if q1 <= c['km'] <= q3]
        pick = max(mid or cands, key=lambda c: c['day'])
        exemplars.append({'route': route, 'season': season, 'n_days': len(by_day),
                          'exemplar_day': pick['day'], 'exemplar_id': pick['id']})

    return {'airframes': sorted(airframes.values(), key=lambda a: a['hex']),
            'flightnums': sorted(flightnums, key=lambda f: f['num']),
            'route_exemplars': sorted(exemplars, key=lambda e: (e['route'], e['season'])),
            'flags': flags}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--flights', nargs='+', required=True)
    ap.add_argument('--registry')
    ap.add_argument('--prefix-map')
    ap.add_argument('--window-start')
    ap.add_argument('--window-end')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    registry = json.load(open(args.registry)) if args.registry else None
    prefix_map = json.load(open(args.prefix_map)) if args.prefix_map else None
    window = (args.window_start, args.window_end) if args.window_start and args.window_end else None
    days = load_days(args.flights)
    out = aggregate(days, registry, prefix_map, window)
    with open(args.out, 'w') as f:
        json.dump(out, f, separators=(',', ':'))
    print(f"{len(days)} day(s): {len(out['airframes'])} airframes, "
          f"{len(out['flightnums'])} flight numbers, {len(out['route_exemplars'])} route exemplars, "
          f"{len(out['flags'])} flags")


if __name__ == '__main__':
    main()
