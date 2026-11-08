#!/usr/bin/env python3
"""Maintain MANIFEST.json: per published day, the extractor version, airports
revision, counts and file hashes (from each day's meta-YYYY-MM-DD.json asset).

  manifest.py update FILE...   merge local meta files written by extract_day.py
  manifest.py reindex          rebuild by reading meta assets from this repo's
                               releases (used once after each backfill batch)

The manifest is the daily commit that keeps the repository active (GitHub
disables schedules in public repositories after 60 days without activity) and
the version index that tells a backfill which days predate the current
extractor. Committing is left to the workflow step.
"""
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, '..', 'MANIFEST.json')


def load():
    try:
        return json.load(open(PATH))
    except Exception:
        return {'days': {}}


def save(man):
    man['days'] = dict(sorted(man['days'].items()))
    with open(PATH, 'w') as f:
        json.dump(man, f, indent=1, sort_keys=True)
        f.write('\n')


def merge(man, meta):
    day = meta.get('day')
    if not day or not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
        return False
    man['days'][day] = {k: meta[k] for k in
                        ('v', 'airports_rev', 'allowlist', 'scanned', 'legs', 'errors',
                         'bad_meta', 'oversize', 'absurd_legs', 'sha256') if k in meta}
    return True


def fetch_json(url):
    import time
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'ihateflying-opendata',
                                                       'Accept': 'application/octet-stream'})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(10 * (attempt + 1))


def reindex(man):
    sys.path.insert(0, HERE)
    from plan import paged, release_assets, OWN  # noqa: E402
    changed = 0
    for rel in paged(f'repos/{OWN}/releases'):
        for a in release_assets(rel):
            m = re.match(r'^meta-(\d{4}-\d{2}-\d{2})\.json$', a['name'])
            if not m or a.get('state') != 'uploaded':
                continue
            day = m.group(1)
            have = man['days'].get(day, {})
            if have.get('v') and have.get('sha256'):
                continue
            if merge(man, fetch_json(a['browser_download_url'])):
                changed += 1
                if changed % 50 == 0:
                    save(man)  # a late hiccup must not repeat 1,300 fetches
    return changed


def main():
    mode = sys.argv[1]
    man = load()
    if mode == 'update':
        n = sum(merge(man, json.load(open(p))) for p in sys.argv[2:])
        save(man)
        print(f'merged {n} day(s); manifest now covers {len(man["days"])}')
    elif mode == 'reindex':
        n = reindex(man)
        save(man)
        print(f'reindexed {n} day(s); manifest now covers {len(man["days"])}')
    else:
        sys.exit('usage: manifest.py update FILE... | reindex')


if __name__ == '__main__':
    main()
