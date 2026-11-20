# tx_tdlr_licenses — Texas Department of Licensing and Regulation (TDLR)

| | |
|---|---|
| Tier | 1 (official licence data) |
| Trades | hvac (Air Conditioning & Refrigeration Contractors), electrical (Electrical Contractors) |
| Agency | Texas Department of Licensing and Regulation |
| Dataset | "TDLR - All Licenses", Socrata id `7358-krk7`, on the Texas Open Data Portal |
| API | `https://data.texas.gov/resource/7358-krk7.json` (SODA) |
| Also | Flat-file downloads listed at https://www.tdlr.texas.gov/LicenseSearch/licfile.asp |
| Update frequency | Refreshed regularly by TDLR; confirm `rowsUpdatedAt` in the dataset metadata |
| Access | Public, no login. An optional free Socrata app token (`SOCRATA_APP_TOKEN`) raises rate limits |
| Connector | `prodata/connectors/tx_tdlr_licenses.py` |

## What we use, and why

The open-data API lets us ask only for what the market needs, with no bulk download:

```
$where = upper(license_type) in (<contractor types from config/trade_map.yaml>)
         AND upper(business_county) in (<counties from config/market.yaml>)
```

**Only contractor-level licence types** are queried (business licences). Technicians,
journeymen and apprentices are individuals and are not imported.
**The owner's phone is never imported**, only the published business phone.

## Fields (expected API names)

`license_type`, `license_number`, `license_subtype`, `business_name`, `business_county`,
`business_address_line1`, `business_address_line2`, `business_city_state_zip`,
`business_telephone`, `owner_name` (→ `qualifier_name`), `license_expiration_date_mmddccyy`.

## Verification status — needs a live check

Research ran from a sandbox that couldn't reach `data.texas.gov` or `tdlr.texas.gov`, so the
following are **not yet confirmed against the live API**:

- [ ] dataset id `7358-krk7` is current on data.texas.gov (it is also referenced via the City of Austin portal)
- [ ] exact field names above, especially `business_county` and `license_type`, which the server-side filter depends on
- [ ] exact licence-type labels (e.g. "A/C Contractor", "Electrical Contractor"); add variants to `config/trade_map.yaml`
- [ ] the portal's terms of use and robots.txt

```bash
uv run prodata sources probe tx_tdlr_licenses   # prints columns, field mapping and all licence types with counts
```

A wrong field name makes the API return HTTP 400. The connector stops and reports it, pointing to `probe`.
