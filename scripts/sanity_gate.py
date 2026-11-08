#!/usr/bin/env python3
"""Refuse to publish a day whose extraction looks implausible.

A published day is marked done forever, so an old format quirk or a half
archive must FAIL LOUDLY here, never upload. Thresholds from measured
baselines (legs/scanned 1.28 allowlisted; suspect motion 1.0%-3.0% of legs).

A day that trips the suspect-motion gate for an investigated, explained
upstream cause can be admitted by a line in scripts/gate_exceptions.txt
(an owner-visible commit). The exception names the exact archive release
and a raised CEILING, never "no check": a different archive, or a worse
share than the one investigated, still refuses.

Usage: sanity_gate.py YYYY-MM-DD OUT_DIR
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUS_SHARE_MAX = 0.10
EXCEPTION_CEILING_MAX = 0.20   # no exception may admit worse than this


def load_exceptions():
    """{day: (source_tag, ceiling, reason)} from scripts/gate_exceptions.txt.
    Format: YYYY-MM-DD SOURCE_TAG MAX_SUS_SHARE reason..."""
    out = {}
    try:
        for line in open(os.path.join(HERE, 'gate_exceptions.txt')):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            day, tag, ceiling, reason = line.split(None, 3)
            ceiling = float(ceiling)
            if not SUS_SHARE_MAX < ceiling <= EXCEPTION_CEILING_MAX:
                sys.exit(f'gate_exceptions.txt: {day} ceiling {ceiling} outside '
                         f'({SUS_SHARE_MAX}, {EXCEPTION_CEILING_MAX}]')
            out[day] = (tag, ceiling, reason)
    except FileNotFoundError:
        pass
    return out


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
    limit, note = SUS_SHARE_MAX, None
    exc = load_exceptions().get(day)
    if exc and exc[0] == m.get('source'):
        limit, note = exc[1], f'gate exception {exc[1]:.2f}: {exc[2]}'
    share = m['sus_legs'] / max(1, m['legs'])
    if share > limit:
        problems.append(f"suspect-motion legs {m['sus_legs']} ({share:.2%}) exceed {limit:.0%} of legs"
                        + (' (above the exception ceiling)' if note else ''))
    if problems:
        sys.exit('refusing to publish: ' + '; '.join(problems))
    if note:
        print(note)
    print('sanity gate passed:', json.dumps({k: m[k] for k in ('scanned', 'legs', 'errors', 'bad_meta', 'sus_legs')}))


if __name__ == '__main__':
    main()
