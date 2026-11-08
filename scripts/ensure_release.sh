#!/usr/bin/env bash
# Create-or-reuse the month's release data-YYYY-MM. Usage: ensure_release.sh YYYY-MM
# Needs GH_TOKEN with contents: write. Backfill calls this serially from its
# plan job BEFORE the parallel day jobs start: eight day jobs racing to
# create a new month produced two releases under one tag (data-2025-12,
# 2026-11-08). The day jobs still call it as a fallback.
# Retries with backoff: a 15-second api.github.com connection blip
# (2026-11-08 22:37 UTC) otherwise throws away a fully extracted day. Each
# attempt looks before it creates, so a create that succeeded server-side
# but lost its reply is found, not repeated.
set -euo pipefail
month="${1:-}"
[[ "$month" =~ ^[0-9]{4}-[0-9]{2}$ ]] || { echo "bad month: $month"; exit 1; }
for attempt in 1 2 3 4 5; do
  if gh release view "data-$month" >/dev/null 2>&1; then exit 0; fi
  gh release create "data-$month" --title "Flights $month" \
    --notes "Airline flight legs seen by adsb.lol receivers in $month, one file set per day. Derived from adsb.lol globe_history, (c) adsb.lol contributors, Open Database License (ODbL) 1.0: https://opendatacommons.org/licenses/odbl/1-0/ . Data in this repository is likewise ODbL 1.0; see DATA-LICENSE.md and README.md." \
    >/dev/null || true
  if gh release view "data-$month" >/dev/null 2>&1; then exit 0; fi
  [ "$attempt" = 5 ] || sleep $((attempt * 30))
done
echo "could not view or create release data-$month after 5 attempts"
exit 1
