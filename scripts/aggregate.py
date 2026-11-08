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

Route exemplar: for each directional route and season, candidate legs must
have their departure and landing observed, both ends assigned from below
1,500 ft AGL, coverage of at least 0.8, and a flown length within 1.3x the
great circle (a diversion or a return is not the route). At least 5 distinct
observed days are required; the published exemplar is the MEDOID of the last
up-to-10 qualifying day-tracks: the one actually-flown track closest to the
others. An actual flown path, never an average: averages draw lines no
aircraft ever flew. (This file selects the exemplar day and id; the medoid
distance runs where the path files are at hand, over those candidates.)

Flight numbers: only suffix-less callsigns (KAL081, not KLM18F) are mapped,
and only for prefixes in the supplied map: several large carriers fly
ATC-scrambled callsigns whose digits are not the marketing flight number, so
mapping them would label flights with numbers no passenger ever booked. The
map must list only carriers whose callsign number is the marketing number.

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
import math
import re

TURN_MIN_S = 15 * 60
CIRCUIT_KM = 60
DAILY_CAP = 14
MISMATCH_FLAG_DAYS = 3
EXEMPLAR_MIN_DAYS = 5
EXEMPLAR_MIN_COV = 0.8
NUM_MIN_DAYS = 3
EXEMPLAR_MAX_AGL = 1500
EXEMPLAR_MAX_VS_GC = 1.3
MEDOID_LAST_DAYS = 10
AIRLINE_PLAIN = __import__('re').compile(r'^[A-Z]{3}[0-9]{1,4}$')


_COORDS = None


def airport_coords():
    global _COORDS
    if _COORDS is None:
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        rows = json.load(open(os.path.join(here, 'airports.json')))
        _COORDS = {r[0]: (r[2], r[3]) for r in rows}
    return _COORDS


def gc_km(a, b):
    co = airport_coords()
    if a not in co or b not in co:
        return None
    (la1, lo1), (la2, lo2) = co[a], co[b]
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


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
                if prefix_map and cs[:3] in prefix_map and AIRLINE_PLAIN.match(cs):
                    digits = cs[3:].lstrip('0')
                    if digits.isdigit():
                        num = prefix_map[cs[:3]] + digits
                if num and r.get('reg'):
                    numdays[num][r['reg']].add(day)
                    numtypes[(num, r['reg'])] = r.get('type')
                    numlast[num] = max(numlast.get(num, day), day)
                # Route exemplar candidates: complete, well-observed tracks.
                oq, dq = r.get('oq'), r.get('dq')
                if (r['org'] and r['dst'] and r['org'] != r['dst']
                        and r['tk'] and r['ld'] and r['cov'] >= EXEMPLAR_MIN_COV
                        and oq and dq and oq[0] is not None and dq[0] is not None
                        and oq[0] <= EXEMPLAR_MAX_AGL and dq[0] <= EXEMPLAR_MAX_AGL):
                    route = f"{r['org']}-{r['dst']}"
                    gc = gc_km(r['org'], r['dst'])
                    if gc and gc > 50 and r['km'] <= EXEMPLAR_MAX_VS_GC * gc:
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
        recent = sorted(by_day.values(), key=lambda c: c['day'])[-MEDOID_LAST_DAYS:]
        exemplars.append({'route': route, 'season': season, 'n_days': len(by_day),
                          'candidates': [{'day': c['day'], 'id': c['id'], 'km': c['km']} for c in recent]})

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
