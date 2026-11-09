#!/usr/bin/env python3
"""Find adsb.lol archive days, and which days this repo still needs.

  plan.py one [YYYY-MM-DD]    daily mode (empty arg = scheduled run)
  plan.py range START END     backfill mode

Daily mode (scheduled, no explicit date), with D = yesterday UTC, writes a
GitHub-output `matrix` of up to three days to process:
  - D-2 and D-1: processed from ANY archive instance if this repo has not
    published them (self-heal, never an alarm).
  - D: processed only from the main network (prod-0); when prod-0 has not
    published yet, a warning, exit 0 — adsb.lol is merely late.
  - RED (exit 1) only when D-1 is neither published here nor available from
    any instance: two consecutive unpublished days means the pipeline or the
    source is dead. That is the one alarm this job raises.
With an explicit date (a human asked): that day only, any instance, red when
absent.

Range mode diffs START..END against this repo's releases (asset state checked)
and MANIFEST.json (extractor version checked), newest first, capped at 250
days per run and at END <= today-2 (the daily job owns the fresh edge).

The adsb.lol archive keeps one release per receiver instance per day. prod-0
(the main network) is preferred; the others fill days it lacks.
"""
import datetime as dt
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from extract_day import EXTRACTOR_VERSION

SOURCES = ['adsblol/globe_history_2023', 'adsblol/globe_history_2024',
           'adsblol/globe_history_2025', 'adsblol/globe_history_2026']
PREFERENCE = ['prod-0', 'prod-0tmp', 'staging-0', 'prod-1', 'staging-0tmp', 'test-0', 'test-1']
TAG = re.compile(r'^v(\d{4})\.(\d{2})\.(\d{2})-planes-readsb-(.+)$')
ASSET_URL = re.compile(r'^https://github\.com/adsblol/globe_history_\d{4}/releases/download/[A-Za-z0-9._+-]+/[A-Za-z0-9._%+-]+$')
OWN = os.environ.get('GITHUB_REPOSITORY', 'psients/ihateflying-opendata')
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_MATRIX = 250  # GitHub allows 256 jobs per matrix


def load_holes():
    """scripts/upstream_holes.txt: days whose upstream archive is missing or
    unrecoverable, with the reason. Never scheduled; verify.py records them
    SKIPPED with that reason. Adding a day is an owner-visible commit."""
    holes = {}
    try:
        for line in open(os.path.join(HERE, 'upstream_holes.txt')):
            line = line.strip()
            if line and not line.startswith('#'):
                day, _, reason = line.partition(' ')
                holes[day] = reason.strip()
    except FileNotFoundError:
        pass
    return holes


HOLES = load_holes()


def load_overrides():
    """scripts/source_overrides.txt: days whose preferred archive instance is
    unusable, with the instance to use instead and why. Owner-visible."""
    over = {}
    try:
        for line in open(os.path.join(HERE, 'source_overrides.txt')):
            line = line.strip()
            if line and not line.startswith('#'):
                parts = line.split(None, 2)
                over[parts[0]] = parts[1]
    except FileNotFoundError:
        pass
    return over


OVERRIDES = load_overrides()


def api(path, accept='application/vnd.github+json'):
    """GET api.github.com/<path>. 404 -> None; other errors retried 3x."""
    headers = {'Accept': accept, 'User-Agent': 'ihateflying-opendata'}
    if os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    for attempt in range(4):
        try:
            req = urllib.request.Request('https://api.github.com/' + path, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 3:
                raise
        # URLError alone missed dropped connections: http.client's
        # RemoteDisconnected / IncompleteRead and socket resets are OSError or
        # HTTPException, not URLError (2026-11-09: an I12 lookup died on one).
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            if attempt == 3:
                raise
        time.sleep(10 * (attempt + 1))


def paged(path):
    page, out = 1, []
    while True:
        chunk = api(f'{path}?per_page=100&page={page}') or []
        out.extend(chunk)
        if len(chunk) < 100:
            return out
        page += 1


def parse_day(s):
    return dt.date.fromisoformat(s)


def archive_assets(release):
    """The release's archive as ONE form: the split parts (NAME.tar.aa,
    .ab, ...) or the single NAME.tar - never both. Five upstream releases
    hold both forms (2024-03-22 and 2024-04-22 prod-0, 2024-09-16/17 and
    2026-06-06 staging-0); joining every '.tar' asset fed the extractor the
    day twice (2024-04-22: 62,483 duplicate legs). When both exist, the
    form with more bytes wins (2024-03-22: the split original 2,008 MB vs a
    252 MB re-upload). Any other '.tar' name = no archive (fail closed)."""
    if not release:
        return []
    tag = release.get('tag_name', '')
    ok = [a for a in release.get('assets', [])
          if a.get('state') == 'uploaded' and a.get('size', 0) > 0 and '.tar' in a['name']]
    split = sorted((a for a in ok if re.fullmatch(re.escape(tag) + r'\.tar\.[a-z]{2}', a['name'])),
                   key=lambda a: a['name'])
    plain = [a for a in ok if a['name'] == tag + '.tar']
    if len(split) + len(plain) != len(ok):
        return []
    # Split parts are exactly .aa, .ab, .ac ... in sequence (a .tar.gz
    # or a gap in the sequence is not a split archive).
    want = [f'{tag}.tar.a{chr(ord("a") + i)}' for i in range(len(split))]
    if [a['name'] for a in split] != want:
        return []
    if split and plain:
        return split if sum(a['size'] for a in split) >= plain[0]['size'] else plain
    return split or plain


def urls_of(release):
    urls = [a['browser_download_url'] for a in archive_assets(release)]
    if not urls or not all(ASSET_URL.match(u) for u in urls):
        return None
    return ' '.join(urls)


def instance_urls(day, inst):
    """One instance's release URLs for a day, or None (ignores holes and
    overrides: measure.yml uses it to compare instances)."""
    y = str(day.year)
    repos = [r for r in SOURCES if r.endswith(y)] + [r for r in SOURCES if not r.endswith(y)]
    tag_day = day.isoformat().replace('-', '.')
    for repo in repos:
        urls = urls_of(api(f'repos/{repo}/releases/tags/v{tag_day}-planes-readsb-{inst}'))
        if urls:
            return urls
    return None


def find_day(day, instances=PREFERENCE):
    """The day's best available release URLs, or None (listed holes too)."""
    if day.isoformat() in HOLES:
        return None
    if day.isoformat() in OVERRIDES:
        instances = [OVERRIDES[day.isoformat()]]
    for inst in instances:
        urls = instance_urls(day, inst)
        if urls:
            return urls
    return None


def release_assets(rel):
    """A release's assets, paginated: the releases LIST embeds at most ~100
    assets per release and a month already holds 93 (3 x 31)."""
    assets = rel.get('assets', [])
    if len(assets) < 100:
        return assets
    return paged(f"repos/{OWN}/releases/{rel['id']}/assets")


def published_here():
    """Days whose flights+paths assets are fully uploaded in our releases."""
    done = set()
    for rel in paged(f'repos/{OWN}/releases'):
        ok = {a['name'] for a in release_assets(rel)
              if a.get('state') == 'uploaded' and a.get('size', 0) > 0}
        for n in ok:
            m = re.match(r'^flights-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$', n)
            if m and f'paths-{m.group(1)}.jsonl.gz' in ok:
                done.add(m.group(1))
    return done


def live_manifest():
    """The per-day entries of the LIVE MANIFEST.json on main - never this
    checkout's copy. A dispatched run checks out the commit of its dispatch
    moment, so a batch queued behind another read a manifest missing
    everything the first published, took those days as version 0, and
    redid all 250 (2026-11-08, two pairs). An unreadable manifest stops the
    plan rather than silently redoing published days."""
    man = api(f'repos/{OWN}/contents/MANIFEST.json?ref=main', accept='application/vnd.github.raw')
    return (man or {}).get('days', {})


def manifest_versions():
    return {d: e.get('v', 0) for d, e in live_manifest().items()}


def source_instance(entry):
    return (entry.get('source') or '').split('-planes-readsb-')[-1]


def out(line):
    print(line)
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a') as f:
            f.write(line + '\n')


def main():
    mode = sys.argv[1]
    today = dt.datetime.now(dt.timezone.utc).date()
    if mode == 'one':
        explicit = len(sys.argv) > 2 and sys.argv[2].strip()
        if explicit:
            day = parse_day(sys.argv[2].strip())
            urls = find_day(day)
            if not urls:
                sys.exit(f'no adsb.lol archive found for {day}')
            out('matrix=' + json.dumps({'include': [{'day': day.isoformat(), 'urls': urls}]}, separators=(',', ':')))
            out('count=1')
            out('red=')
            return
        d = today - dt.timedelta(days=1)
        done = published_here()
        todo = []
        red = ''
        # Self-heal window: any unpublished day in the last 14 that the
        # archive has (a 3+-day outage must not leave permanent holes; the
        # done-set is already fetched, so this costs find_day calls only for
        # actual holes). Beyond 14 days: a manual backfill dispatch.
        for back in range(14, 1, -1):
            day_b = d - dt.timedelta(days=back)
            if day_b.isoformat() not in done:
                urls = find_day(day_b)
                if urls:
                    todo.append({'day': day_b.isoformat(), 'urls': urls})
        # D-1: any instance; missing everywhere AND unpublished = the one alarm.
        day1 = d - dt.timedelta(days=1)  # noqa: E501 (the day before yesterday)
        if day1.isoformat() not in done:
            urls = find_day(day1)
            if urls:
                todo.append({'day': day1.isoformat(), 'urls': urls})
            else:
                # The alarm is an OUTPUT, not an exit: the days job must
                # still process whatever self-heal work was found, and a
                # separate alarm job turns this red.
                red = (f'{day1} is neither published here nor available from any adsb.lol '
                       f'instance; with {d} also pending, the pipeline or the source is dead')
        # D (yesterday): the main network only; late publishing is never red.
        if d.isoformat() not in done:
            urls = find_day(d, instances=['prod-0'])
            if urls:
                todo.append({'day': d.isoformat(), 'urls': urls})
            else:
                print(f'::warning::adsb.lol has not published prod-0 for {d} yet; tomorrow self-heals it')
        out('matrix=' + json.dumps({'include': todo}, separators=(',', ':')))
        out(f'count={len(todo)}')
        out(f'red={red}')
        return
    if mode == 'range':
        start, end = parse_day(sys.argv[2]), parse_day(sys.argv[3])
        # FORCE: published days to reprocess anyway (a defect fixed in the
        # extraction path, e.g. the 2026-11-09 duplicate-member days).
        force = set(sys.argv[4].split()) if len(sys.argv) > 4 else set()
        for f in force:
            parse_day(f)
        # The daily job's self-heal reaches back 15 days (today-15 ..
        # today-1); ending at today-16 keeps the two day sets disjoint,
        # so daily and backfill can never claim the same day.
        end = min(end, today - dt.timedelta(days=16))
        done, man = published_here(), live_manifest()
        vers = {d: e.get('v', 0) for d, e in man.items()}
        best = {}
        for repo in SOURCES:
            for rel in paged(f'repos/{repo}/releases'):
                m = TAG.match(rel.get('tag_name', ''))
                if not m or m.group(4) not in PREFERENCE:
                    continue
                day = f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
                if not (start.isoformat() <= day <= end.isoformat()) or day in HOLES:
                    continue
                if day in OVERRIDES and m.group(4) != OVERRIDES[day]:
                    continue
                urls = urls_of(rel)
                rank = PREFERENCE.index(m.group(4))
                if urls and (day not in best or rank < best[day][0]):
                    best[day] = (rank, urls)
        # Overrides are declarative: a published day whose source differs
        # from its source_overrides.txt instance is republished from it
        # (verify's I12 holds it red until then).
        todo = [{'day': d2, 'urls': u} for d2, (_, u) in sorted(best.items(), reverse=True)
                if d2 not in done or vers.get(d2, 0) < EXTRACTOR_VERSION or d2 in force
                or (d2 in OVERRIDES and source_instance(man.get(d2, {})) != OVERRIDES[d2])]
        todo = todo[:MAX_MATRIX]
        out(f'count={len(todo)}')
        out('matrix=' + json.dumps({'include': todo}, separators=(',', ':')))
        sys.stderr.write(f'{len(todo)} days to process in {start}..{end}\n')
        return
    if mode == 'pairs':
        # measure.yml: "DAY:INSTANCE DAY:INSTANCE ..." -> one job per pair,
        # holes and overrides ignored (it exists to compare instances).
        todo = []
        for pair in sys.argv[2].split():
            day_s, _, inst = pair.partition(':')
            if inst not in PREFERENCE:
                sys.exit(f'unknown instance in {pair!r}')
            urls = instance_urls(parse_day(day_s), inst)
            if urls:
                todo.append({'day': day_s, 'inst': inst, 'urls': urls})
            else:
                print(f'::warning::no {inst} archive for {day_s}')
        out(f'count={len(todo)}')
        out('matrix=' + json.dumps({'include': todo[:MAX_MATRIX]}, separators=(',', ':')))
        return
    sys.exit('usage: plan.py one [YYYY-MM-DD] | range START END | pairs "DAY:INST ..."')


if __name__ == '__main__':
    main()
