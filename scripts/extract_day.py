#!/usr/bin/env python3
"""Extract every airline flight from one day of adsb.lol globe_history.

Reads the day's tar on stdin (the release parts .tar.aa + .tar.ab, concatenated)
and writes two gzip JSON-lines files into OUT_DIR:

  flights-YYYY-MM-DD.jsonl.gz  one line per flight leg (see SCHEMA in README.md)
  paths-YYYY-MM-DD.jsonl.gz    each leg's simplified path, keyed by the same id

Source data: adsb.lol globe_history, (c) adsb.lol contributors, ODbL 1.0.
Only airline-format callsigns are kept (three letters + digits); military and
non-ICAO addresses are skipped. Nothing here is a timetable: times are when the
receivers saw the aircraft, and every count is a floor (receivers miss things,
especially over oceans).

Usage: cat day.tar.aa day.tar.ab | python3 extract_day.py YYYY-MM-DD OUT_DIR
"""
import gzip
import json
import math
import multiprocessing as mp
import os
import re
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
AIRLINE_CS = re.compile(r'^[A-Z]{3}[0-9]{1,4}[A-Z]{0,2}$')
NEW_FLIGHT_GAP_S = 45 * 60
LOW_FT = 10000
FT_PER_KM_GLIDE = 172.0
TURN_KMH = float(os.environ.get('TURN_KMH', '400'))  # a 3-degree path loses ~172 ft per km of ground distance

_AIRPORTS = None
_GRID = None


def load_airports():
    global _AIRPORTS, _GRID
    if _AIRPORTS is not None:
        return
    rows = json.load(open(os.path.join(HERE, 'airports.json')))
    _AIRPORTS = [(code, lat, lon, elev or 0) for code, _icao, lat, lon, elev in rows]
    _GRID = {}
    for a in _AIRPORTS:
        _GRID.setdefault((math.floor(a[1]), math.floor(a[2])), []).append(a)


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
            # Gone 45+ minutes and back much closer than it could have flown in
            # that time (under 400 km/h on average): it landed somewhere unseen
            # and turned around. A long ocean crossing keeps cruise speed.
            turned_around = TURN_KMH > 0 and gap >= 45 * 60 and km(prev[1], prev[2], p[1], p[2]) < TURN_KMH * gap / 3600.0
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


def touchdown_at(p):
    """True when a point is on the ground, or within 1,500 ft of an airport's
    elevation inside its glide path: a landing even if the ground is unseen."""
    if p[4]:
        return True
    if p[3] is None or p[3] > 6000:
        return False
    for code, alat, alon, elev in nearby(p[1], p[2]):
        if p[3] - elev <= 1500 and km(p[1], p[2], alat, alon) <= 3.0 + 1.3 * max(0.0, p[3] - elev) / FT_PER_KM_GLIDE:
            return True
    return False


def split_touchdowns(leg):
    """Split a leg at an interior touchdown that has real flight (5,000 ft+) on
    both sides, so a quick turnaround is two flights, not one round trip."""
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


def end_airport(leg, from_start):
    seq = leg if from_start else leg[::-1]
    for p in seq[:60]:
        if low(p):
            code = airport_for(p[1], p[2], p[3], p[4])
            if code:
                return code
        else:
            return None
    return None


def rdp(pts, eps):
    """Ramer-Douglas-Peucker on [lat, lon] with longitude scaled by cos(lat)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (y1, x1), (y2, x2) = pts[i], pts[j]
        c = math.cos(math.radians((y1 + y2) / 2))
        dx, dy = (x2 - x1) * c, (y2 - y1)
        norm = math.hypot(dx, dy) or 1e-12
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            y, x = pts[k]
            dist = abs(dy * (x - x1) * c - dx * (y - y1)) / norm
            if dist > best:
                best, idx = dist, k
        if best > eps and idx > 0:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(pts, keep) if k]


def simplify(path):
    eps = 0.03
    out = rdp(path, eps)
    while len(out) > 150:
        eps *= 1.6
        out = rdp(path, eps)
    return [[round(a, 3), round(b, 3)] for a, b in out]


def leg_record(day, meta, n, leg):
    calls = [p[6] for p in leg if p[6]]
    cs = max(set(calls), key=calls.count) if calls else None
    if not cs or not AIRLINE_CS.match(cs):
        return None
    air = [p for p in leg if not p[4] and p[3] is not None]
    if len(air) < 5 or max(p[3] for p in air) < 3000:
        return None
    path = [(p[1], p[2]) for p in leg]
    flown = sum(km(path[i][0], path[i][1], path[i + 1][0], path[i + 1][1]) for i in range(len(path) - 1))
    gaps = [leg[i + 1][0] - leg[i][0] for i in range(len(leg) - 1)]
    span = max(1, air[-1][0] - air[0][0])
    covered = sum(g for g in gaps if g <= 300)
    org = end_airport(leg, True)
    dst = end_airport(leg, False)
    fid = f"{day}-{meta['icao']}-{n}"
    rec = {
        'id': fid, 'hex': meta['icao'], 'reg': meta.get('r'), 'type': meta.get('t'), 'cs': cs,
        'org': org, 'dst': dst,
        'tk': low(leg[0]), 'ld': low(leg[-1]),  # takeoff / landing actually seen
        't0': int(air[0][0]), 't1': int(air[-1][0]),
        'km': round(flown), 'alt': int(max(p[3] for p in air)),
        'gap': round(max(gaps, default=0) / 60), 'cov': round(min(1.0, covered / span), 2),
    }
    return rec, {'id': fid, 'p': simplify(path)}


def process(item):
    day, name, raw = item
    load_airports()
    try:
        data = json.loads(gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw)
    except Exception:
        return []
    if data.get('dbFlags', 0) & 1:  # military
        return []
    base = data.get('timestamp', 0)
    points = []
    for p in data.get('trace') or []:
        if len(p) < 7:
            continue
        ground = p[3] == 'ground'
        alt = None if ground or not isinstance(p[3], (int, float)) else p[3]
        det = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
        cs = (det.get('flight') or '').strip() if det else ''
        points.append((base + p[0], p[1], p[2], alt, ground, p[6] or 0, cs))
    out = []
    n = 0
    for leg in split_legs(points):
        if len(leg) < 8:
            continue
        r = leg_record(day, data, n, leg)
        if r:
            out.append(r)
            n += 1
    return out


def members(day, stream):
    with tarfile.open(fileobj=stream, mode='r|') as tar:
        for m in tar:
            if m.isfile() and 'trace_full_' in m.name and '~' not in m.name:
                yield (day, m.name, tar.extractfile(m).read())


def main():
    day, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, f'flights-{day}.jsonl.gz')
    ppath = os.path.join(out_dir, f'paths-{day}.jsonl.gz')
    flights = paths = 0
    with mp.Pool(max(1, os.cpu_count() or 1)) as pool, \
            gzip.open(fpath, 'wt', compresslevel=9) as ff, gzip.open(ppath, 'wt', compresslevel=9) as pf:
        for result in pool.imap_unordered(process, members(day, sys.stdin.buffer), chunksize=64):
            for rec, path in result:
                ff.write(json.dumps(rec, separators=(',', ':')) + '\n')
                pf.write(json.dumps(path, separators=(',', ':')) + '\n')
                flights += 1
                paths += 1
    sys.stderr.write(f'{day}: {flights} airline flight legs\n')


if __name__ == '__main__':
    main()
