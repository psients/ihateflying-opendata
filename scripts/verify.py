#!/usr/bin/env python3
"""Quarantine verifier. The site reads NOTHING from this pipeline until
QUARANTINE.md shows the exit criteria met (owner's rule, 2026-11-09).

Modes:
  verify.py daily              sweep the last 17 days: verify every published
                               day that has no record yet (or was reprocessed
                               under a newer extractor), write SKIPPED records
                               for days adsb.lol never archived, re-red on
                               verifier exceptions. Exits 1 if any NEW red.
  verify.py range START END    verify a backfill batch day by day with
                               cross-midnight context. NEVER exits before all
                               records are written (a red must reach the log).
  verify.py render             regenerate QUARANTINE.md from the per-day
                               records (the ONLY writer of that file).
  verify.py explain DAY TEXT   attach a written cause to a day's record.
  verify.py drill              write a clearly-labelled DRILL red and exit 1,
                               to prove the failure-email channel end to end.

Records live one file per day in quarantine/days/DAY.json, so concurrent
writers (the daily job and a range run) can never conflict: they touch
different paths, and the table is regenerated, never merged.

THE STREAK (frozen definition; changing it, or any counting constant in this
file or aggregate.py, is an owner-visible commit that RESTARTS the streak):
scanning back from yesterday (UTC) to the QUARANTINE CLOCK START, count the
days whose record is CLEAN under the current extractor version. The clock
start (quarantine/clock.json) is the data-day of the first daily
verification run, so the streak can grow by at most one per day of
operation: the catch-up days published on the first run, and every
backfilled day, are verified but sit before the clock and never shorten the
month. A SKIPPED record (adsb.lol has no archive for that day) neither
counts nor breaks. A RED breaks it. A day with no record breaks it once it
is 4+ days old (younger gaps are healing in progress). Drills are recorded
apart (quarantine/drills/) and never touch a data-day. Every red, inside
the clock window or before it, must carry an explanation before exit; a
re-verified record keeps the records it replaced under "previous", so an
explanation is never lost.

Invariants (I*) red the day; warnings (W*) are visible but not red.
Baselines measured on 2026-11-07 (allowlisted, extractor v5): legs/scanned
1.28, tk 0.66, ld 0.66, both-ends-null 0.084, sus 1.03%, deps/legs 0.655,
chain continuity 0.993.
  I1  meta matches the files: line counts, sha256 of flights AND paths,
      extractor version current (older = warning while reprocessing),
      allowlist on
  I2  legs/scanned in [0.4, 3.5]   (2023 era: [0.5, 3.5], the protocol's)
  I3  tk and ld rates >= 0.35
  I4  both-ends-null <= 0.35
  I5  sus legs <= 10% red, > 3% warning (real-world GPS interference moves it
      1.0%-3.0% day to day; measured on five Oct-Nov 2026 days)
  I6  no duplicate (hex, t0)
  I7  no impossible back-to-back countable departures (cross-midnight
      context when the previous day is published)
  I8  first-5-minutes countable departures <= 2x the mid-day average
      (W8 when disarmed because the previous day is unpublished)
  I9  every org/dst carries oq/dq; every paths line has <= 150 points with
      never-decreasing in-segment timestamps (equal seconds allowed: sub-second fixes round together), and flights/paths ids match
      1:1 (the no-chord-across-gaps property is an extractor guarantee,
      proven at extraction; simplification legitimately keeps points more
      than 300 s apart, so it cannot be re-checked here)
  I10 countable departures / legs in [0.40, 0.85]
  I11 flight-chain continuity (prev dst = next org per airframe, where
      both known): red below 0.95 in every era; warning below 0.985
      (2023 era: below 0.97). Baselines: 0.993 on 2026-11-07; 0.971-0.979
      across 2023-06-01..07, where breaks show the same ground-time pattern
      as 2026 (median 2.0 h vs 1.6 h for continuous pairs) - more unseen
      flights from sparser receivers, not a new error class. A systemic
      relabel measured 0.883 (red); a single-hub relabel moves it ~0.008
      (warning).
  I12 source completeness: no other adsb.lol instance's archive for the day
      is >= 1.4x the bytes of the published one, unless a
      scripts/source_overrides.txt line confirms the published instance (a
      measured, human choice); an override naming a DIFFERENT instance than
      the published one is red until the day is republished. Measured
      2026-11-09 over 1,344 days: exactly four days at >= 1.4x, three of them
      thin (2025-06-11 prod-0 held 33,705 aircraft against ~60k around it)
      and one an empty stub. Size flags for a decision and never switches
      by itself: bytes are a proxy (2023-03-03's better test-1 sat under
      1.4x), so measure.yml decides. Sizes count ONE archive form per
      release (plan.archive_assets); summing both forms of a two-form
      release once made 2024-09-16's staging-0 look 1.3-1.4x bigger. An
      unreadable upstream listing is a warning.
  W12 any top-30 prefix collapsing >70% day over day
  W13 an excluded prefix (meta excluded_top) with >= 300 legs that was not
      in yesterday's top 10 AND at least doubled (yesterday's 10th count is
      below half of today's): allowlist drift, a new or missing airline,
      without top-10 membership flicker
  W14 ratio or tk drifting >15% against the record 7 days earlier
  W15 any of the anchor carriers (KAL AAR SWA RYR DAL UAL AAL) with zero
      countable departures
  W17 a thin day: fewer than 25,000 aircraft scanned (2023 era: 12,000) -
      a partial upstream archive, published because what it holds is real,
      labelled so its low counts are never mistaken for low traffic
  W18 legs below 80% of the median of the +-7 surrounding days (MANIFEST.json,
      at least 4 neighbours): a partial day by comparison, which W17's fixed
      floor misses (2026-01-23 held 59% with its only rival archive
      truncated; 2023-03-09's best instance covers 7 h). Christmas dips
      (81-82%) stay under it: they are real traffic
  W16 the upstream archive was irregular (meta tar_gaps > 0): the day was
      read past regions that are not valid tar records - end-marker padding
      between joined archive pieces (nothing lost) or damage (aircraft stored
      there lost). The warning states the bytes skipped; seen so far: ~64 KB
      on 2026-04-30 and 2026-05-04, next to adsb.lol's 2026-05-06 outage,
      with full-size days recovered (66,743 and 76,094 aircraft)
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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aggregate import countable  # noqa: E402
from extract_day import EXTRACTOR_VERSION  # noqa: E402

ROOT = os.path.normpath(os.path.join(HERE, '..'))
DAYS_DIR = os.path.join(ROOT, 'quarantine', 'days')
DRILLS_DIR = os.path.join(ROOT, 'quarantine', 'drills')
CLOCK = os.path.join(ROOT, 'quarantine', 'clock.json')
MANIFEST = os.path.join(ROOT, 'MANIFEST.json')
LOG = os.path.join(ROOT, 'QUARANTINE.md')
REPO = os.environ.get('GITHUB_REPOSITORY', 'psients/ihateflying-opendata')
ANCHORS = ('KAL', 'AAR', 'SWA', 'RYR', 'DAL', 'UAL', 'AAL')

BOUNDS = {
    '2023': {'ratio': (0.5, 3.5), 'tk': 0.35, 'ld': 0.35, 'null2': 0.35, 'chain_warn': 0.97},
    'now': {'ratio': (0.4, 3.5), 'tk': 0.35, 'ld': 0.35, 'null2': 0.35, 'chain_warn': 0.985},
}
CHAIN_RED = 0.95
SOURCE_RATIO_RED = 1.4
THIN_RELATIVE = 0.80
_SIZES = None
_LEGS = None


def neighbour_median_legs(day):
    """Median legs of the published days within +-7 of DAY (itself
    excluded), from MANIFEST.json; None with fewer than 4 neighbours."""
    global _LEGS
    if _LEGS is None:
        try:
            _LEGS = {d: e.get('legs') for d, e in json.load(open(MANIFEST)).get('days', {}).items()}
        except Exception:
            _LEGS = {}
    d0 = dt.date.fromisoformat(day)
    near = [_LEGS[x] for k in range(-7, 8) if k
            and (x := (d0 + dt.timedelta(days=k)).isoformat()) in _LEGS and _LEGS[x]]
    if len(near) < 4:
        return None
    near.sort()
    mid = len(near) // 2
    return near[mid] if len(near) % 2 else (near[mid - 1] + near[mid]) / 2


def instance_sizes():
    """{day: {instance: archive bytes}} over every adsb.lol release, listed
    once per process (plan.py's sources and instance ladder)."""
    global _SIZES
    if _SIZES is None:
        from plan import PREFERENCE, SOURCES, TAG, archive_assets, paged
        sizes = collections.defaultdict(dict)
        for repo in SOURCES:
            for rel in paged(f'repos/{repo}/releases'):
                m = TAG.match(rel.get('tag_name', ''))
                if m and m.group(4) in PREFERENCE:
                    day = f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
                    # One archive form only: summing both forms of a
                    # two-form release doubled its apparent size.
                    sizes[day][m.group(4)] = sum(a['size'] for a in archive_assets(rel))
        _SIZES = sizes
    return _SIZES


def source_check(day, source_tag):
    """I12. Returns None, ('warn', text) or ('red', text)."""
    inst = (source_tag or '').split('-planes-readsb-')[-1]
    try:
        from plan import OVERRIDES
        sizes = instance_sizes().get(day, {})
    except Exception as e:
        return ('warn', f'I12 not checked: upstream listing unavailable ({type(e).__name__})')
    if day in OVERRIDES:
        if OVERRIDES[day] != inst:
            return ('red', f'I12 published {inst} but source_overrides.txt names {OVERRIDES[day]}: '
                           f'republish pending')
        return None
    if inst not in sizes:
        return ('warn', f'I12 published instance {inst or "?"} is no longer listed upstream')
    big = max(sizes, key=sizes.get)
    ratio = sizes[big] / max(1, sizes[inst])
    if big != inst and ratio >= SOURCE_RATIO_RED:
        return ('red', f'I12 {big} archive is {ratio:.1f}x the published {inst} '
                       f'({sizes[big] / 1e6:.0f} MB vs {sizes[inst] / 1e6:.0f} MB): measure both, '
                       f'record the choice in source_overrides.txt')
    return None


def era(day):
    return '2023' if day < '2024-01-01' else 'now'


def today_utc():
    return dt.datetime.now(dt.timezone.utc).date()


def rec_path(day):
    return os.path.join(DAYS_DIR, f'{day}.json')


def load_rec(day):
    try:
        return json.load(open(rec_path(day)))
    except Exception:
        return None


def write_rec(rec, annotate=False):
    """Write a day's record. A replaced record that was RED, or carried an
    explanation, is kept under "previous" (newest first): a red's written
    cause must survive its own re-verification. annotate=True updates the
    same verification in place (attaching an explanation adds no history)."""
    os.makedirs(DAYS_DIR, exist_ok=True)
    old = load_rec(rec['day'])
    if not annotate and old and (old.get('verdict') == 'RED' or old.get('explanation')):
        kept = {k: v for k, v in old.items() if k != 'previous'}
        rec['previous'] = [kept] + list(old.get('previous') or [])
    elif old and old.get('previous') and 'previous' not in rec:
        rec['previous'] = old['previous']
    rec['verified_at'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    with open(rec_path(rec['day']), 'w') as f:
        json.dump(rec, f, indent=1, sort_keys=True)
        f.write('\n')


def clock_start():
    try:
        return json.load(open(CLOCK))['start']
    except Exception:
        return None


def manifest_versions():
    try:
        return {d: e.get('v') for d, e in json.load(open(MANIFEST)).get('days', {}).items()}
    except Exception:
        return {}


def fetch(day, dest):
    """Download a published day's three files. Returns dict or None."""
    month = day[:7]
    out = {}
    for key, name in (('flights', f'flights-{day}.jsonl.gz'),
                      ('paths', f'paths-{day}.jsonl.gz'),
                      ('meta', f'meta-{day}.json')):
        path = os.path.join(dest, name)
        # Retried (10-40 s apart): a single HTTP 500 must not record a
        # published day as "not fetchable". A truly missing asset still
        # returns None after ~100 s.
        for attempt in range(1, 6):
            if os.path.exists(path):
                break
            r = subprocess.run(['gh', 'release', 'download', f'data-{month}', '-R', REPO,
                                '--pattern', name, '--dir', dest, '--clobber'],
                               capture_output=True, text=True)
            if r.returncode == 0:
                break
            if attempt == 5:
                return None
            time.sleep(10 * attempt)
        out[key] = path
    return out


def upstream_has(day):
    """Does any adsb.lol instance archive this day? (plan.py's ladder.)"""
    try:
        from plan import find_day
        return find_day(dt.date.fromisoformat(day)) is not None
    except Exception:
        return True  # unknowable now = assume yes, let healing retry


def check_day(day, files, prev_legs, prev_meta, history):
    problems, warnings = [], []
    meta = json.load(open(files['meta']))
    legs = [json.loads(l) for l in gzip.open(files['flights'], 'rt')]
    n = len(legs)

    # I1 integrity
    for key in ('flights', 'paths'):
        sha = hashlib.sha256(open(files[key], 'rb').read()).hexdigest()
        base = os.path.basename(files[key])
        if meta['sha256'].get(base) != sha:
            problems.append(f'I1 {key} sha256 mismatch')
    if n != meta['legs']:
        problems.append(f'I1 line count {n} != meta legs {meta["legs"]}')
    if meta['v'] != EXTRACTOR_VERSION:
        warnings.append(f'I1 extractor v{meta["v"]} (current v{EXTRACTOR_VERSION}); reprocess pending')
    if not meta.get('allowlist'):
        problems.append('I1 allowlist off')
    thin = 12000 if era(day) == '2023' else 25000
    if meta.get('scanned', 0) < thin:
        warnings.append(f"W17 thin day: {meta.get('scanned')} aircraft scanned (normal: ~42k in 2023, "
                        f"~60-76k in 2026); counts for this day are far below its real traffic")
    if meta.get('dup_members'):
        warnings.append(f"W16 upstream archive parts do not join: {meta['dup_members']} airframe(s) repeated "
                        f"after a {meta.get('tar_gap_bytes', 0) / 1024:.0f} KB unreadable region (skipped, one "
                        f"trace each); the parts were cut from different packings, so aircraft that sat at "
                        f"the joint are missing from this day")
    elif meta.get('tar_gaps'):
        warnings.append(f"W16 upstream archive irregular: {meta['tar_gaps']} region(s) unreadable as tar "
                        f"records, {meta.get('tar_gap_bytes', 0) / 1024:.0f} KB skipped (padding between joined "
                        f"archive pieces loses nothing; damage would lose any aircraft stored there)")
    med = neighbour_median_legs(day)
    if med and n < THIN_RELATIVE * med:
        warnings.append(f'W18 partial day: {n} legs, {n / med:.0%} of the surrounding median ({med:.0f}); '
                        f'its counts understate real traffic')
    src = source_check(day, meta.get('source'))
    if src:
        (problems if src[0] == 'red' else warnings).append(src[1])

    # I2-I5
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
    if sus > 0.10:
        problems.append(f'I5 sus {sus:.3f}')
    elif sus > 0.03:
        warnings.append(f'I5 sus {sus:.3f} (heavy GPS interference day?)')

    # I6
    dup = sum(1 for c in collections.Counter((r['hex'], r['t0']) for r in legs).values() if c > 1)
    if dup:
        problems.append(f'I6 {dup} duplicate (hex, t0)')

    # I7/I8/I10/W15 on countable departures
    by_hex = collections.defaultdict(list)
    for r in legs:
        by_hex[r['hex']].append(r)
    prev_end = {}
    for r in prev_legs or []:
        prev_end[r['hex']] = max(prev_end.get(r['hex'], 0), r['t1'])
    day0 = dt.datetime.fromisoformat(day + 'T00:00:00+00:00').timestamp()
    bad_pairs = deps_total = 0
    buckets = collections.Counter()
    anchor_deps = collections.Counter()
    for hx, ls in by_hex.items():
        deps = countable(ls, prev_end.get(hx))
        deps_total += len(deps)
        for a, c in zip(deps, deps[1:]):
            if c['t0'] - a['t1'] < 15 * 60:
                bad_pairs += 1
        for r in deps:
            buckets[int((r['t0'] - day0) // 300)] += 1
            if r['cs'][:3] in ANCHORS:
                anchor_deps[r['cs'][:3]] += 1
    if bad_pairs:
        problems.append(f'I7 {bad_pairs} impossible back-to-back departures')
    mid = [buckets[i] for i in range(24, 264)]
    mid_avg = sum(mid) / max(1, len(mid))
    if prev_legs is None:
        warnings.append('W8 midnight guard disarmed (previous day unpublished)')
    elif buckets[0] > 2 * mid_avg and buckets[0] > 20:
        problems.append(f'I8 first-5-min departures {buckets[0]} vs mid-day avg {mid_avg:.0f}')
    dl = deps_total / max(1, n)
    if not 0.40 <= dl <= 0.85:
        problems.append(f'I10 departures/legs {dl:.2f}')
    dead = [a for a in ANCHORS if anchor_deps[a] == 0]
    if dead:
        warnings.append(f'W15 anchor carriers with zero departures: {dead}')

    # I9 ends quality + paths
    missing_q = sum(1 for r in legs if (r['org'] and not r.get('oq')) or (r['dst'] and not r.get('dq')))
    if missing_q:
        problems.append(f'I9 {missing_q} legs missing end quality')
    pids, pbad_pts, pbad_order = set(), 0, 0
    for line in gzip.open(files['paths'], 'rt'):
        p = json.loads(line)
        pids.add(p['id'])
        pts = sum(len(s) for s in p['segs'])
        if pts > 150:
            pbad_pts += 1
        for seg in p['segs']:
            if any(c[2] < a[2] for a, c in zip(seg, seg[1:])):
                pbad_order += 1
    fids = {r['id'] for r in legs}
    if pids != fids:
        problems.append(f'I9 flights/paths ids differ ({len(fids ^ pids)} unmatched)')
    if pbad_pts:
        problems.append(f'I9 {pbad_pts} paths over 150 points')
    if pbad_order:
        problems.append(f'I9 {pbad_order} segments with decreasing timestamps')

    # I11 chain continuity
    ok = tot = 0
    for hx, ls in by_hex.items():
        ls.sort(key=lambda x: x['t0'])
        for a, c in zip(ls, ls[1:]):
            if a['dst'] and c['org']:
                tot += 1
                ok += a['dst'] == c['org']
    if tot >= 200:
        cont = ok / tot
        if cont < CHAIN_RED:
            problems.append(f'I11 chain continuity {cont:.3f}')
        elif cont < b['chain_warn']:
            warnings.append(f"I11 chain continuity {cont:.3f} (era warning line {b['chain_warn']})")

    # W12 volume collapse vs yesterday
    if prev_legs is not None:
        prev_top = collections.Counter(r['cs'][:3] for r in prev_legs).most_common(30)
        today_by = collections.Counter(r['cs'][:3] for r in legs)
        collapsed = [p for p, c in prev_top if c >= 100 and today_by[p] < 0.3 * c]
        if collapsed:
            warnings.append(f'W12 prefix volume collapse: {collapsed}')

    # W13 excluded-prefix drift vs yesterday's meta
    if prev_meta is not None:
        prev_top = prev_meta.get('excluded_top', {})
        floor10 = min(prev_top.values()) if len(prev_top) >= 10 else 0
        new_big = [p for p, c in meta.get('excluded_top', {}).items()
                   if c >= 300 and p not in prev_top and floor10 < c / 2]
        if new_big:
            warnings.append(f'W13 new large excluded prefixes: {new_big}')

    # W14 week-over-week drift
    week_ago = (dt.date.fromisoformat(day) - dt.timedelta(days=7)).isoformat()
    old = history.get(week_ago)
    if old and old.get('stats'):
        for k, cur in (('ratio', ratio), ('tk', tk)):
            was = old['stats'].get(k)
            if was and abs(cur - was) / was > 0.15:
                warnings.append(f'W14 {k} {cur:.2f} vs {was:.2f} a week earlier')

    stats = {'legs': n, 'tk': round(tk, 2), 'ld': round(ld, 2), 'sus': round(sus, 3),
             'ratio': round(ratio, 2), 'deps': deps_total, 'deps_legs': round(dl, 2),
             'chain': round(ok / max(1, tot), 3) if tot else None}
    return problems, warnings, stats, meta, legs


def verify_one(day, dest, history):
    """Verify one published day; returns the record (already written)."""
    files = fetch(day, dest)
    if not files:
        return None
    prev_day = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
    prev = fetch(prev_day, dest)
    prev_legs = prev_meta = None
    if prev:
        prev_legs = [json.loads(l) for l in gzip.open(prev['flights'], 'rt')]
        prev_meta = json.load(open(prev['meta']))
    try:
        problems, warnings, stats, meta, _ = check_day(day, files, prev_legs, prev_meta, history)
        rec = {'day': day, 'mode': 'daily', 'verdict': 'RED' if problems else 'clean',
               'extractor_v': meta['v'], 'problems': problems, 'warnings': warnings,
               'stats': stats, 'explanation': None}
    except Exception as e:  # a crashing verifier must still leave a red row
        rec = {'day': day, 'mode': 'daily', 'verdict': 'RED', 'extractor_v': None,
               'problems': [f'verifier exception: {type(e).__name__}: {e}'],
               'warnings': [], 'stats': {}, 'explanation': None}
    write_rec(rec)
    return rec


def load_history():
    hist = {}
    for p in glob.glob(os.path.join(DAYS_DIR, '*.json')):
        try:
            r = json.load(open(p))
            hist[r['day']] = r
        except Exception:
            continue
    return hist


def hole_reason(day):
    try:
        from plan import HOLES
        if day in HOLES:
            return f'upstream hole (scripts/upstream_holes.txt): {HOLES[day]}'
    except Exception:
        pass
    return 'adsb.lol has no archive for this day'


def published_days():
    from plan import published_here
    return published_here()


def mode_daily():
    dest = os.environ.get('RUNNER_TEMP', '/tmp')
    history = load_history()
    published = published_days()
    versions = manifest_versions()
    today = today_utc()
    if clock_start() is None:
        os.makedirs(os.path.dirname(CLOCK), exist_ok=True)
        with open(CLOCK, 'w') as f:
            json.dump({'start': (today - dt.timedelta(days=1)).isoformat(),
                       'set_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                       'note': 'Quarantine clock: the data-day of the first daily verification '
                               'run. The streak counts from here; set once, never moved.'},
                      f, indent=1, sort_keys=True)
            f.write('\n')
        print(f'quarantine clock started at {(today - dt.timedelta(days=1)).isoformat()}')
    new_red = []
    for back in range(17, 0, -1):
        day = (today - dt.timedelta(days=back)).isoformat()
        have = history.get(day)
        # Re-verify a recorded day only when its published files were rebuilt
        # by a newer extractor (MANIFEST says so); otherwise its record stands.
        rebuilt = (have and have.get('extractor_v') and versions.get(day)
                   and versions[day] > have['extractor_v'])
        # Re-arm: a day verified while its previous day was unpublished ran
        # without the midnight checks (W8). Once that previous day exists,
        # verify again so I7/I8 actually run (2026-11-09: four days, one of
        # them a streak day, were recorded clean with the guard disarmed).
        prev = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
        rearm = (have and prev in published
                 and any(w.startswith('W8') for w in have.get('warnings', [])))
        if have and not rebuilt and not rearm:
            continue
        if day in published:
            rec = verify_one(day, dest, history)
            if rec:
                print(f"{day}: {rec['verdict']}" + (f" {rec['problems']}" if rec['problems'] else ''))
                for w in rec['warnings']:
                    print(f'::warning::{day}: {w}')
                if rec['verdict'] == 'RED':
                    new_red.append(day)
            else:
                rec = {'day': day, 'mode': 'daily', 'verdict': 'RED', 'extractor_v': None,
                       'problems': ['published but not fetchable (ghost or missing asset)'],
                       'warnings': [], 'stats': {}, 'explanation': None}
                write_rec(rec)
                new_red.append(day)
        elif back >= 2 and not upstream_has(day):
            write_rec({'day': day, 'mode': 'daily', 'verdict': 'SKIPPED', 'extractor_v': None,
                       'problems': [], 'warnings': [], 'stats': {},
                       'explanation': hole_reason(day)})
            print(f'{day}: SKIPPED (no upstream archive)')
        # else: unpublished but healable - no record yet; the streak tolerates
        # gaps younger than 4 days, and the publish alarm covers persistence.
    if new_red:
        print(f"::error::red day(s): {', '.join(new_red)}")
        sys.exit(1)


def mode_range(start, end):
    dest = os.environ.get('RUNNER_TEMP', '/tmp')
    history = load_history()
    published = published_days()
    day = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    reds = 0
    while day <= last:
        d = day.isoformat()
        if d in published:
            rec = verify_one(d, dest, history)
            if rec is None:
                write_rec({'day': d, 'mode': 'range', 'verdict': 'RED', 'extractor_v': None,
                           'problems': ['published but not fetchable'], 'warnings': [],
                           'stats': {}, 'explanation': None})
                reds += 1
            else:
                rec['mode'] = 'range'
                write_rec(rec)
                if rec['verdict'] == 'RED':
                    reds += 1
                    print(f"::error::{d}: {rec['problems']}")
                else:
                    print(f'{d} clean')
        elif d not in history and not upstream_has(d):
            write_rec({'day': d, 'mode': 'range', 'verdict': 'SKIPPED', 'extractor_v': None,
                       'problems': [], 'warnings': [], 'stats': {},
                       'explanation': hole_reason(d)})
            print(f'{d}: SKIPPED (no upstream archive)')
        day += dt.timedelta(days=1)
    # Records are on disk; the WORKFLOW commits them before honouring this.
    if reds:
        print(f'::error::{reds} day(s) failed verification')
        sys.exit(1)


def streak_of(history, start, today):
    """The frozen streak rule (module docstring), as code."""
    if not start:
        return 0
    first = dt.date.fromisoformat(start)
    cursor = today - dt.timedelta(days=1)
    streak = 0
    while cursor >= first:
        r = history.get(cursor.isoformat())
        if r is None:
            if (today - cursor).days > 3:
                break          # an old hole: the pipeline failed that day
        elif r['verdict'] == 'SKIPPED':
            pass               # no upstream archive: neither counts nor breaks
        elif r['verdict'] == 'clean' and r.get('extractor_v') == EXTRACTOR_VERSION:
            streak += 1
        else:
            break              # a RED, or a day not yet re-verified after a version change
        cursor -= dt.timedelta(days=1)
    return streak


def load_drills():
    out = []
    for p in sorted(glob.glob(os.path.join(DRILLS_DIR, '*.json'))):
        try:
            out.append(json.load(open(p)))
        except Exception:
            continue
    return out


def render():
    history = load_history()
    days = sorted(history)
    start = clock_start()
    today = today_utc()
    streak = streak_of(history, start, today)
    unexplained = [d for d in days if history[d]['verdict'] == 'RED' and not history[d].get('explanation')]
    lines = [
        '# Quarantine log',
        '',
        'GENERATED by scripts/verify.py render; edit the per-day files in',
        'quarantine/days/, never this table. The site reads nothing from this',
        "pipeline until the exit criteria hold (owner's rule, 2026-11-09).",
        '',
        'THE STREAK, frozen definition: scanning back from yesterday (UTC) to the',
        'quarantine clock start, the count of days with a CLEAN record under the',
        'current extractor version. The clock starts at the data-day of the first',
        'daily verification run, so the streak grows by at most one per day of',
        'operation; catch-up and backfilled days are verified but never count.',
        'SKIPPED-no-upstream days neither count nor break; a RED breaks it; a',
        'recordless day breaks it once 4+ days old. Changing any counting constant',
        'in verify.py or aggregate.py is an owner-visible commit and RESTARTS the',
        'streak. Exit needs 30, with every RED explained.',
        '',
        f'**Current streak: {streak} of 30 clean day(s).** Clock started: {start or "not yet"}. '
        f'Extractor v{EXTRACTOR_VERSION}. Unexplained reds: {len(unexplained)}.',
        '',
        '| day | mode | verdict | legs | tk | sus | deps | problems | warnings | explanation |',
        '|---|---|---|---|---|---|---|---|---|---|',
    ]
    for d in reversed(days):
        r = history[d]
        s = r.get('stats') or {}
        lines.append('| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |'.format(
            d, r.get('mode', '?'), r['verdict'], s.get('legs', ''), s.get('tk', ''),
            s.get('sus', ''), s.get('deps', ''),
            '; '.join(r.get('problems', [])) or '-',
            '; '.join(r.get('warnings', [])) or '-',
            (r.get('explanation') or '-').replace('|', '/')))
    drills = load_drills()
    if drills:
        lines += ['', '## Drills (deliberate test reds; never a data-day)', '',
                  '| fired at | purpose |', '|---|---|']
        for dr in drills:
            lines.append(f"| {dr.get('fired_at', '?')} | {dr.get('purpose', '-')} |")
    with open(LOG, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'rendered {len(days)} day(s); streak {streak}; clock {start}')


def main():
    mode = sys.argv[1]
    if mode == 'daily':
        mode_daily()
    elif mode == 'range':
        mode_range(sys.argv[2], sys.argv[3])
    elif mode == 'render':
        render()
    elif mode == 'explain':
        rec = load_rec(sys.argv[2])
        if not rec:
            sys.exit(f'no record for {sys.argv[2]}')
        rec['explanation'] = sys.argv[3]
        write_rec(rec, annotate=True)
        print(f'explanation recorded for {sys.argv[2]}')
    elif mode == 'drill':
        # A drill is recorded apart from the data-days, so proving the alarm
        # channel can never break the streak it exists to protect.
        os.makedirs(DRILLS_DIR, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        with open(os.path.join(DRILLS_DIR, stamp.replace(':', '') + '.json'), 'w') as f:
            json.dump({'fired_at': stamp, 'purpose': 'deliberate test of the failure-email channel'},
                      f, indent=1, sort_keys=True)
            f.write('\n')
        print('::error::DRILL red (deliberate test of the failure email; not a data problem)')
        sys.exit(1)
    else:
        sys.exit('usage: verify.py daily | range START END | render | explain DAY TEXT | drill')


if __name__ == '__main__':
    main()
