#!/usr/bin/env bash
# Create-or-reuse the month's release data-YYYY-MM. Usage: ensure_release.sh YYYY-MM
# Needs GH_TOKEN with contents: write. Backfill calls this serially from its
# plan job BEFORE the parallel day jobs start: eight day jobs racing to
# create a new month produced two releases under one tag (data-2025-12,
# 2026-11-08). The day jobs still call it as a fallback.
set -euo pipefail
month="${1:-}"
[[ "$month" =~ ^[0-9]{4}-[0-9]{2}$ ]] || { echo "bad month: $month"; exit 1; }
gh release view "data-$month" >/dev/null 2>&1 \
  || gh release create "data-$month" --title "Flights $month" \
       --notes "Airline flight legs seen by adsb.lol receivers in $month, one file set per day. Derived from adsb.lol globe_history, (c) adsb.lol contributors, Open Database License (ODbL) 1.0: https://opendatacommons.org/licenses/odbl/1-0/ . Data in this repository is likewise ODbL 1.0; see DATA-LICENSE.md and README.md." \
  || true
gh release view "data-$month" >/dev/null
