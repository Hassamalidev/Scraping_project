"""Connector interface (spec section 7) and the records passed between stages."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterable
from datetime import date, datetime
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, Field

from prodata.config import MarketConfig, TradeMap
from prodata.http_client import HttpClient


class SourceError(Exception):
    """A connector cannot continue (bad format, link not found…). Stop and report."""


class FetchJob(BaseModel):
    source_id: str
    url: str
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class RawRecordIn(BaseModel):
    source_id: str
    external_id: str
    url: str
    payload: dict[str, Any]
    fetched_at: datetime

    @property
    def content_hash(self) -> str:
        return content_hash(self.payload)


class ParsedPhone(BaseModel):
    raw: str
    label: str = "office"
    evidence: str | None = None  # e.g. "on license record"


class ParsedLicense(BaseModel):
    state: str
    agency: str
    license_number: str
    license_type: str | None = None
    trade: str | None = None
    classification: str | None = None
    status: str | None = None
    issued_on: date | None = None
    expires_on: date | None = None
    qualifier_name: str | None = None
    bond_info: str | None = None
    insurance_info: str | None = None
    workers_comp: str | None = None
    disciplinary_actions: list[Any] | None = None


class ParsedPro(BaseModel):
    """One business as described by one raw record. Validated before loading."""

    source_id: str
    external_id: str
    legal_name: str = Field(min_length=1)
    dba_name: str | None = None
    entity_type: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    county: str | None = None
    website: str | None = None
    phones: list[ParsedPhone] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    license: ParsedLicense | None = None
    trades: list[str] = Field(default_factory=list)
    trade_evidence: str = "license"  # license|website|osm|manual
    lat: float | None = None
    lon: float | None = None
    opening_hours: str | None = None
    in_market: bool = True


class ParseSkip(Exception):
    """Row is valid but intentionally not imported (individual licence, out of market…)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Connector(Protocol):
    source_id: str
    tier: int

    def discover(self, market: MarketConfig) -> Iterable[FetchJob]: ...

    def fetch(self, job: FetchJob) -> AsyncIterator[RawRecordIn]: ...

    def parse(self, raw: RawRecordIn) -> Iterable[ParsedPro]: ...


class BaseConnector:
    """Shared plumbing. Subclasses set the class attributes and implement the three steps."""

    source_id: ClassVar[str]
    name: ClassVar[str]
    tier: ClassVar[int]
    url: ClassVar[str]
    terms_notes: ClassVar[str] = ""
    trades: ClassVar[list[str]] = []

    def __init__(self, client: HttpClient, market: MarketConfig, trade_map: TradeMap, options: dict | None = None):
        self.client = client
        self.market = market
        self.trade_map = trade_map
        self.options = options or {}
        self.unmapped_classes: dict[str, int] = {}

    def map_trade(self, label: str | None) -> str | None:
        known, trade = self.trade_map.lookup(self.source_id, label)
        if not known and label:
            self.unmapped_classes[label] = self.unmapped_classes.get(label, 0) + 1
        return trade

    def in_market(self, county: str | None, zip_code: str | None) -> bool:
        if county:
            return self.market.market.county_in_market(county)
        return self.market.market.zip_in_market(zip_code)

    async def probe(self) -> dict[str, Any]:
        """Fetch a small sample and report columns — used to verify field mappings."""
        raise NotImplementedError


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# ---- tabular helpers -----------------------------------------------------------


def norm_header(h: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", h.strip().lower())).strip("_")


class FieldMap:
    """Canonical field → list of accepted column names (normalised).

    Official files rename columns now and then; an alias list keeps parsing working
    and `report()` shows exactly which column fed each field.
    """

    def __init__(self, aliases: dict[str, list[str]]):
        self.aliases = {k: [norm_header(a) for a in v] for k, v in aliases.items()}

    def normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {norm_header(k): v for k, v in row.items() if k is not None}

    def get(self, row: dict[str, Any], field: str) -> str | None:
        for alias in self.aliases.get(field, []):
            value = row.get(alias)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    def report(self, columns: Iterable[str]) -> dict[str, str | None]:
        cols = {norm_header(c) for c in columns}
        return {f: next((a for a in al if a in cols), None) for f, al in self.aliases.items()}
