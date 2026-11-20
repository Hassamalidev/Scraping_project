# osm_overpass — OpenStreetMap via the Overpass API

| | |
|---|---|
| Tier | 3 (open data) |
| Trades | carpentry, handyman, flooring/carpet, plumbing, electrical, HVAC, painting, roofing |
| Data | OpenStreetMap features tagged `craft=*` / `shop=*` |
| API | `https://overpass-api.de/api/interpreter` (public instance, GET `?data=<query>`) |
| Licence | Open Database License (ODbL). Attribution required: **© OpenStreetMap contributors** |
| Usage policy | https://dev.overpass-api.de/overpass-doc/en/preface/commons.html — keep volume modest (well under 10,000 queries/day), no parallel hammering |
| Update frequency | OSM edits appear within minutes; one query per run is enough |
| Connector | `prodata/connectors/osm_overpass.py` |

## Why this source

Texas doesn't license carpenters, handymen or flooring installers, so state licence files can't
list them. OSM is open data meant for reuse and covers these trades, with business phone,
email, website, address and opening hours when contributors have added them.

## Query

One query per metro: every node/way/relation with one of the mapped tags inside the metro radius
(`around:<radius m>,<lat>,<lon>`), returned with tags and centre coordinates. The tags queried are
in `TAGS` in the connector, and their trade mapping is in `config/trade_map.yaml` under `osm_overpass`.

## Rules applied

- Business features only (`craft=*`, `shop=*`). Only business contact tags are read.
- Features with `disused:*`, `was:*` or `abandoned:*` tags are treated as closed and skipped.
- OSM phones are crowd-sourced. They are **not** treated as confirmed business lines, so they're
  only callable when the DNC data says the number is clear.
- A feature whose phone matches an existing business is linked to it (dedupe rule 2) and only
  fills blanks. It never overwrites licence data.
- Attribution is shown in the app wherever OSM data appears. Keep it in any export or product
  that shows this data.

## Verification status

- [ ] confirm `overpass-api.de` robots.txt allows `/api/interpreter` (the client checks it automatically and stops if not)
- [ ] `uv run prodata sources probe osm_overpass` returns elements for a 5-mile radius
