"""Texas Department of Licensing and Regulation — licence holders (Tier 1).

Docs: docs/sources/tx_tdlr_licenses.md

Uses the state open-data portal (Socrata) dataset "TDLR - All Licenses"
(id 7358-krk7) through its SODA API, filtered server-side to the contractor
licence types mapped in config/trade_map.yaml and to the market's counties, so we
only download what the market needs.

Only *contractor* licence types are queried (business-level). Individual licences
(technicians, journeymen, apprentices) are not businesses and are not imported.
Owner phone numbers are never imported — only the published business phone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

from prodata.config import get_settings
from prodata.connectors.base import (
    BaseConnector,
    FetchJob,
    FieldMap,
    ParsedLicense,
    ParsedPhone,
    ParsedPro,
    ParseSkip,
    RawRecordIn,
    SourceError,
)
from prodata.pipeline.normalize import clean_text, normalize_zip, parse_date, split_city_state_zip

DOMAIN = "https://data.texas.gov"
DATASET_ID = "7358-krk7"
PAGE_SIZE = 5000

FIELDS = FieldMap(
    {
        "license_type": ["license_type"],
        "license_subtype": ["license_subtype"],
        "license_number": ["license_number"],
        "county": ["business_county", "county"],
        "business_name": ["business_name"],
        "address1": ["business_address_line1", "business_address_line_1"],
        "address2": ["business_address_line2", "business_address_line_2"],
        "city_state_zip": ["business_city_state_zip"],
        "zip": ["business_zip", "zip"],
        "phone": ["business_telephone", "business_phone"],
        "owner_name": ["owner_name"],
        "expires": ["license_expiration_date_mmddccyy", "license_expiration_date", "expiration_date"],
    }
)


def _soql_list(values: Iterable[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


class TdlrLicensesConnector(BaseConnector):
    source_id = "tx_tdlr_licenses"
    name = "Texas Department of Licensing and Regulation — contractor licences (open data)"
    tier = 1
    url = f"{DOMAIN}/d/{DATASET_ID}"
    trades = ["hvac", "electrical"]
    terms_notes = (
        "State open-data portal (Socrata SODA API). Public licence data; optional free "
        "app token raises rate limits. See docs/sources/tx_tdlr_licenses.md."
    )

    @property
    def endpoint(self) -> str:
        return self.options.get("url") or f"{DOMAIN}/resource/{DATASET_ID}.json"

    @property
    def headers(self) -> dict[str, str]:
        token = get_settings().socrata_app_token
        return {"X-App-Token": token} if token else {}

    def contractor_types(self) -> list[str]:
        mapping = self.trade_map._maps.get(self.source_id, {})
        return sorted(label.upper() for label, trade in mapping.items() if trade)

    def where_clause(self) -> str:
        clauses = [f"upper(license_type) in ({_soql_list(self.contractor_types())})"]
        counties = [c.upper() for c in self.market.market.counties]
        if counties:
            clauses.append(f"upper(business_county) in ({_soql_list(counties)})")
        return " AND ".join(clauses)

    def discover(self, market) -> Iterable[FetchJob]:
        yield FetchJob(
            source_id=self.source_id,
            url=self.endpoint,
            params={"$where": self.where_clause(), "$order": ":id"},
            description="Contractor licences in market counties",
        )

    async def fetch(self, job: FetchJob) -> AsyncIterator[RawRecordIn]:
        offset = 0
        while True:
            params = {**job.params, "$limit": PAGE_SIZE, "$offset": offset}
            resp = await self.client.get(job.url, params=params, headers=self.headers)
            if resp.status >= 400:
                raise SourceError(
                    f"HTTP {resp.status} from {resp.url}: {resp.text()[:300]} — "
                    "field names may have changed; run `prodata sources probe tx_tdlr_licenses`."
                )
            rows = resp.json()
            if not isinstance(rows, list):
                raise SourceError(f"Unexpected response from {resp.url}: {str(rows)[:300]}")
            for row in rows:
                norm = FIELDS.normalize_row(row)
                number = FIELDS.get(norm, "license_number")
                if not number:
                    continue
                yield RawRecordIn(
                    source_id=self.source_id,
                    external_id=f"{number}|{FIELDS.get(norm, 'license_type') or ''}",
                    url=resp.url,
                    payload=row,
                    fetched_at=resp.fetched_at,
                )
            if len(rows) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

    def parse(self, raw: RawRecordIn) -> Iterable[ParsedPro]:
        row = FIELDS.normalize_row(raw.payload)
        get = lambda f: FIELDS.get(row, f)  # noqa: E731

        license_type = get("license_type")
        trade = self.map_trade(license_type)
        if trade is None:
            raise ParseSkip(f"licence type not imported: {license_type}")

        owner = clean_text(get("owner_name"))
        business = clean_text(get("business_name"))
        legal_name = business or owner
        if not legal_name:
            raise ParseSkip("no business or owner name")

        city, state, zip_code = split_city_state_zip(get("city_state_zip"))
        zip_code = zip_code or normalize_zip(get("zip"))
        county = clean_text(get("county"))

        phones = []
        if get("phone"):
            phones.append(ParsedPhone(raw=get("phone"), label="office", evidence="on license record"))

        yield ParsedPro(
            source_id=self.source_id,
            external_id=raw.external_id,
            legal_name=legal_name,
            entity_type="company" if business else "sole_proprietor_or_unknown",
            address_line1=clean_text(get("address1")),
            address_line2=clean_text(get("address2")),
            city=city,
            state=state or "TX",
            zip=zip_code,
            county=county,
            phones=phones,
            license=ParsedLicense(
                state="TX",
                agency="TDLR",
                license_number=get("license_number"),
                license_type=license_type,
                trade=trade,
                classification=clean_text(get("license_subtype")) or license_type,
                status="listed",
                expires_on=parse_date(get("expires")),
                qualifier_name=owner,
            ),
            trades=[trade],
            in_market=self.in_market(county, zip_code),
        )

    async def probe(self) -> dict[str, Any]:
        sample = await self.client.get(self.endpoint, params={"$limit": 3}, headers=self.headers, ttl_seconds=0)
        rows = sample.json() if sample.status < 400 else []
        types = await self.client.get(
            self.endpoint,
            params={"$select": "license_type, count(*) as n", "$group": "license_type", "$order": "n DESC", "$limit": 500},
            headers=self.headers,
            ttl_seconds=0,
        )
        columns = sorted({k for r in rows for k in r})
        return {
            "endpoint": self.endpoint,
            "status": sample.status,
            "columns": columns,
            "field_mapping": FIELDS.report(columns),
            "license_types": types.json() if types.status < 400 else types.text()[:500],
            "sample": rows[:1],
        }
