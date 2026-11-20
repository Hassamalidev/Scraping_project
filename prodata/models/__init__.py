"""SQLAlchemy 2 models — the schema in spec section 5.

Rules baked in here:
- `raw_records` is append-only and unique on (source_id, external_id, content_hash).
- Every derived row (licence, phone, email, trade, service area) points at the raw
  record it came from (`raw_record_id`) — provenance on everything.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    tier: Mapped[int] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text)
    terms_notes: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(40))


class RawRecord(Base):
    __tablename__ = "raw_records"
    __table_args__ = (UniqueConstraint("source_id", "external_id", "content_hash", name="uq_raw_record_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(80), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)
    # identity
    legal_name: Mapped[str] = mapped_column(Text)
    dba_name: Mapped[str | None] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    website: Mapped[str | None] = mapped_column(Text)
    website_status: Mapped[str | None] = mapped_column(String(40))
    # location
    address_line1: Mapped[str | None] = mapped_column(Text)
    address_line2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(120), index=True)
    state: Mapped[str | None] = mapped_column(String(2))
    zip: Mapped[str | None] = mapped_column(String(10))
    county: Mapped[str | None] = mapped_column(String(80))
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    distance_to_metro_center_mi: Mapped[float | None] = mapped_column(Float)
    # details
    service_radius_miles: Mapped[float | None] = mapped_column(Float)
    years_in_business: Mapped[int | None] = mapped_column(Integer)
    emergency_service: Mapped[bool | None] = mapped_column(Boolean)
    languages: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # scoring
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # active|inactive|unknown
    quality_score: Mapped[int | None] = mapped_column(Integer)
    score_breakdown: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    # set when the worker signed up through the public "Join" form
    signed_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contact_name: Mapped[str | None] = mapped_column(Text)  # the person to ask for

    licenses: Mapped[list[License]] = relationship(back_populates="business")
    phones: Mapped[list[Phone]] = relationship(back_populates="business")
    emails: Mapped[list[Email]] = relationship(back_populates="business")
    trades: Mapped[list[BusinessTrade]] = relationship(back_populates="business")
    sources: Mapped[list[BusinessSource]] = relationship(back_populates="business")
    outreach: Mapped[list[OutreachStatus]] = relationship(back_populates="business")


class BusinessSource(Base):
    __tablename__ = "business_sources"

    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("raw_records.id"), primary_key=True)
    match_method: Mapped[str] = mapped_column(String(40))
    match_confidence: Mapped[float] = mapped_column(Float)

    business: Mapped[Business] = relationship(back_populates="sources")
    raw_record: Mapped[RawRecord] = relationship()


class License(Base):
    __tablename__ = "licenses"
    __table_args__ = (UniqueConstraint("state", "agency", "license_number", name="uq_license"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    state: Mapped[str] = mapped_column(String(2))
    agency: Mapped[str] = mapped_column(String(40))
    license_number: Mapped[str] = mapped_column(String(80))
    license_type: Mapped[str | None] = mapped_column(Text)
    trade: Mapped[str | None] = mapped_column(String(40))
    classification: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(40))
    issued_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    qualifier_name: Mapped[str | None] = mapped_column(Text)
    bond_info: Mapped[str | None] = mapped_column(Text)
    insurance_info: Mapped[str | None] = mapped_column(Text)
    workers_comp: Mapped[str | None] = mapped_column(Text)
    disciplinary_actions: Mapped[list | None] = mapped_column(JSONB)
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("raw_records.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped[Business] = relationship(back_populates="licenses")
    raw_record: Mapped[RawRecord] = relationship()


class BusinessTrade(Base):
    __tablename__ = "business_trades"

    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    trade: Mapped[str] = mapped_column(String(40), primary_key=True)
    evidence: Mapped[str] = mapped_column(String(20), primary_key=True)  # license|website|osm|manual
    raw_record_id: Mapped[int | None] = mapped_column(ForeignKey("raw_records.id"))

    business: Mapped[Business] = relationship(back_populates="trades")


class ServiceArea(Base):
    __tablename__ = "service_areas"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    city: Mapped[str | None] = mapped_column(String(120))
    zip: Mapped[str | None] = mapped_column(String(10))
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("raw_records.id"))


class Phone(Base):
    __tablename__ = "phones"
    __table_args__ = (UniqueConstraint("business_id", "e164", name="uq_phone_business"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    e164: Mapped[str] = mapped_column(String(20), index=True)
    raw_value: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(20), default="unknown")  # office|mobile|fax|unknown
    line_type: Mapped[str | None] = mapped_column(String(20))
    carrier: Mapped[str | None] = mapped_column(Text)
    published_as_business: Mapped[bool] = mapped_column(Boolean, default=False)
    published_as_business_evidence: Mapped[str | None] = mapped_column(Text)
    dnc_status: Mapped[str] = mapped_column(String(20), default="unknown")  # unknown|clear|listed
    dnc_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    call_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    # the owner of this number asked to be called (sign-up form, or contacted us directly)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("raw_records.id"))

    business: Mapped[Business] = relationship(back_populates="phones")
    raw_record: Mapped[RawRecord] = relationship()


class Email(Base):
    __tablename__ = "emails"
    __table_args__ = (UniqueConstraint("business_id", "email", name="uq_email_business"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="named")  # role|named
    domain_matches_website: Mapped[bool | None] = mapped_column(Boolean)
    mx_valid: Mapped[bool | None] = mapped_column(Boolean)
    verification_status: Mapped[str | None] = mapped_column(String(40))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("raw_records.id"))

    business: Mapped[Business] = relationship(back_populates="emails")
    raw_record: Mapped[RawRecord] = relationship()


class DncNumber(Base):
    __tablename__ = "dnc_numbers"

    e164: Mapped[str] = mapped_column(String(20), primary_key=True)
    area_code: Mapped[str] = mapped_column(String(3), index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DncImport(Base):
    """One row per area-code file import — drives the 31-day freshness rule."""

    __tablename__ = "dnc_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    area_code: Mapped[str] = mapped_column(String(3), index=True)
    file_name: Mapped[str] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Suppression(Base):
    __tablename__ = "suppression_list"
    __table_args__ = (UniqueConstraint("value", "value_type", name="uq_suppression_value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(20))  # phone|email|domain|business_id
    reason: Mapped[str] = mapped_column(String(20))  # opt_out|dnc_request|bounce|complaint|manual
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)


class OutreachStatus(Base):
    __tablename__ = "outreach_status"

    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    channel: Mapped[str] = mapped_column(String(10), primary_key=True)  # email|phone
    status: Mapped[str] = mapped_column(String(20), default="not_contacted")
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    business: Mapped[Business] = relationship(back_populates="outreach")


class JobRequest(Base):
    """Someone who wants work done. Only ever created by that person (public request
    form, with recorded consent) or by the operator after that person contacted them.
    Never scraped."""

    __tablename__ = "job_requests"
    __table_args__ = (CheckConstraint("consent_at IS NOT NULL", name="ck_job_request_consent"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    # who and what
    name: Mapped[str] = mapped_column(Text)
    phone_e164: Mapped[str] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(320))
    trade: Mapped[str] = mapped_column(String(40), index=True)
    details: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(120))
    zip: Mapped[str | None] = mapped_column(String(10))
    timing: Mapped[str | None] = mapped_column(String(40))
    budget: Mapped[str | None] = mapped_column(Text)
    # how we got it, and the consent record
    origin: Mapped[str] = mapped_column(String(20))  # request_form|contacted_me
    consent_text: Mapped[str] = mapped_column(Text)
    consent_version: Mapped[str] = mapped_column(String(20))
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consent_ip: Mapped[str | None] = mapped_column(String(64))
    consent_user_agent: Mapped[str | None] = mapped_column(Text)
    consent_page_url: Mapped[str | None] = mapped_column(Text)
    # working it
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    last_called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"))
    # the deal: recorded only, money moves outside the app
    client_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    worker_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    payment_method: Mapped[str | None] = mapped_column(String(20))
    payment_status: Mapped[str] = mapped_column(String(20), default="unpaid")  # unpaid|deposit|paid
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    worker: Mapped[Business | None] = relationship()

    @property
    def my_cut(self) -> Decimal | None:
        if self.client_price is None or self.worker_price is None:
            return None
        return self.client_price - self.worker_price


class MergeLog(Base):
    __tablename__ = "merge_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    kept_business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    merged_business_id: Mapped[int] = mapped_column(Integer)
    rule: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|ok|failed
    counts: Mapped[dict | None] = mapped_column(JSONB)
    errors: Mapped[list | None] = mapped_column(JSONB)


OUTREACH_STATUSES = [
    "not_contacted",
    "contacted",
    "replied",
    "interested",
    "signed_up",
    "declined",
    "do_not_contact",
]
SUPPRESSION_TYPES = ["phone", "email", "domain", "business_id"]
SUPPRESSION_REASONS = ["opt_out", "dnc_request", "bounce", "complaint", "manual"]
CLIENT_STATUSES = ["new", "called", "matched", "done", "lost", "do_not_contact"]
PAYMENT_METHODS = ["cash", "bank_transfer", "zelle", "venmo", "cash_app", "paypal", "check", "other"]
PAYMENT_STATUSES = ["unpaid", "deposit", "paid"]
