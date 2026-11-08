#!/usr/bin/env bash
# Race-safe commit of pipeline state to main.
#
# Every file a workflow commits is either PRODUCED by that job at a path no
# other job writes (a per-day record, a drill, a dated dry-run file) or
# GENERATED from the records and releases (MANIFEST.json, QUARANTINE.md).
# So a rejected push is never rebased - rebasing two regenerated files is
# exactly the conflict that turns a harmless race into a red email. Instead
# the commit is rebuilt on top of the newest main: produced files restored,
# generated files regenerated. No conflict is possible and no row is lost.
#
# usage: commit_state.sh "<message>" <stash-dir> [manifest] [render]
#   stash-dir  produced files, laid out at their repository paths (may be
#              empty or missing)
#   manifest   regenerate MANIFEST.json (scripts/manifest.py reindex)
#   render     regenerate QUARANTINE.md (scripts/verify.py render)
set -euo pipefail
msg="$1"
stash="$2"
shift 2
git config user.name opendata-bot
git config user.email actions@github.com
for attempt in 1 2 3 4 5; do
  git fetch -q origin main
  git reset -q --hard origin/main
  if [ -d "$stash" ]; then
    cp -R "$stash/." .
  fi
  for step in "$@"; do
    case "$step" in
      manifest) python3 scripts/manifest.py reindex ;;
      render) python3 scripts/verify.py render ;;
      *) echo "unknown regenerate step: $step"; exit 2 ;;
    esac
  done
  for p in MANIFEST.json QUARANTINE.md quarantine dryrun; do
    if [ -e "$p" ]; then
      git add -A -- "$p"
    fi
  done
  if git diff --cached --quiet; then
    echo "nothing to commit"
    exit 0
  fi
  git commit -q -m "$msg"
  if git push -q origin HEAD:main; then
    echo "committed: $msg"
    exit 0
  fi
  echo "push rejected (attempt $attempt); rebuilding on the newest main"
  sleep $((attempt * 15))
done
echo "could not commit after 5 attempts"
exit 1
