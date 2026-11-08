#!/usr/bin/env python3
"""Extract every airline flight from one day of adsb.lol globe_history.

Reads the day's tar on stdin (the release parts concatenated) and writes three
files into OUT_DIR:

  flights-YYYY-MM-DD.jsonl.gz  one line per flight leg (schema in README.md)
  paths-YYYY-MM-DD.jsonl.gz    each leg's observed track as time-stamped
                               segments, split at coverage gaps, so a gap can
                               never be drawn as if it were an observed line
  meta-YYYY-MM-DD.json         extractor version, airports revision, counts

Source data: adsb.lol globe_history, (c) adsb.lol contributors, ODbL 1.0.
Only airline-format callsigns are kept (three letters + digits); military and
non-ICAO addresses are skipped. If scripts/airlines.txt exists, only callsign
prefixes listed there are kept (one 3-letter ICAO designator per line, #
comments allowed). Nothing here is a timetable: times are when receivers saw
the aircraft, and every count is a floor.

Usage: cat day.tar.aa day.tar.ab | python3 extract_day.py YYYY-MM-DD OUT_DIR
"""
import gzip
import hashlib
import io
import json
import math
import multiprocessing as mp
import os
import re
import sys
import tarfile

EXTRACTOR_VERSION = 3

HERE = os.path.dirname(os.path.abspath(__file__))
AIRLINE_CS = re.compile(r'^[A-Z]{3}[0-9]{1,4}[A-Z]{0,2}$')
HEX_RE = re.compile(r'^[0-9a-f]{6}$')
REG_RE = re.compile(r'^[A-Z0-9-]{1,10}$')
TYPE_RE = re.compile(r'^[A-Z0-9-]{2,8}$')
NEW_FLIGHT_GAP_S = 45 * 60
LOW_FT = 10000
FT_PER_KM_GLIDE = 172.0   # a 3-degree path loses ~172 ft per km of ground distance
TURN_KMH = 400.0          # gone 45+ min and back closer than this average = landed unseen
PATH_GAP_S = 300          # a path segment never spans a gap longer than this
ABSURD_KMH = 1500.0       # three consecutive legs of this ground speed = spoofed trace
MAX_RAW_MEMBER = 64 * 1024 * 1024
MAX_DECOMPRESSED = 256 * 1024 * 1024

_AIRPORTS = None
_GRID = None
_ALLOW = None


def load_airports():
    global _AIRPORTS, _GRID, _ALLOW
    if _AIRPORTS is not None:
        return
    rows = json.load(open(os.path.join(HERE, 'airports.json')))
    _AIRPORTS = [(code, lat, lon, elev or 0) for code, _icao, lat, lon, elev in rows]
    _GRID = {}
    for a in _AIRPORTS:
        _GRID.setdefault((math.floor(a[1]), math.floor(a[2])), []).append(a)
    allow_path = os.path.join(HERE, 'airlines.txt')
    if os.path.exists(allow_path):
        _ALLOW = {ln.strip().upper() for ln in open(allow_path)
                  if ln.strip() and not ln.startswith('#') and len(ln.strip()) == 3}
    else:
        _ALLOW = None


def airports_rev():
    with open(os.path.join(HERE, 'airports.json'), 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:8]


def km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def nearby(lat, lon):
    gy, gx = math.floor(lat), math.floor(lon)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            yield from _GRID.get((gy + dy, (gx + dx + 180) % 360 - 180), ())


def airport_for(lat, lon, alt_ft, ground):
    """The airport this low point belongs to, judged by a generous glide path:
    on the ground within 4 km; airborne, within 3 km plus 1.3x the distance a
    3-degree path covers from that height. Nearest qualifying airport wins."""
    best, best_d = None, 1e9
    for code, alat, alon, elev in nearby(lat, lon):
        d = km(lat, lon, alat, alon)
        if ground:
            ok = d <= 4.0
        else:
            ok = d <= 3.0 + 1.3 * max(0.0, (alt_ft or 0) - elev) / FT_PER_KM_GLIDE
        if ok and d < best_d:
            best, best_d = code, d
    return best


def low(p):
    return p[4] or (p[3] is not None and p[3] < LOW_FT)


def touchdown_at(p):
    """On the ground, or within 1,500 ft of an airport's elevation inside its
    glide path: a landing even where receivers cannot see the ground."""
    if p[4]:
        return True
    if p[3] is None or p[3] > 6000:
        return False
    for code, alat, alon, elev in nearby(p[1], p[2]):
        if p[3] - elev <= 1500 and km(p[1], p[2], alat, alon) <= 3.0 + 1.3 * max(0.0, p[3] - elev) / FT_PER_KM_GLIDE:
            return True
    return False


def split_touchdowns(leg):
    """Split a leg at an interior touchdown that has real flight (5,000 ft+)
    on both sides, so a quick turnaround is two flights, not one round trip."""
    high = [i for i, p in enumerate(leg) if p[3] is not None and p[3] >= 5000]
    if len(high) < 2:
        return [leg]
    first_high, last_high = high[0], high[-1]
    for i in range(first_high + 1, last_high):
        if touchdown_at(leg[i]):
            j = i
            while j + 1 < last_high and touchdown_at(leg[j + 1]):
                j += 1
            return [leg[:j + 1]] + split_touchdowns(leg[j + 1:])
    return [leg]


def split_legs(points):
    legs, cur = [], []
    for p in points:
        if cur:
            prev = cur[-1]
            gap = p[0] - prev[0]
            new_leg = bool(p[5] & 2) and (low(prev) or low(p))
            stopped = prev[4] and gap > NEW_FLIGHT_GAP_S
            # Lost while low, back 20+ minutes later: it landed somewhere the
            # receivers could not see the ground.
            unseen_stop = low(prev) and gap >= 20 * 60
            # Gone 45+ minutes and back much closer than it could have flown
            # in that time: it landed unseen and turned around. A long ocean
            # crossing keeps cruise speed and so never trips this.
            turned_around = gap >= 45 * 60 and km(prev[1], prev[2], p[1], p[2]) < TURN_KMH * gap / 3600.0
            if new_leg or stopped or unseen_stop or turned_around:
                legs.append(cur)
                cur = []
        cur.append(p)
    if cur:
        legs.append(cur)
    out = []
    for leg in legs:
        out.extend(split_touchdowns(leg))
    return out


AIRPORT_ELEV = None


def elev_of(code):
    global AIRPORT_ELEV
    if AIRPORT_ELEV is None:
        AIRPORT_ELEV = {a[0]: a[3] for a in _AIRPORTS}
    return AIRPORT_ELEV.get(code, 0)


def end_airport(leg, from_start):
    """The airport at this end plus the assignment quality: (code, agl_ft,
    distance_km) from the first low point that matched, or (None, None, None).
    Quality lets every consumer choose its own strictness without
    reprocessing: a route needs a near-and-low assignment, a rough label
    does not."""
    seq = leg if from_start else leg[::-1]
    for p in seq[:60]:
        if low(p):
            code = airport_for(p[1], p[2], p[3], p[4])
            if code:
                agl = 0 if p[4] else max(0, int((p[3] or 0) - elev_of(code)))
                dist = min(km(p[1], p[2], a[1], a[2]) for a in _AIRPORTS if a[0] == code)
                return code, agl, round(dist, 1)
        else:
            return None, None, None
    return None, None, None


def departure_seen(leg, org, org_agl, org_km):
    """True only when the departure itself was observed: the leg starts on
    the ground, or airborne below 3,000 ft above its assigned origin within
    10 km of it. A fragment re-acquired at altitude (a midnight-boundary
    continuation, a post-gap reacquisition, a go-around remnant) is NOT a
    departure, however low the fix: counting it would inflate the floor."""
    first = leg[0]
    if first[4]:
        return True
    return (org is not None and org_agl is not None and org_agl < 3000
            and org_km is not None and org_km <= 10.0)


def absurd_speed(leg):
    """Three consecutive inter-fix ground speeds above ABSURD_KMH: a spoofed
    or garbled trace. Real aircraft in the strongest jet streams stay under
    ~1,350 km/h over the ground."""
    run = 0
    for a, b in zip(leg, leg[1:]):
        dt = b[0] - a[0]
        if dt < 5:
            continue
        if km(a[1], a[2], b[1], b[2]) / dt * 3600.0 > ABSURD_KMH:
            run += 1
            if run >= 3:
                return True
        else:
            run = 0
    return False


def rdp(pts, eps):
    """Ramer-Douglas-Peucker on [lat, lon, dt] points (dt ignored for error,
    kept on the survivors), longitude scaled by cos(lat)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        y1, x1 = pts[i][0], pts[i][1]
        y2, x2 = pts[j][0], pts[j][1]
        c = math.cos(math.radians((y1 + y2) / 2))
        dx, dy = (x2 - x1) * c, (y2 - y1)
        norm = math.hypot(dx, dy) or 1e-12
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            y, x = pts[k][0], pts[k][1]
            dist = abs(dy * (x - x1) * c - dx * (y - y1)) / norm
            if dist > best:
                best, idx = dist, k
        if best > eps and idx > 0:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(pts, keep) if k]


def path_segments(leg, t0):
    """Observed track as segments that never span a coverage gap (> PATH_GAP_S)
    so a consumer cannot draw a chord across unseen sky. Points are
    [lat, lon, seconds_since_t0]."""
    segs, cur = [], []
    for a, b in zip([None] + leg[:-1], leg):
        if cur and a is not None and b[0] - a[0] > PATH_GAP_S:
            segs.append(cur)
            cur = []
        cur.append([b[1], b[2], int(b[0] - t0)])
    if cur:
        segs.append(cur)
    total = sum(len(s) for s in segs)
    eps = 0.03
    while True:
        out = [rdp(s, eps) if len(s) > 2 else s for s in segs]
        total = sum(len(s) for s in out)
        if total <= 150 or eps > 3:
            break
        eps *= 1.6
    return [[[round(p[0], 3), round(p[1], 3), p[2]] for p in s] for s in out]


def leg_record(day, meta, leg):
    calls = [p[6] for p in leg if p[6]]
    # Deterministic: highest count, ties broken alphabetically, so identical
    # input always publishes identical bytes (the meta file carries hashes).
    cs = max(sorted(set(calls)), key=calls.count) if calls else None
    if not cs or not AIRLINE_CS.match(cs):
        return None
    if _ALLOW is not None and cs[:3] not in _ALLOW:
        return None
    air = [p for p in leg if not p[4] and p[3] is not None]
    if len(air) < 5 or max(p[3] for p in air) < 3000:
        return None
    if absurd_speed(leg):
        return 'absurd'
    path = [(p[1], p[2]) for p in leg]
    flown = sum(km(path[i][0], path[i][1], path[i + 1][0], path[i + 1][1]) for i in range(len(path) - 1))
    gaps = [leg[i + 1][0] - leg[i][0] for i in range(len(leg) - 1)]
    # Coverage of the airborne window only: a gap is counted where it
    # overlaps [first airborne fix, last airborne fix], so ground taxi can
    # neither pad nor dilute it.
    a0, a1 = air[0][0], air[-1][0]
    span = max(1, a1 - a0)
    covered = 0
    for q, r in zip(leg, leg[1:]):
        if r[0] - q[0] <= PATH_GAP_S:
            overlap = min(r[0], a1) - max(q[0], a0)
            if overlap > 0:
                covered += overlap
    org, org_agl, org_km = end_airport(leg, True)
    dst, dst_agl, dst_km = end_airport(leg, False)
    t0 = int(a0)
    fid = f"{day}-{meta['icao']}-{t0}"
    rec = {
        'id': fid, 'v': EXTRACTOR_VERSION, 'hex': meta['icao'],
        'reg': meta.get('r'), 'type': meta.get('t'), 'cs': cs,
        'org': org, 'oq': [org_agl, org_km] if org else None,
        'dst': dst, 'dq': [dst_agl, dst_km] if dst else None,
        'tk': departure_seen(leg, org, org_agl, org_km),
        'ld': bool(leg[-1][4]) or (dst is not None and dst_agl is not None
                                   and dst_agl < 3000 and dst_km is not None and dst_km <= 10.0),
        't0': t0, 't1': int(a1),
        'km': round(flown), 'alt': int(max(p[3] for p in air)),
        'gap': round(max(gaps, default=0) / 60), 'cov': round(min(1.0, covered / span), 2),
    }
    return rec, {'id': fid, 'v': EXTRACTOR_VERSION, 'segs': path_segments(leg, t0)}


def clean_ident(value, pattern):
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    return v if pattern.match(v) else None


def process(item):
    """One aircraft's day. Never raises: a malformed trace is one skipped
    file, not a dead day."""
    day, raw = item
    load_airports()
    try:
        if raw[:2] == b'\x1f\x8b':
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as g:
                blob = g.read(MAX_DECOMPRESSED + 1)
            if len(blob) > MAX_DECOMPRESSED:
                return [], 'oversize'
        else:
            blob = raw
        data = json.loads(blob)
        if not isinstance(data, dict) or data.get('dbFlags', 0) & 1:
            return [], None
        hexid = data.get('icao')
        if not isinstance(hexid, str) or not HEX_RE.match(hexid.strip().lower()):
            return [], 'bad_meta'
        meta = {
            'icao': hexid.strip().lower(),
            'r': clean_ident(data.get('r'), REG_RE),
            't': clean_ident(data.get('t'), TYPE_RE),
        }
        base = data.get('timestamp', 0)
        if not isinstance(base, (int, float)):
            return [], 'bad_meta'
        points = []
        for p in data.get('trace') or []:
            if not isinstance(p, list) or len(p) < 7:
                continue
            if not isinstance(p[0], (int, float)) or not isinstance(p[1], (int, float)) or not isinstance(p[2], (int, float)):
                continue
            if not (-90 <= p[1] <= 90 and -180 <= p[2] <= 180):
                continue
            ground = p[3] == 'ground'
            alt = p[3] if not ground and isinstance(p[3], (int, float)) else None
            det = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
            cs = det.get('flight') if det else None
            cs = cs.strip() if isinstance(cs, str) else ''
            flags = p[6] if isinstance(p[6], int) else 0
            points.append((base + p[0], p[1], p[2], alt, ground, flags, cs))
        out, absurd = [], 0
        for leg in split_legs(points):
            if len(leg) < 8:
                continue
            r = leg_record(day, meta, leg)
            if r == 'absurd':
                absurd += 1
            elif r:
                out.append(r)
        return out, ('absurd', absurd) if absurd else None
    except Exception:
        return [], 'error'


def members(day, stream, stats):
    with tarfile.open(fileobj=stream, mode='r|') as tar:
        for m in tar:
            if not m.isfile() or 'trace_full_' not in m.name or '~' in m.name:
                continue
            if m.size > MAX_RAW_MEMBER:
                stats['oversize'] += 1
                continue
            stats['scanned'] += 1
            yield (day, tar.extractfile(m).read())


def main():
    day, out_dir = sys.argv[1], sys.argv[2]
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
        sys.exit(f'bad day: {day}')
    os.makedirs(out_dir, exist_ok=True)
    load_airports()
    stats = {'scanned': 0, 'legs': 0, 'errors': 0, 'bad_meta': 0, 'oversize': 0, 'absurd_legs': 0}
    fpath = os.path.join(out_dir, f'flights-{day}.jsonl.gz')
    ppath = os.path.join(out_dir, f'paths-{day}.jsonl.gz')
    with mp.Pool(max(1, os.cpu_count() or 1)) as pool, \
            gzip.open(fpath, 'wt', compresslevel=9) as ff, gzip.open(ppath, 'wt', compresslevel=9) as pf:
        for result, note in pool.imap_unordered(process, members(day, sys.stdin.buffer, stats), chunksize=64):
            if note == 'error':
                stats['errors'] += 1
            elif note == 'bad_meta':
                stats['bad_meta'] += 1
            elif note == 'oversize':
                stats['oversize'] += 1
            elif isinstance(note, tuple) and note[0] == 'absurd':
                stats['absurd_legs'] += note[1]
            for rec, path in result:
                ff.write(json.dumps(rec, separators=(',', ':')) + '\n')
                pf.write(json.dumps(path, separators=(',', ':')) + '\n')
                stats['legs'] += 1
    meta = {
        'day': day, 'v': EXTRACTOR_VERSION, 'airports_rev': airports_rev(),
        'allowlist': _ALLOW is not None, **stats,
        'sha256': {os.path.basename(p): hashlib.sha256(open(p, 'rb').read()).hexdigest() for p in (fpath, ppath)},
    }
    with open(os.path.join(out_dir, f'meta-{day}.json'), 'w') as mf:
        json.dump(meta, mf, indent=1)
    sys.stderr.write(json.dumps({k: v for k, v in meta.items() if k != 'sha256'}) + '\n')


if __name__ == '__main__':
    main()
