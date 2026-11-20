# Tradebook

Tradebook connects people who need work done with the trades who do it, in one place:

- **Find workers**: plumbers, carpenters, handymen, flooring, painters, roofers, electricians and
  HVAC techs. Some are collected from official licence data and OpenStreetMap; others sign up on
  your public **Join** page (`/join`).
- **Find clients**: people who want a pro. They fill in your public **Request a pro** page
  (`/request`), or you add someone who called you.
- **Call both sides**: every person has a big phone number and a *Call* button. Log how each
  call went.
- **Deals**: write down what the client pays, what the worker gets, how the client paid
  (cash, Zelle, bank transfer…) and whether it's paid. Your cut is worked out for you. Money is
  never handled by the app.

> **Clients are never scraped.** Clients come only from people who ask for a pro themselves
> (the request form records their consent: wording, time, IP, page) or who contacted you directly.
> Scraping private people's numbers to cold-call them is illegal in the US (TCPA, up to $1,500
> per call) and is excluded by the spec.

How workers are collected:

- **Business data only.** Businesses and their published business contacts.
- **Official sources first.** State licence bulk files and open-data APIs, not scraping.
- **Polite.** robots.txt honoured, ≤ 1 request/second per domain, retries with backoff, everything cached.
- **Provenance on everything.** Every value links to the raw source record it came from.
- **Call rules built in.** People who asked to be called (sign-ups, client requests) can be called
  straight away. Workers found in public data can be called once fresh (≤ 31-day) Do-Not-Call data
  is imported for their area code. Anyone who asks to stop goes on a permanent do-not-contact list.
  No automated calls or texts, ever.

## What's in it

| Part | What it does |
|---|---|
| Web app (`prodata web`) | Home with the two sides, Find workers, Find clients, Deals, public `/request` and `/join` forms, do-not-call list, data sources |
| CLI (`prodata …`) | The same pipeline from the terminal, for scheduling |
| Connectors | **TX TSBPE** (plumbing — Responsible Master Plumbers), **TX TDLR** (HVAC and electrical contractors, via data.texas.gov) and **OpenStreetMap** (carpenters, handymen, flooring, painters, roofers and more — trades Texas doesn't license) |
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
(`uv run prodata dnc import <file>` or the Do-not-call page) before cold-calling workers found in
public data. Until then their numbers show **Don't call yet: no DNC data for this area code**.
People who signed up or asked for a call are callable straight away.

## Using the web app

- **Home**: the two sides. Worker and client counts per trade, the links to share, money this
  month, and the clients waiting for a call.
- **Find workers**: pick a trade (Plumber, Carpenter, Handyman…), search by name or phone, filter
  *Want work* / *Not called yet* / *OK to call*. People who want work are listed first. A worker's
  page has the call log ("Interested", "Not interested", "Do not contact", notes), licence details,
  the jobs you gave them, and *Where this came from*.
- **Find clients**: requests by trade and status (New → Called → Worker found → Job done). A client's
  page has the job, their number, **matching workers** (interested first, same city next) with a
  *Choose* button, the call log, **the deal** and the consent record.
- **Deals**: every priced job with client price, worker pay, your cut, payment method and status.
  Also shows totals for the month, money collected and money still owed.
- **Public pages**: `/request` (for clients) and `/join` (for workers). Share the links in ads,
  flyers and posts. Both need a consent tick-box and have a hidden spam trap.
- **Do-not-call list**: people who asked not to be contacted, plus National DNC Registry imports.
- **Data sources**: collection runs and their results.

**Putting it online:** set `ADMIN_PASSWORD` in `.env` first. Every page except `/request` and
`/join` then asks for it (user `admin`). `BRAND_NAME` sets the name shown on the public pages.

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
  people.py     workers, client requests, matching, deals
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
