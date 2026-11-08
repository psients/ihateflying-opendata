# ihateflying-opendata

Daily summaries of airline flights seen by the open ADS-B receiver network at
[adsb.lol](https://adsb.lol), built for [ihateflying.org](https://www.ihateflying.org)
and published openly, as the source licence requires.

Each day of the adsb.lol archive (about 3 GB of raw aircraft tracks) is processed
on a GitHub runner and reduced to two small files. Nothing raw is kept.

## Files

Releases named `data-YYYY-MM` hold two files per day:

- `flights-YYYY-MM-DD.jsonl.gz`: one JSON line per airline flight leg the receivers saw
- `paths-YYYY-MM-DD.jsonl.gz`: each leg's simplified path (same `id`), as `[[lat, lon], ...]`

Coverage starts on 2023-02-16, the first day of the adsb.lol archive.

## Flight fields

| field | meaning |
|---|---|
| `id` | `YYYY-MM-DD-<hex>-<n>`: day, transponder address, leg number that day |
| `hex` | ICAO 24-bit transponder address |
| `reg`, `type` | registration and ICAO type code from adsb.lol's aircraft database (not independently verified) |
| `cs` | callsign, e.g. `KAL081` |
| `org`, `dst` | departure and arrival airport (IATA), inferred from where the aircraft was seen low; `null` when not seen |
| `tk`, `ld` | whether the takeoff / the landing was actually observed |
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

## Licence

- **Data:** derived from [adsb.lol globe_history](https://github.com/adsblol/globe_history), © adsb.lol contributors, under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). This database is published under the same licence; see [DATA-LICENSE.md](DATA-LICENSE.md).
- **Airport positions:** [OurAirports](https://ourairports.com/data/) (public domain).
- **Code:** MIT; see [LICENSE](LICENSE).
