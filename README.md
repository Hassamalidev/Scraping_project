# Tradebook — Pro Data Engine

Tradebook builds a verified database of local trade businesses (plumbers, HVAC and
electrical contractors, and more) from **official licence data**, and tells you, for
every phone number and email, whether you're allowed to contact it.

It is Phase 1 of a home-services lead marketplace (see [project_spec.md](project_spec.md)).

- **Business data only.** Licensed businesses and their published business contacts. Never homeowners.
- **Official sources first.** State licence bulk files and open-data APIs, not scraping.
- **Polite.** robots.txt honoured, ≤ 1 request/second per domain, retries with backoff, everything cached.
- **Provenance on everything.** Every value links to the raw source record it came from.
- **Compliance built in.** DNC status and a permanent suppression list on every number; nothing is
  marked callable without fresh (≤ 31-day) Do-Not-Call data. No automated calls or texts, ever.

## What's in it

| Part | What it does |
|---|---|
| Web app (`prodata web`) | Browse and filter pros, see why each number is or isn't callable, log outreach, manage suppression, import DNC files, run sources, download CSVs |
| CLI (`prodata …`) | The same pipeline from the terminal, for scheduling |
| Connectors | **TX TSBPE** (plumbing — Responsible Master Plumbers) and **TX TDLR** (HVAC and electrical contractors, via data.texas.gov) |
| Database | PostgreSQL 16 + PostGIS (Docker), SQLAlchemy 2, Alembic migrations |

## Quick start

Needs Docker Desktop, Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env              # then set CONTACT_EMAIL (it goes in the crawler User-Agent)
docker compose up -d              # Postgres + PostGIS on localhost:5440
uv sync
uv run prodata db upgrade         # create tables
uv run prodata sources list

uv run prodata collect --all      # download licence data for the market in config/market.yaml
uv run prodata web                # open http://127.0.0.1:8000
```

Before calling anyone, import the National DNC Registry area-code files your company downloads
(`uv run prodata dnc import <file>` or the Compliance page). Until then every number shows
**Blocked — no DNC data for this area code**, by design.

## Using the web app

- **Overview**: counts, coverage by trade, source health, recent runs, compliance warnings.
- **Pros**: search by name, licence number or phone; filter by trade, city, outreach status, "cleared to call".
  *Download CSV* exports exactly what you're looking at (suppressed values are always removed).
  *Email-tool CSV* gives first-touch rows for your email tool (verified emails only).
- **A pro's page**: phones with DNC status and the reason they can or can't be called, emails, licences,
  an outreach log per channel (status, notes, "contacted just now"), and *Where this came from*:
  the original source records.
- **Compliance**: add to the suppression list (phone, email, domain, business); upload DNC files and see their age.
- **Sources**: run a source, see run history, unmapped licence classes and errors.

## CLI

```
prodata sources list
prodata sources probe <source_id>     # fetch a sample, show columns and field mapping
prodata collect --source <id> | --all [--url <override>]
prodata verify                        # refresh call_ok / email_ok flags
prodata dnc import <path>
prodata suppress add <value> --type phone|email|domain|business_id --reason opt_out
prodata suppress list
prodata export --market dfw --trade plumbing [--min-score 60] [--format csv|email-tool]
prodata stats --market dfw
prodata run --market dfw              # everything built so far
prodata web
```

`normalize`, `geocode`, `dedupe`, `enrich` and `score` exist and say which milestone delivers them.

## Configuration

- `config/market.yaml` — the launch market (DFW example), counties/ZIP prefixes used as the
  market filter until geocoding lands, crawl politeness, DNC max age.
- `config/trade_map.yaml` — source licence classes → internal trades. Unmapped classes show up in run results.
- `.env` — database URL, contact email, optional Socrata app token. Never committed.

## Tests

```bash
uv run pytest
```

Unit tests (parsing, normalisation, robots.txt, rate limit, retries, cache, call_ok/email_ok rules)
need nothing. Database tests run against the `prodata_test` database created by Docker Compose and
are skipped when it isn't reachable. Tests never hit live sites.

## Layout

```
prodata/
  cli.py  config.py  db.py  http_client.py  logs.py
  connectors/   base.py  tx_tsbpe_plumbing.py  tx_tdlr_licenses.py
  pipeline/     collect.py  load.py  normalize.py  compliance.py  export.py
  models/       SQLAlchemy models (spec section 5)
  web/          FastAPI app, Jinja templates, CSS
config/         market.yaml  trade_map.yaml
docs/sources/   one page per data source (written before its connector)
migrations/     Alembic
tests/
```

## Status

| Milestone | State |
|---|---|
| M1 Skeleton (Docker, models, migrations, config, HTTP client, CLI) | Done |
| M2 First Tier-1 connector (TX plumbing) | Built; column mapping needs a live check (see below) |
| M6/M7 parts: suppression, DNC import, call_ok/email_ok, CSV export, stats | Built early so the web app is usable |
| M3 geocoding, M4 dedupe rules 2–5, M5 website enrichment, M6 verification, M7 scoring | Next |

**Live verification still needed:** the TSBPE and TDLR column names are mapped through alias lists
(see `docs/sources/`). Run `prodata sources probe tx_tsbpe_plumbing` and
`prodata sources probe tx_tdlr_licenses` once, and add any unmatched column names to the
`FIELDS` map in the connector.
