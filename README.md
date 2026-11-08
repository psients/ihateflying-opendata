# ihateflying-opendata

Code: MIT. Published data: ODbL 1.0 (see [DATA-LICENSE.md](DATA-LICENSE.md); GitHub's
sidebar licence badge covers the code only).

Daily summaries of airline flights seen by the open ADS-B receiver network at
[adsb.lol](https://adsb.lol), built for [ihateflying.org](https://www.ihateflying.org)
and published openly, as the source licence requires.

Each day of the adsb.lol archive (about 3 GB of raw aircraft tracks) is processed
on a GitHub runner and reduced to two small files. Nothing raw is kept.

## Files

Releases named `data-YYYY-MM` hold three files per day:

- `flights-YYYY-MM-DD.jsonl.gz`: one JSON line per airline flight leg the receivers saw
- `paths-YYYY-MM-DD.jsonl.gz`: each leg's observed track (same `id`), as time-stamped
  segments `{"segs": [[[lat, lon, seconds_since_t0], ...], ...]}`. A segment never spans
  a coverage gap of more than 5 minutes, so unseen sky is never drawn as a line.
- `meta-YYYY-MM-DD.json`: extractor version, airports revision, counts, file hashes.
  `MANIFEST.json` in the repository collects these per day.

Every line carries `v`, the extractor version that produced it. When the extractor
changes, affected days are reprocessed and the manifest says which version each day has.

Coverage starts on 2023-02-16, the first day of the adsb.lol archive.

## Flight fields

| field | meaning |
|---|---|
| `id` | `YYYY-MM-DD-<hex>-<t0>`: day, transponder address, first airborne second |
| `v` | extractor version that produced this line |
| `hex` | ICAO 24-bit transponder address |
| `reg`, `type` | registration and ICAO type code from adsb.lol's aircraft database (not independently verified) |
| `cs` | callsign, e.g. `KAL081` |
| `org`, `dst` | departure and arrival airport (IATA), inferred from where the aircraft was seen low; `null` when not seen |
| `oq`, `dq` | assignment quality for `org`/`dst`: `[feet above the airport, km from it]` at the moment of assignment; generous assignments (high or far) are real, flagged, and filterable |
| `tk`, `ld` | whether the departure / the landing itself was observed: the leg starts or ends on the ground, or below 3,000 ft within 10 km of the assigned airport. A fragment re-acquired at altitude is never a departure |
| `t0`, `t1` | first and last airborne sighting (Unix seconds, UTC) |
| `km` | distance flown along the observed track |
| `alt` | highest barometric altitude seen (ft) |
| `gap` | longest stretch without a position (minutes) |
| `cov` | share of the airborne time with positions no more than 5 minutes apart |

## Read this before using the numbers

- **Counts are floors.** Receivers miss flights, above all over oceans and in regions with few feeders, and coverage grew over the archive's life.
- **Times are sightings, not a timetable.** `t0`/`t1` are when receivers saw the aircraft airborne.
- **Airports are inferred.** A takeoff or landing is matched to the nearest airport whose approach path fits the aircraft's height. When the aircraft was not seen low, the field is `null`.
- **Only airline-format callsigns** (three letters and digits) are kept. Military and non-ICAO addresses are excluded.
- **Identity labels** (`reg`, `type`) come from the community aircraft database used by adsb.lol and similar sites; treat them as unverified.

## Counting flights honestly

`scripts/aggregate.py` is the method ihateflying.org uses to turn these files into its
displayed aggregates (published per ODbL 4.6(b)). Its header states the counting rule;
the short form: a flight is a leg whose departure was actually observed (`tk`), with 15
minutes of ground time since the airframe's previous leg, no same-airport circuits, at
most 14 a day per airframe, and the broadcast registration must not contradict the
expected one. Flight numbers come only from suffix-less callsigns of carriers whose
callsign number is the booked number. Route exemplars are single real tracks: this script
lists the recent well-observed candidates, and the consumer picks the medoid
(the one track closest to the others) where the path files are at hand. Never
averages. Every count is a floor.

## Integrity notes

- Crowd-sourced ADS-B can be spoofed. Legs with three consecutive inter-fix ground
  speeds above 1,500 km/h are dropped at extraction; identity and daily-cap checks run
  at aggregation; disagreements are flagged, not displayed.
- `reg` and `type` come from the community aircraft database and are not independently
  verified here.
- Do not enable GitHub's immutable releases on this repository: days are re-uploaded in
  place when the extractor version changes.

## Privacy

This repository summarises data the adsb.lol archive already publishes in raw form.
[PENDING OWNER DECISION: either the airline-prefix allowlist (`scripts/airlines.txt`)
ships enabled, or this section gains a removal-request contact address.]

## Licence

- **Data:** derived from [adsb.lol globe_history](https://github.com/adsblol/globe_history), © adsb.lol contributors, under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). This database is published under the same licence; see [DATA-LICENSE.md](DATA-LICENSE.md).
- **Airport positions:** [OurAirports](https://ourairports.com/data/) (public domain).
- **Code:** MIT; see [LICENSE](LICENSE).
