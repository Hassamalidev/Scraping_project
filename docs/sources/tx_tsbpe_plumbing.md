# tx_tsbpe_plumbing — Texas State Board of Plumbing Examiners (TSBPE)

| | |
|---|---|
| Tier | 1 (official licence data) |
| Trade | plumbing |
| Agency | Texas State Board of Plumbing Examiners — licenses plumbers in Texas |
| Page | https://tsbpe.texas.gov/free-licensee-list/ ("Free Licensee List") |
| Format | CSV, one file per licence category |
| Update frequency | Daily (the board states tables are current as of the previous work day) |
| Access | Public download, no login. Individual lookups: https://www.tsbpe.texas.gov/search/license-search |
| Connector | `prodata/connectors/tx_tsbpe_plumbing.py` |

## What we use, and why

The board publishes separate CSV files for Responsible Master Plumbers (RMP), Master,
Journeyman, Tradesman Plumber-Limited, Plumber's Apprentice and Plumbing Inspector.

**We import only the Responsible Master Plumber list.** RMPs are the licence holders allowed to
offer and contract plumbing work to the public, and the board describes this spreadsheet as
including company details. The other files describe individual workers, not businesses, so
importing them would collect personal data that isn't a business contact (spec rule 1).

## Access rules

- Official bulk download intended for public use; no scraping of search pages.
- The connector fetches the list page once (cached 6 h) to find the current RMP file link, then
  downloads the file (cached 20 h, since it changes daily). One request per second, robots.txt honoured.
- Public information under the Texas Public Information Act; see the board's
  [Open Records policy](https://tsbpe.texas.gov/open-records/).

## Fields

Captured as published: licence number, licence type, licensee (RMP) name → `qualifier_name`,
company name, company address, city, state, ZIP, county (if present), company phone,
email (if present), issue/expiration dates (if present).

Missing a status column, a licence is recorded as `listed` (present on the board's current list),
not assumed active. M6 verification decides activity.

## Verification status — needs a live check

Research was done from a sandbox that couldn't reach `tsbpe.texas.gov`, so the following were
**not confirmed against the live site**:

- [ ] robots.txt allows `/free-licensee-list/` and the CSV path
- [ ] exact RMP link text / file name (the connector matches links containing "Responsible" or "RMP")
- [ ] exact CSV column names (the connector accepts common variants via `FIELDS`)
- [ ] any terms or disclaimer text on the download page

Run once and update this page and the `FIELDS` aliases with the result:

```bash
uv run prodata sources probe tx_tsbpe_plumbing
```

If the page or terms forbid automated download, stop and report (spec rule 3). Don't work around it.
