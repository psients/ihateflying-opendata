#!/usr/bin/env python3
"""Refuse to publish a day whose extraction looks implausible.

A published day is marked done forever, so an old format quirk or a half
archive must FAIL LOUDLY here, never upload. Thresholds from the measured
2026-11-07 baseline (legs/scanned 1.68, absurd drops 0.094%).

Usage: sanity_gate.py YYYY-MM-DD OUT_DIR
"""
import json
import sys


def main():
    day, out_dir = sys.argv[1], sys.argv[2]
    m = json.load(open(f'{out_dir}/meta-{day}.json'))
    ratio = m['legs'] / max(1, m['scanned'])
    problems = []
    if m['legs'] <= 0:
        problems.append('zero legs')
    if not 0.3 <= ratio <= 5.0:
        problems.append(f'legs/scanned {ratio:.2f} outside [0.3, 5.0]')
    if m['absurd_legs'] > 0.01 * max(1, m['legs']):
        problems.append(f"absurd-speed drops {m['absurd_legs']} exceed 1% of legs")
    if problems:
        sys.exit('refusing to publish: ' + '; '.join(problems))
    print('sanity gate passed:', json.dumps({k: m[k] for k in ('scanned', 'legs', 'errors', 'bad_meta', 'absurd_legs')}))


if __name__ == '__main__':
    main()
