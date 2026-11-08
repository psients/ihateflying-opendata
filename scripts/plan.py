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


def api(path):
    """GET api.github.com/<path>. 404 -> None; other errors retried 3x."""
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'ihateflying-opendata'}
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
        except urllib.error.URLError:
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


def urls_of(release):
    if not release:
        return None
    assets = sorted(release.get('assets', []), key=lambda a: a['name'])
    urls = [a['browser_download_url'] for a in assets
            if '.tar' in a['name'] and a.get('state') == 'uploaded' and a.get('size', 0) > 0]
    if not urls or not all(ASSET_URL.match(u) for u in urls):
        return None
    return ' '.join(urls)


def find_day(day, instances=PREFERENCE):
    """The day's best available release URLs, or None (listed holes too)."""
    if day.isoformat() in HOLES:
        return None
    y = str(day.year)
    repos = [r for r in SOURCES if r.endswith(y)] + [r for r in SOURCES if not r.endswith(y)]
    tag_day = day.isoformat().replace('-', '.')
    for inst in instances:
        for repo in repos:
            urls = urls_of(api(f'repos/{repo}/releases/tags/v{tag_day}-planes-readsb-{inst}'))
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


def manifest_versions():
    try:
        man = json.load(open(os.path.join(HERE, '..', 'MANIFEST.json')))
        return {d: e.get('v', 0) for d, e in man.get('days', {}).items()}
    except Exception:
        return {}


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
        # The daily job's self-heal reaches back 15 days (today-15 ..
        # today-1); ending at today-16 keeps the two day sets disjoint,
        # so daily and backfill can never claim the same day.
        end = min(end, today - dt.timedelta(days=16))
        done, vers = published_here(), manifest_versions()
        best = {}
        for repo in SOURCES:
            for rel in paged(f'repos/{repo}/releases'):
                m = TAG.match(rel.get('tag_name', ''))
                if not m or m.group(4) not in PREFERENCE:
                    continue
                day = f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
                if not (start.isoformat() <= day <= end.isoformat()) or day in HOLES:
                    continue
                urls = urls_of(rel)
                rank = PREFERENCE.index(m.group(4))
                if urls and (day not in best or rank < best[day][0]):
                    best[day] = (rank, urls)
        todo = [{'day': d2, 'urls': u} for d2, (_, u) in sorted(best.items(), reverse=True)
                if d2 not in done or vers.get(d2, 0) < EXTRACTOR_VERSION]
        todo = todo[:MAX_MATRIX]
        out(f'count={len(todo)}')
        out('matrix=' + json.dumps({'include': todo}, separators=(',', ':')))
        sys.stderr.write(f'{len(todo)} days to process in {start}..{end}\n')
        return
    sys.exit('usage: plan.py one [YYYY-MM-DD] | range START END')


if __name__ == '__main__':
    main()
