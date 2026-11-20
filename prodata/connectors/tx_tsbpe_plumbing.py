"""Texas State Board of Plumbing Examiners — Responsible Master Plumber list (Tier 1).

Docs: docs/sources/tx_tsbpe_plumbing.md

Only the Responsible Master Plumber (RMP) file is used: RMPs are the licence holders
allowed to offer and contract plumbing work to the public, and the file carries
company details. Journeyman / apprentice / tradesman files describe individual
workers rather than businesses, so they are deliberately not imported.

The download link is discovered from the official "Free Licensee List" page instead
of being hard-coded, because file names there change. Override with
`options.url` (or `--url` on the CLI) if the page layout changes.
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

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

LIST_PAGE = "https://tsbpe.texas.gov/free-licensee-list/"
DATA_EXTENSIONS = (".csv", ".txt", ".xlsx", ".xls")

FIELDS = FieldMap(
    {
        "license_number": ["license_number", "license_no", "license", "lic_no", "lic_number", "license_num", "licnum"],
        "license_type": ["license_type", "license_type_desc", "type", "license_desc", "rank"],
        "status": ["status", "license_status"],
        "company": ["company_name", "company", "business_name", "firm_name", "employer", "employer_name"],
        "dba": ["dba", "dba_name", "doing_business_as"],
        "person": ["name", "licensee_name", "full_name", "licensee", "rmp_name"],
        "first_name": ["first_name", "fname"],
        "last_name": ["last_name", "lname"],
        "address1": ["company_address", "business_address", "address", "address1", "address_1", "street", "mailing_address"],
        "address2": ["company_address2", "address2", "address_2", "suite"],
        "city": ["company_city", "business_city", "city"],
        "state": ["company_state", "business_state", "state"],
        "zip": ["company_zip", "business_zip", "zip", "zip_code", "zipcode", "postal_code"],
        "city_state_zip": ["city_state_zip", "company_city_state_zip"],
        "county": ["company_county", "business_county", "county"],
        "phone": ["company_phone", "business_phone", "phone", "phone_number", "telephone"],
        "email": ["company_email", "business_email", "email"],
        "issued": ["issue_date", "original_issue_date", "issued", "license_issue_date"],
        "expires": ["expiration_date", "exp_date", "expires", "license_expiration_date", "expiration"],
        "insurance": ["insurance", "insurance_carrier", "insurance_company"],
    }
)


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def find_rmp_link(html: str, base_url: str = LIST_PAGE) -> str:
    """Pick the Responsible Master Plumber data file from the list page."""
    parser = _LinkCollector()
    parser.feed(html)
    candidates = []
    for href, text in parser.links:
        if not href:
            continue
        label = f"{text} {href}".lower()
        is_data = href.lower().split("?")[0].endswith(DATA_EXTENSIONS)
        if is_data and ("responsible" in label or "rmp" in label):
            candidates.append(urljoin(base_url, href))
    if not candidates:
        raise SourceError(
            "Could not find the Responsible Master Plumber download link on "
            f"{base_url}. Check the page and pass the file URL with --url."
        )
    # Prefer CSV when the board publishes several formats.
    candidates.sort(key=lambda u: 0 if u.lower().split("?")[0].endswith(".csv") else 1)
    return candidates[0]


def decode_table(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def iter_csv_rows(text: str) -> Iterable[dict[str, Any]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|")
    except csv.Error:
        dialect = csv.excel
    yield from csv.DictReader(io.StringIO(text), dialect=dialect)


class TsbpePlumbingConnector(BaseConnector):
    source_id = "tx_tsbpe_plumbing"
    name = "Texas State Board of Plumbing Examiners — Responsible Master Plumbers"
    tier = 1
    url = LIST_PAGE
    trades = ["plumbing"]
    terms_notes = (
        "Public licensee list published by the state board for free download; "
        "updated daily. Texas Public Information Act data. See docs/sources/tx_tsbpe_plumbing.md."
    )

    def discover(self, market) -> Iterable[FetchJob]:
        # The real file URL is resolved in fetch() (it needs one page request).
        yield FetchJob(
            source_id=self.source_id,
            url=self.options.get("url") or LIST_PAGE,
            description="Responsible Master Plumber list (statewide file, filtered to market at parse time)",
        )

    async def resolve_file_url(self, job: FetchJob) -> str:
        if job.url != LIST_PAGE:
            return job.url
        page = await self.client.get(LIST_PAGE, ttl_seconds=6 * 3600)
        return find_rmp_link(page.text(), LIST_PAGE)

    async def fetch(self, job: FetchJob) -> AsyncIterator[RawRecordIn]:
        file_url = await self.resolve_file_url(job)
        resp = await self.client.get(file_url)
        if resp.status >= 400:
            raise SourceError(f"HTTP {resp.status} downloading {file_url}")
        text = decode_table(resp.read_bytes())
        seen: dict[str, int] = {}
        for row in iter_csv_rows(text):
            payload = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            norm = FIELDS.normalize_row(payload)
            number = FIELDS.get(norm, "license_number")
            if not number:
                continue
            key = f"{number}|{FIELDS.get(norm, 'license_type') or ''}"
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:  # same licence listed twice (e.g. two addresses)
                key = f"{key}|{seen[key]}"
            yield RawRecordIn(
                source_id=self.source_id,
                external_id=key,
                url=file_url,
                payload=payload,
                fetched_at=resp.fetched_at,
            )

    def parse(self, raw: RawRecordIn) -> Iterable[ParsedPro]:
        row = FIELDS.normalize_row(raw.payload)
        get = lambda f: FIELDS.get(row, f)  # noqa: E731

        number = get("license_number")
        if not number:
            raise ParseSkip("no licence number")

        license_type = get("license_type") or "Responsible Master Plumber"
        trade = self.map_trade(license_type)
        if trade is None:
            raise ParseSkip(f"licence class not mapped to a trade: {license_type}")

        person = get("person") or " ".join(p for p in (get("first_name"), get("last_name")) if p) or None
        company = get("company")
        legal_name = company or person
        if not legal_name:
            raise ParseSkip("no business or licensee name")

        city, state, zip_code = get("city"), get("state"), normalize_zip(get("zip"))
        if not city and get("city_state_zip"):
            city, state, zip_code = split_city_state_zip(get("city_state_zip"))
        county = get("county")

        phones = []
        if get("phone"):
            phones.append(ParsedPhone(raw=get("phone"), label="office", evidence="on license record"))
        emails = [get("email")] if get("email") and "@" in get("email") else []

        yield ParsedPro(
            source_id=self.source_id,
            external_id=raw.external_id,
            legal_name=clean_text(legal_name),
            dba_name=clean_text(get("dba")),
            entity_type="company" if company else "sole_proprietor_or_unknown",
            address_line1=clean_text(get("address1")),
            address_line2=clean_text(get("address2")),
            city=clean_text(city),
            state=(state or "TX").upper()[:2],
            zip=zip_code,
            county=clean_text(county),
            phones=phones,
            emails=emails,
            license=ParsedLicense(
                state="TX",
                agency="TSBPE",
                license_number=number,
                license_type=license_type,
                trade=trade,
                classification=license_type,
                # No status column means "present on the board's current list" — recorded
                # as such, not assumed active; verification (M6) decides.
                status=clean_text(get("status")) or "listed",
                issued_on=parse_date(get("issued")),
                expires_on=parse_date(get("expires")),
                qualifier_name=clean_text(person),
                insurance_info=clean_text(get("insurance")),
            ),
            trades=[trade],
            in_market=self.in_market(county, zip_code),
        )

    async def probe(self) -> dict[str, Any]:
        job = next(iter(self.discover(self.market)))
        file_url = await self.resolve_file_url(job)
        resp = await self.client.get(file_url)
        text = decode_table(resp.read_bytes())
        rows = iter_csv_rows(text)
        first = next(rows, {}) or {}
        count = 1 + sum(1 for _ in rows) if first else 0
        return {
            "file_url": file_url,
            "row_count": count,
            "columns": list(first.keys()),
            "field_mapping": FIELDS.report(first.keys()),
            "sample": first,
        }
