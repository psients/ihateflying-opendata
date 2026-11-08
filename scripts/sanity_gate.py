#!/usr/bin/env python3
"""Refuse to publish a day whose extraction looks implausible.

A published day is marked done forever, so an old format quirk or a half
archive must FAIL LOUDLY here, never upload. Thresholds from measured
baselines (legs/scanned 1.28 allowlisted; suspect motion 1.0%-3.0% of legs).

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
    # Catastrophe-only: suspect motion swings with real-world GPS interference
    # (measured 1.0%-3.0% across five Oct-Nov 2026 days; the spikes cluster in
    # known interference zones - Baltic, Balkans, Gulf, US test ranges). Those
    # legs are real flights, kept and flagged. Only a broken extraction makes
    # nearly everything teleport.
    if m['sus_legs'] > 0.10 * max(1, m['legs']):
        problems.append(f"suspect-motion legs {m['sus_legs']} exceed 10% of legs")
    if problems:
        sys.exit('refusing to publish: ' + '; '.join(problems))
    print('sanity gate passed:', json.dumps({k: m[k] for k in ('scanned', 'legs', 'errors', 'bad_meta', 'sus_legs')}))


if __name__ == '__main__':
    main()
