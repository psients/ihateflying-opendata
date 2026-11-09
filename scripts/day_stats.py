#!/usr/bin/env python3
"""Completeness stats for one extracted day, to compare adsb.lol instances.

Usage: day_stats.py YYYY-MM-DD OUT_DIR  -> one JSON line on stdout.
  scanned/legs/sus  from the meta file
  tk / ld           legs whose departure / landing was observed
  hexes             distinct airframes with at least one leg
  air_h             airborne hours summed over legs
  hours_covered     UTC hours of the day in which at least 100 legs start
                    (a partial archive shows as missing hours)
"""
import collections
import gzip
import json
import sys


def main():
    day, out_dir = sys.argv[1], sys.argv[2]
    meta = json.load(open(f'{out_dir}/meta-{day}.json'))
    legs = [json.loads(line) for line in gzip.open(f'{out_dir}/flights-{day}.jsonl.gz', 'rt')]
    by_hour = collections.Counter(r['t0'] // 3600 % 24 for r in legs)
    print(json.dumps({
        'day': day, 'source': meta.get('source'), 'scanned': meta['scanned'], 'legs': len(legs),
        'sus': meta.get('sus_legs'), 'tk': sum(r['tk'] for r in legs), 'ld': sum(r['ld'] for r in legs),
        'hexes': len({r['hex'] for r in legs}),
        'air_h': round(sum(r['t1'] - r['t0'] for r in legs) / 3600),
        'hours_covered': sum(1 for h in range(24) if by_hour[h] >= 100),
    }, sort_keys=True))


if __name__ == '__main__':
    main()
