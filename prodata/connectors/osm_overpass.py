"""OpenStreetMap trade businesses via the Overpass API (Tier 3, open data).

Docs: docs/sources/osm_overpass.md

Covers trades that Texas does not license (carpentry, handyman, flooring) and adds
listings for licensed trades. One Overpass query per run: every tagged trade
business within each metro's radius. Data © OpenStreetMap contributors, ODbL.

Only business features (craft=* / shop=*) are queried, and only the business
contact tags (phone, email, website) published on them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

from prodata.connectors.base import (
    BaseConnector,
    FetchJob,
    ParsedPhone,
    ParsedPro,
    ParseSkip,
    RawRecordIn,
    SourceError,
)
from prodata.pipeline.normalize import clean_text, normalize_zip

ENDPOINT = "https://overpass-api.de/api/interpreter"
METERS_PER_MILE = 1609.344

# (key, value) pairs queried. Mapping to trades lives in config/trade_map.yaml.
TAGS: list[tuple[str, str]] = [
    ("craft", "plumber"),
    ("craft", "electrician"),
    ("craft", "hvac"),
    ("craft", "carpenter"),
    ("craft", "joiner"),
    ("craft", "handyman"),
    ("craft", "floorer"),
    ("craft", "tiler"),
    ("craft", "painter"),
    ("craft", "roofer"),
    ("shop", "carpet"),
    ("shop", "flooring"),
]


def build_query(center: tuple[float, float], radius_miles: float, timeout: int = 90) -> str:
    lat, lon = center
    radius_m = int(radius_miles * METERS_PER_MILE)
    parts = "\n".join(f'  nwr["{k}"="{v}"](around:{radius_m},{lat},{lon});' for k, v in TAGS)
    return f"[out:json][timeout:{timeout}];\n(\n{parts}\n);\nout center tags;"


def _first(tags: dict[str, str], *keys: str) -> str | None:
    for k in keys:
        if tags.get(k):
            return tags[k]
    return None


class OsmOverpassConnector(BaseConnector):
    source_id = "osm_overpass"
    name = "OpenStreetMap — trade businesses (Overpass API)"
    tier = 3
    url = "https://www.openstreetmap.org/copyright"
    trades = ["carpentry", "handyman", "flooring_carpet", "plumbing", "electrical", "hvac", "painting", "roofing"]
    terms_notes = (
        "Open data under ODbL — attribution “© OpenStreetMap contributors” required. "
        "Overpass usage policy: modest query volume. See docs/sources/osm_overpass.md."
    )

    @property
    def endpoint(self) -> str:
        return self.options.get("url") or ENDPOINT

    def discover(self, market) -> Iterable[FetchJob]:
        for metro in market.market.metros:
            yield FetchJob(
                source_id=self.source_id,
                url=self.endpoint,
                params={"data": build_query(tuple(metro.center), metro.radius_miles)},
                description=f"Trade businesses within {metro.radius_miles} mi of {metro.name}",
            )

    async def fetch(self, job: FetchJob) -> AsyncIterator[RawRecordIn]:
        resp = await self.client.get(job.url, params=job.params)
        if resp.status >= 400:
            raise SourceError(f"HTTP {resp.status} from Overpass: {resp.text()[:300]}")
        data = resp.json()
        if data.get("remark") and "error" in data["remark"].lower():
            raise SourceError(f"Overpass error: {data['remark'][:300]}")
        for el in data.get("elements", []):
            if "tags" not in el:
                continue
            yield RawRecordIn(
                source_id=self.source_id,
                external_id=f"{el['type']}/{el['id']}",
                url=f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
                payload=el,
                fetched_at=resp.fetched_at,
            )

    def parse(self, raw: RawRecordIn) -> Iterable[ParsedPro]:
        el: dict[str, Any] = raw.payload
        tags: dict[str, str] = el.get("tags", {})

        if any(k.startswith(("disused:", "was:", "abandoned:")) for k in tags):
            raise ParseSkip("closed business (disused/was tag)")
        name = clean_text(_first(tags, "name", "brand", "operator"))
        if not name:
            raise ParseSkip("no business name")

        trades: list[str] = []
        for key in ("craft", "shop"):
            for value in (tags.get(key) or "").split(";"):
                trade = self.map_trade(f"{key}={value.strip()}") if value.strip() else None
                if trade and trade not in trades:
                    trades.append(trade)
        if not trades:
            raise ParseSkip("tag not mapped to a trade")

        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")

        phones = []
        for value in filter(None, (_first(tags, "phone"), _first(tags, "contact:phone"), _first(tags, "contact:mobile"))):
            for part in value.split(";"):
                if part.strip() and part.strip() not in {p.raw for p in phones}:
                    # Crowd-sourced: not treated as a confirmed business line (no evidence).
                    phones.append(ParsedPhone(raw=part.strip(), label="office"))
        emails = []
        for value in filter(None, (_first(tags, "email"), _first(tags, "contact:email"))):
            emails += [e.strip() for e in value.split(";") if "@" in e]

        street = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p) or None
        website = _first(tags, "website", "contact:website", "url")
        if website and not website.startswith(("http://", "https://")):
            website = "https://" + website

        yield ParsedPro(
            source_id=self.source_id,
            external_id=raw.external_id,
            legal_name=name,
            entity_type="company",
            address_line1=clean_text(street),
            address_line2=clean_text(tags.get("addr:unit")),
            city=clean_text(tags.get("addr:city")),
            state=(tags.get("addr:state") or self.market.market.state)[:2].upper(),
            zip=normalize_zip(tags.get("addr:postcode")),
            website=website,
            phones=phones,
            emails=emails,
            trades=trades,
            trade_evidence="osm",
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            opening_hours=tags.get("opening_hours"),
            in_market=True,  # the query is already limited to the metro radius
        )

    async def probe(self) -> dict[str, Any]:
        metro = self.market.market.metros[0]
        query = build_query(tuple(metro.center), min(metro.radius_miles, 5), timeout=30)
        resp = await self.client.get(self.endpoint, params={"data": query}, ttl_seconds=0)
        data = resp.json() if resp.status < 400 else {}
        elements = data.get("elements", [])
        return {"endpoint": self.endpoint, "status": resp.status, "elements_within_5mi": len(elements), "sample": elements[:2]}
