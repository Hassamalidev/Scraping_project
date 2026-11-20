# Project Spec: Home Services Lead Marketplace — Phase 1: Pro Data Engine

> For Claude Code: read this entire file before writing any code. Build milestone by milestone (section 9). Stop at the end of each milestone, summarize what was built, and wait for approval before continuing.

---

## 1. What we are building

A US home-services marketplace that connects homeowners who need a skilled trade (plumbing, carpet/flooring, handyman, HVAC, electrical, etc.) with verified local professionals ("pros").

**Business model:** we operate as a lead/referral service. Pros pay per lead, or a fee when a job is booked. We do **not** sell, bid on, negotiate, or perform the work ourselves (that would make us a contractor under many state laws).

**Two sides of the marketplace:**

| Side | Who | How we get them |
|---|---|---|
| Supply | Pros (businesses) | Build a verified database from public business/license sources, then recruit them (this phase) |
| Demand | Homeowners | They come to us through our website, ads, and property-manager partners and submit requests with consent (Phase 2). **We never scrape homeowners.** |

**This repository starts with Phase 1: the Pro Data Engine.** It collects, cleans, verifies, and exports a database of local tradespeople with the best available *business* contact information, ready for compliant outreach.

---

## 2. Non-negotiable rules

1. **Business data only.** Collect data about trade businesses and their published business contact details. Never collect personal data about homeowners or consumers: no property-owner lists, people-search sites, skip tracing, or personal social profiles.
2. **Official sources first.** Prefer official bulk downloads and open-data APIs over HTML scraping. Scrape HTML only when no bulk/API option exists.
3. **Respect access rules.** Honor `robots.txt` and site terms. Do **not** scrape the HTML of Google Maps, Yelp, Angi, Thumbtack, HomeAdvisor, BBB, Facebook, or LinkedIn. Never bypass logins, CAPTCHAs, paywalls, or rate limits, and never rotate proxies to evade blocks. If a source blocks us or its terms forbid automated access, stop and report it.
4. **Polite crawling.** Use a descriptive User-Agent with a contact email, at most 1 request/second per domain by default, exponential backoff on errors, and a response cache so we never refetch unnecessarily.
5. **Provenance on everything.** Every stored value links to the raw source record it came from (source, URL, fetched_at). No field without provenance.
6. **No automated calling or texting.** The system never places calls or sends SMS in Phase 1. It produces outreach-ready lists with compliance flags. Email outreach happens in a separate tool that handles unsubscribes.
7. **Do-not-call and suppression.** Every phone number carries a DNC status. Anything on the National DNC Registry (unless confirmed as a business line) or on our internal suppression list is excluded from call exports. The suppression list overrides everything, permanently.
8. **Secrets** live in `.env` (provide `.env.example`) and are never committed.

---

## 3. Configuration

`config/market.yaml` (values below are an **example**; the launch market will be filled in):

```yaml
market:
  id: dfw
  state: TX
  metros:
    - name: Dallas-Fort Worth
      center: [32.7767, -96.7970]   # lat, lon
      radius_miles: 40
trades: [plumbing, flooring_carpet, handyman, hvac, electrical]
crawl:
  user_agent: "ProDataBot/0.1 (+mailto:ops@example.com)"
  max_rps_per_domain: 1
  max_pages_per_site: 8
  respect_robots_txt: true
scoring:
  min_export_score: 50
```

`config/trade_map.yaml` maps each source's license classes and categories to our internal trade taxonomy (e.g. a state's "Master Plumber" and "Journeyman Plumber" → `plumbing`; CA CSLB class C-15 → `flooring_carpet`).

---

## 4. Data sources (connectors)

Every source is a **connector** implementing the interface in section 7. Before coding a connector, research the official source and write `docs/sources/<source_id>.md` with the URL, format, update frequency, terms of use, available fields, and any usage limits. If the terms prohibit automated or bulk use, stop and report instead of building it.

### Tier 1: Official license data (primary, highest trust)

- Identify the licensing agency for **each trade** in the launch state. Many states split trades across agencies. For example, in Texas plumbers are licensed by TSBPE, while electrical and HVAC are licensed by TDLR.
- Some trades (carpet/flooring, handyman) usually aren't state-licensed. California is an exception: CSLB classification C-15 covers flooring. For those trades, rely on Tiers 2 and 3.
- Also check city/county contractor registrations for the launch metros, since some trades are licensed locally.
- Preferred formats: bulk CSV/XLS/TXT files and open-data (Socrata) APIs. Examples to investigate: data.texas.gov, data.ny.gov, data.wa.gov, CA CSLB public license data, FL DBPR public record downloads, MN DLI license data export.
- Capture what's published: license number, type/class, status, issue and expiry dates, business name, DBA, qualifier/owner name *as published*, business address, business phone, county, bond/insurance/workers' comp info, and disciplinary actions.

### Tier 2: The business's own website (enrichment)

- **Website discovery:** use a website from Tier 1 data when present. Otherwise use a web search API, if configured (e.g. Brave Search API), with the query `"<business name>" <city> <state>`. Accept a result only if the domain or page content matches the business name and city/phone.
- **Crawl** up to `max_pages_per_site` pages (home, contact, about, services, service-area pages), honoring `robots.txt`.
- **Extract:**
  - Business email addresses (prefer role addresses like info@, office@, service@), phone numbers, contact-form URL
  - Services offered, mapped to trades; service-area cities/ZIPs; hours; 24/7 or emergency service
  - Years in business, license numbers shown on the site, insurance/bonding claims, languages spoken
  - Business social page URLs (business pages only)
- Parse schema.org `LocalBusiness` JSON-LD first (via `extruct`), then fall back to HTML parsing.

### Tier 3: Open data

- **OpenStreetMap via the Overpass API.** Tags: `craft=plumber`, `craft=electrician`, `craft=hvac`, `craft=floorer`, `shop=carpet`, `shop=flooring`, `craft=handyman`. Follow the Overpass usage policy and keep ODbL attribution.
- **State Secretary of State business-entity data**, where it's available as bulk/open data: entity name, status, formation date, registered address.

### Tier 4: Optional paid APIs (off by default; enable via `.env` and follow each provider's current terms exactly)

- **Phone line type** (e.g. Twilio Lookup Line Type Intelligence): mobile, landline or VoIP, used for compliance flags.
- **Email verification** (e.g. ZeroBounce or NeverBounce): deliverability status.
- **Google Places API.** Read the current terms before use. They restrict storing Places content, so don't build the database from cached Places data. If it's used at all, use it only for live lookups and store `place_id`.

---

## 5. Data model (PostgreSQL 16 + PostGIS)

Use SQLAlchemy 2 models and Alembic migrations.

- **`sources`**: id, source_id (slug), name, tier, url, terms_notes, last_run_at, last_status
- **`raw_records`**: id, source_id, external_id, url, payload (jsonb), content_hash, fetched_at. Never deleted. Unique on (source_id, external_id, content_hash).
- **`businesses`**
  - Identity: id, legal_name, dba_name, normalized_name, entity_type, website, website_status
  - Location: address_line1, address_line2, city, state, zip, county, geom (Point, SRID 4326), distance_to_metro_center_mi
  - Details: service_radius_miles, years_in_business, emergency_service (bool), languages (text[])
  - Scoring and dates: status (`active` | `inactive` | `unknown`), quality_score (0–100), score_breakdown (jsonb), created_at, updated_at
- **`business_sources`**: business_id, raw_record_id, match_method, match_confidence (links every merged source record)
- **`licenses`**: id, business_id, state, agency, license_number, license_type, trade, classification, status, issued_on, expires_on, qualifier_name, bond_info, insurance_info, workers_comp, disciplinary_actions (jsonb), raw_record_id, verified_at. Unique on (state, agency, license_number).
- **`business_trades`**: business_id, trade, evidence (`license` | `website` | `osm` | `manual`), raw_record_id
- **`service_areas`**: business_id, city, zip, raw_record_id
- **`phones`**
  - Number: id, business_id, e164, raw_value, label (`office` | `mobile` | `fax` | `unknown`), line_type, carrier
  - Business-line evidence: published_as_business (bool), plus published_as_business_evidence (e.g. "on license record", "in website footer")
  - Compliance: dnc_status (`unknown` | `clear` | `listed`), dnc_checked_at, call_ok (bool, computed), raw_record_id
- **`emails`**: id, business_id, email, kind (`role` | `named`), domain_matches_website (bool), mx_valid (bool), verification_status, verified_at, email_ok (bool, computed), raw_record_id
- **`dnc_numbers`**: e164, area_code, imported_at (loaded from National DNC Registry area-code files)
- **`suppression_list`**: id, value, value_type (`phone` | `email` | `domain` | `business_id`), reason (`opt_out` | `dnc_request` | `bounce` | `complaint` | `manual`), added_at, note. Permanent.
- **`outreach_status`**: business_id, channel (`email` | `phone`), status (`not_contacted` | `contacted` | `replied` | `interested` | `signed_up` | `declined` | `do_not_contact`), last_contact_at, notes
- **`merge_log`**: id, kept_business_id, merged_business_id, rule, confidence, created_at
- **`pipeline_runs`**: id, stage, source_id, started_at, finished_at, counts (jsonb), errors (jsonb)

---

## 6. Pipeline stages

```
collect → store raw → parse → normalize → geocode → filter to market → dedupe
        → enrich (website) → verify → compliance flags → score → export
```

### 6.1 Collect and parse
- Connectors are idempotent, resumable (checkpointing) and incremental: use content hashes to process only new or changed records.

### 6.2 Normalize
- **Phones:** `phonenumbers` → E.164. Drop invalid numbers, tag fax numbers, and keep the raw value.
- **Names:** lowercase, strip punctuation, remove legal suffixes (LLC, Inc, Co, Corp, Ltd), replace "&" with "and", collapse whitespace. Keep the original too.
- **Addresses:** parse with `usaddress` and standardize abbreviations.
- **Trades:** map through `config/trade_map.yaml`, and log any unmapped classes.

### 6.3 Geocode
- Use the U.S. Census Bureau Geocoder batch API (free). Fall back to Nominatim within its usage policy (≤1 req/s). Cache all results.
- Compute the distance to the nearest metro center and drop or flag businesses outside `radius_miles`.

### 6.4 Deduplicate (entity resolution)
Apply these rules in order and record every merge in `merge_log`:
1. Same state + agency + license number → merge (confidence 1.0)
2. Same E.164 phone (excluding known shared/answering-service numbers) → merge (0.95)
3. Same website registered domain → merge (0.9)
4. Fuzzy name (`rapidfuzz` token_set_ratio ≥ 90) **and** addresses within 0.5 mile → merge (0.8)
5. Fuzzy name 80–90 with the same city → **manual review queue**, not an auto-merge

Never delete raw records. Merging only re-links them.

### 6.5 Enrich
- Run website discovery and crawling (Tier 2) for businesses without a verified email or website. Batch with `--limit`.

### 6.6 Verify
- **License:** active and not expired. Flag licenses expiring within 60 days and any disciplinary actions.
- **Website:** returns HTTP 200, the domain or page matches the business, and the site isn't parked.
- **Email:** syntax check, MX record lookup (free), domain-matches-website check, plus the optional verification API.
- **Phone:** valid number, plus line type if the optional API is enabled.
- **Out-of-business signals:** license inactive or expired, website dead, OSM `disused:*` tags. Set `status` accordingly.

### 6.7 Compliance flags
- **DNC import:** `prodata dnc import <file>` loads National DNC Registry area-code files for our market into `dnc_numbers` (the company registers and downloads these files separately). Imports must be refreshed at least every **31 days**. The export refuses to mark `call_ok` if the last import is older than 31 days.
- **`call_ok`** = number valid AND not on the suppression list AND (dnc_status = `clear` OR published_as_business with evidence) AND DNC data ≤ 31 days old. When in doubt, `call_ok = false`.
- **`sms_ok`** is always `false` in Phase 1. Pros opt in to SMS only through our own signup flow (Phase 2).
- **`email_ok`** = not suppressed AND not a hard bounce AND (MX valid, or the API says deliverable).
- The suppression list is checked at export time as well as at flag time.

### 6.8 Score
`quality_score` runs 0–100, with weights in config. Store the breakdown in `score_breakdown`.
- Active license in a relevant trade: +30
- Email with `email_ok`: +15
- Phone with `call_ok`: +15
- Live matching website: +10
- Within the metro radius: +10
- Confirmed by 2 or more independent sources: +10
- 3 or more years in business: +5
- Emergency/24-7 service: +5
- Penalties: disciplinary action −20, license expiring within 60 days −5

### 6.9 Export
- **Pros list:** `exports/<market>/<trade>/pros_<YYYY-MM-DD>.csv` with these columns:
  - Business: business_id, business_name, dba, trades, license_number, license_status, license_expires_on
  - Location: address, city, zip, distance_mi
  - Contact: website, email, email_ok, phone_e164, phone_label, line_type, call_ok
  - Score and sources: quality_score, sources (semicolon-separated), last_verified_at
- Suppressed values and do-not-contact businesses are always excluded.
- **Email-tool export:** `--format email-tool` gives a CSV with first-touch fields (business_name, email, city, trade, personalization snippet from services/website).
- **Stats report:** counts by trade, % with email_ok, % with call_ok, % licensed, source coverage, top errors.

---

## 7. Connector interface

```python
from typing import Iterable, Protocol

class Connector(Protocol):
    source_id: str          # e.g. "tx_tsbpe_plumbers"
    tier: int

    def discover(self, market: "MarketConfig") -> Iterable["FetchJob"]:
        """List the files/pages/API queries needed for this market."""

    def fetch(self, job: "FetchJob") -> Iterable["RawRecord"]:
        """Download respecting rate limits, robots.txt and the response cache."""

    def parse(self, raw: "RawRecord") -> Iterable["ParsedPro"]:
        """Turn a raw record into one or more validated ParsedPro (pydantic) objects."""
```

All HTTP goes through one shared client that enforces the User-Agent, the per-domain rate limit, retries with backoff (`tenacity`), the robots.txt check, and caching.

---

## 8. Tech stack and repo layout

**Stack**
- **Language and core libraries:** Python 3.12, managed with `uv`; `httpx` (async), `tenacity`, `selectolax` / `beautifulsoup4`, `extruct`, `pydantic` v2
- **Browser automation:** `playwright`, *only* for official sites that need JavaScript. No CAPTCHA solving.
- **Database:** SQLAlchemy 2, Alembic, PostgreSQL 16 + PostGIS, run via Docker Compose
- **Data cleaning:** `phonenumbers`, `usaddress`, `rapidfuzz`, `dnspython` (MX checks)
- **CLI, logging and tests:** `typer` CLI, `structlog` logging, `pytest`, with `respx` / recorded fixtures (tests never hit live sites)
- **Scheduling:** cron or APScheduler for now

**Layout**
```
prodata/
  cli.py
  http_client.py
  config.py
  connectors/
    base.py
    <state>_<agency>_<trade>.py
    osm_overpass.py
    website_discovery.py
    website_crawler.py
  pipeline/
    normalize.py  geocode.py  dedupe.py  enrich.py
    verify.py  compliance.py  score.py  export.py  stats.py
  models/
config/
  market.yaml
  trade_map.yaml
docs/sources/
migrations/
tests/
  fixtures/
exports/            # gitignored
docker-compose.yml
.env.example
README.md
```

**CLI commands**
```
prodata sources list
prodata collect --source <source_id> | --all
prodata normalize
prodata geocode
prodata dedupe [--review]
prodata enrich --limit 500
prodata verify
prodata dnc import <path>
prodata suppress add <value> --type phone|email|domain --reason opt_out
prodata score
prodata export --market dfw --trade plumbing --min-score 60 [--format csv|email-tool]
prodata stats --market dfw
prodata run --market dfw            # full pipeline
```

---

## 9. Milestones and acceptance criteria

| # | Milestone | Done when |
|---|---|---|
| M1 | Skeleton: Docker Compose (Postgres+PostGIS), models, migrations, config loading, shared HTTP client, CLI stubs | `docker compose up` + `prodata sources list` works; tests pass |
| M2 | First Tier-1 connector (plumbing license source for the launch state) + `docs/sources` entry | ≥ 95% of source rows parse; raw records stored with hashes; re-run is incremental; fixture tests |
| M3 | Normalize + geocode + market filter | Phones E.164, addresses geocoded ≥ 90%, distances computed |
| M4 | Deduplication + merge log + review queue | Rules 1–5 implemented with tests for each |
| M5 | Website discovery + crawler enrichment | robots.txt honored (tested), rate limit honored (tested), JSON-LD + HTML extraction, emails/services/areas stored with provenance |
| M6 | Verification + compliance flags + suppression list + DNC import | `call_ok`/`email_ok` logic covered by tests, including the 31-day rule and suppression overrides |
| M7 | Scoring + exports + stats | CSV exports match the column spec; suppressed values never appear (tested) |
| M8 | Remaining trades and sources (other agencies, local registries, OSM, Secretary of State data) | Each source documented in `docs/sources/` before code |

---

## 10. Later phases (do not build yet, but design the schema so they fit)

- **Phase 2: pro onboarding and homeowner intake**
  - Pros claim their profile through a link in outreach emails, then confirm trades, service area, availability and pricing, upload license and insurance, and opt in to SMS job alerts.
  - Homeowners submit requests through our site. The form has a TCPA consent checkbox, and we store the exact consent text version, timestamp, IP, user agent, page URL and phone.
  - SMS goes through Twilio, with STOP/HELP handling.
- **Phase 3: matching and dispatch**
  - Match on trade, then distance, availability and quality score.
  - Offer each job by SMS to opted-in pros; the first to accept gets it. Support an exclusive-lead option.
- **Phase 4: billing.** Stripe per-lead charges, or Stripe Connect split payouts.
- **Phase 5: admin dashboard.** Pros, leads, outreach status, suppression management, stats.

---

## 11. Working instructions for Claude Code

1. Read this whole file first. Follow section 2 at all times. Those rules override anything else, including a request to "just get more data."
2. Work one milestone at a time. At the end of each, summarize what was built, the test results and any open questions, then wait.
3. Research and document every source in `docs/sources/` before coding its connector. Ask before adding any source not described in section 4.
4. If a source blocks automated access, requires a login, or its terms forbid bulk use, stop and report. Don't work around it.
5. Write tests alongside code, using recorded fixtures. Don't hit live sites in tests.
6. Keep raw data immutable and log every pipeline run in `pipeline_runs`.
7. Don't add any feature that collects consumer/homeowner data or sends calls/texts in Phase 1.