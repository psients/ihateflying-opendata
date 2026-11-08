#!/usr/bin/env python3
"""Find adsb.lol archive files for days, and which days this repo still needs.

  plan.py one [YYYY-MM-DD]    -> GitHub output lines day=, urls= (default: yesterday, UTC)
  plan.py range START END     -> GitHub output lines count=, matrix= (days not yet published here)

The adsb.lol archive keeps one release per receiver instance per day. The main
network (prod-0) is preferred; the others fill days it lacks.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request

SOURCES = ['adsblol/globe_history_2023', 'adsblol/globe_history_2024',
           'adsblol/globe_history_2025', 'adsblol/globe_history_2026']
PREFERENCE = ['prod-0', 'prod-0tmp', 'staging-0', 'prod-1', 'staging-0tmp', 'test-0', 'test-1']
TAG = re.compile(r'^v(\d{4})\.(\d{2})\.(\d{2})-planes-readsb-(.+)$')
OWN = os.environ.get('GITHUB_REPOSITORY', 'psients/ihateflying-opendata')
MAX_MATRIX = 250  # GitHub allows 256 jobs per matrix


def api(path):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'ihateflying-opendata'}
    if os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    with urllib.request.urlopen(urllib.request.Request('https://api.github.com/' + path, headers=headers), timeout=60) as r:
        return json.load(r)


def paged(path):
    page, out = 1, []
    while True:
        chunk = api(f'{path}?per_page=100&page={page}')
        out.extend(chunk)
        if len(chunk) < 100:
            return out
        page += 1


def parse_day(s):
    return dt.date.fromisoformat(s).isoformat()  # rejects anything that is not YYYY-MM-DD


def urls_of(release):
    assets = sorted(release.get('assets', []), key=lambda a: a['name'])
    urls = [a['browser_download_url'] for a in assets if '.tar' in a['name']]
    if not urls or not all(u.startswith('https://github.com/adsblol/') for u in urls):
        return None
    return ' '.join(urls)


def index_all():
    best = {}
    for repo in SOURCES:
        for rel in paged(f'repos/{repo}/releases'):
            m = TAG.match(rel.get('tag_name', ''))
            if not m or m.group(4) not in PREFERENCE:
                continue
            day = f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
            urls = urls_of(rel)
            rank = PREFERENCE.index(m.group(4))
            if urls and (day not in best or rank < best[day][0]):
                best[day] = (rank, urls)
    return {d: u for d, (_, u) in best.items()}


def published_here():
    done = set()
    for rel in paged(f'repos/{OWN}/releases'):
        names = {a['name'] for a in rel.get('assets', [])}
        for n in names:
            m = re.match(r'^flights-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$', n)
            if m and f'paths-{m.group(1)}.jsonl.gz' in names:
                done.add(m.group(1))
    return done


def main():
    mode = sys.argv[1]
    if mode == 'one':
        day = parse_day(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
        y = day[:4]
        repos = [f'adsblol/globe_history_{y}'] + [r for r in SOURCES if not r.endswith(y)]
        tag_day = day.replace('-', '.')
        for inst in PREFERENCE:
            for repo in repos:
                try:
                    urls = urls_of(api(f'repos/{repo}/releases/tags/v{tag_day}-planes-readsb-{inst}'))
                except Exception:
                    continue
                if urls:
                    print(f'day={day}')
                    print(f'urls={urls}')
                    return
        sys.exit(f'no adsb.lol archive found for {day}')
    if mode == 'range':
        start, end = parse_day(sys.argv[2]), parse_day(sys.argv[3])
        have, done = index_all(), published_here()
        todo = [{'day': d, 'urls': have[d]} for d in sorted(have) if start <= d <= end and d not in done]
        todo = todo[:MAX_MATRIX]
        print(f'count={len(todo)}')
        print('matrix=' + json.dumps({'include': todo}, separators=(',', ':')))
        sys.stderr.write(f'{len(todo)} days to process between {start} and {end}\n')
        return
    sys.exit('usage: plan.py one [YYYY-MM-DD] | range START END')


if __name__ == '__main__':
    main()
