"""Compliance flags, suppression list and DNC import (spec 6.7).

call_ok  = number valid AND not suppressed
           AND (the owner asked us to call — sign-up/request form or contacted us directly
                OR (dnc_status == clear OR published as a business line with evidence)
                   AND the DNC data for that area code is ≤ 31 days old).
email_ok = not suppressed AND not a hard bounce AND (MX valid OR API says deliverable).
sms_ok   = always False in Phase 1.

When in doubt, the answer is False. The suppression list overrides everything and
is checked again at export time.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from prodata.models import (
    SUPPRESSION_REASONS,
    SUPPRESSION_TYPES,
    Business,
    DncImport,
    DncNumber,
    Email,
    OutreachStatus,
    Phone,
    Suppression,
    utcnow,
)
from prodata.pipeline.normalize import normalize_phone

SMS_OK = False  # Phase 1: never.
HARD_BOUNCE = {"invalid", "bounce", "hard_bounce", "undeliverable", "do_not_mail", "spamtrap", "abuse"}
DELIVERABLE = {"valid", "deliverable"}


# ---- suppression ---------------------------------------------------------------


def normalize_suppression_value(value: str, value_type: str) -> str:
    value = value.strip()
    if value_type == "phone":
        e164 = normalize_phone(value)
        if not e164:
            raise ValueError(f"Not a valid phone number: {value}")
        return e164
    if value_type == "email":
        if "@" not in value:
            raise ValueError(f"Not an email address: {value}")
        return value.lower()
    if value_type == "domain":
        return value.lower().removeprefix("http://").removeprefix("https://").removeprefix("www.").split("/")[0]
    if value_type == "business_id":
        if not value.isdigit():
            raise ValueError("business_id must be a number")
        return value
    raise ValueError(f"Unknown suppression type {value_type}; use one of {SUPPRESSION_TYPES}")


def add_suppression(session: Session, value: str, value_type: str, reason: str, note: str | None = None) -> Suppression:
    if reason not in SUPPRESSION_REASONS:
        raise ValueError(f"Unknown reason {reason}; use one of {SUPPRESSION_REASONS}")
    value = normalize_suppression_value(value, value_type)
    existing = session.scalar(select(Suppression).where(Suppression.value == value, Suppression.value_type == value_type))
    if existing:
        return existing
    row = Suppression(value=value, value_type=value_type, reason=reason, note=note)
    session.add(row)
    session.flush()
    # Apply immediately, not just at the next flag refresh.
    refresh_flags(session)
    return row


@dataclass
class SuppressionIndex:
    phones: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    business_ids: set[int] = field(default_factory=set)

    @classmethod
    def load(cls, session: Session) -> SuppressionIndex:
        idx = cls()
        for value, value_type in session.execute(select(Suppression.value, Suppression.value_type)):
            if value_type == "phone":
                idx.phones.add(value)
            elif value_type == "email":
                idx.emails.add(value)
            elif value_type == "domain":
                idx.domains.add(value)
            elif value_type == "business_id":
                idx.business_ids.add(int(value))
        # A business marked do_not_contact in outreach is suppressed too.
        dnc_biz = session.scalars(
            select(OutreachStatus.business_id).where(OutreachStatus.status == "do_not_contact")
        )
        idx.business_ids.update(dnc_biz)
        return idx

    def email_suppressed(self, email: str, business_id: int | None = None) -> bool:
        email = email.lower()
        domain = email.rsplit("@", 1)[-1]
        return email in self.emails or domain in self.domains or (business_id in self.business_ids)

    def phone_suppressed(self, e164: str, business_id: int | None = None) -> bool:
        return e164 in self.phones or (business_id in self.business_ids)


# ---- decisions (pure functions, fully tested) -----------------------------------


@dataclass
class Decision:
    ok: bool
    reason: str


def call_decision(
    *,
    e164: str | None,
    suppressed: bool,
    dnc_status: str,
    published_as_business: bool,
    business_evidence: str | None,
    dnc_imported_at: datetime | None,
    now: datetime | None = None,
    max_age_days: int = 31,
    consented: bool = False,
) -> Decision:
    now = now or datetime.now(timezone.utc)
    if not e164 or not normalize_phone(e164):
        return Decision(False, "invalid number")
    if suppressed:
        return Decision(False, "on suppression list")
    if consented:
        return Decision(True, "asked to be called")
    if dnc_imported_at is None:
        return Decision(False, "no DNC data for this area code")
    if now - dnc_imported_at > timedelta(days=max_age_days):
        return Decision(False, f"DNC data older than {max_age_days} days")
    if dnc_status == "clear":
        return Decision(True, "not on DNC registry")
    if published_as_business and business_evidence:
        return Decision(True, f"published business line ({business_evidence})")
    if dnc_status == "listed":
        return Decision(False, "on National DNC Registry")
    return Decision(False, "DNC status unknown")


def email_decision(
    *,
    email: str,
    suppressed: bool,
    mx_valid: bool | None,
    verification_status: str | None,
) -> Decision:
    if suppressed:
        return Decision(False, "on suppression list")
    status = (verification_status or "").lower()
    if status in HARD_BOUNCE:
        return Decision(False, f"verification: {status}")
    if status in DELIVERABLE:
        return Decision(True, "verified deliverable")
    if mx_valid:
        return Decision(True, "domain accepts mail (MX)")
    if mx_valid is None:
        return Decision(False, "not verified yet")
    return Decision(False, "no MX record")


# ---- DNC ---------------------------------------------------------------------------


def dnc_import_times(session: Session) -> dict[str, datetime]:
    rows = session.execute(select(DncImport.area_code, func.max(DncImport.imported_at)).group_by(DncImport.area_code))
    return {area: ts for area, ts in rows}


def import_dnc_file(session: Session, path: Path, batch_size: int = 10000) -> dict[str, int]:
    """Load a National DNC Registry area-code file (one number per line).

    Each line's digits are read; 10-digit (or 11 starting with 1) numbers are kept.
    """
    now = utcnow()
    per_area: dict[str, int] = defaultdict(int)
    batch: list[dict] = []
    total = 0

    def flush():
        if not batch:
            return
        stmt = insert(DncNumber).values(batch)
        stmt = stmt.on_conflict_do_update(index_elements=["e164"], set_={"imported_at": stmt.excluded.imported_at})
        session.execute(stmt)
        batch.clear()

    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            digits = re.sub(r"\D", "", line)
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) != 10:
                continue
            area = digits[:3]
            batch.append({"e164": f"+1{digits}", "area_code": area, "imported_at": now})
            per_area[area] += 1
            total += 1
            if len(batch) >= batch_size:
                flush()
    flush()
    for area, count in per_area.items():
        session.add(DncImport(area_code=area, file_name=path.name, row_count=count, imported_at=now))
    session.flush()
    refresh_flags(session)
    return {"numbers": total, "area_codes": len(per_area)}


# ---- refresh ----------------------------------------------------------------------


def refresh_flags(session: Session, max_age_days: int = 31) -> dict[str, int]:
    """Recompute dnc_status, call_ok and email_ok for every phone and email."""
    supp = SuppressionIndex.load(session)
    imports = dnc_import_times(session)
    now = datetime.now(timezone.utc)
    counts = defaultdict(int)

    listed = set()
    area_codes = list(imports)
    if area_codes:
        listed = set(session.scalars(select(DncNumber.e164).where(DncNumber.area_code.in_(area_codes))))

    for phone in session.scalars(select(Phone)):
        area = phone.e164[2:5] if phone.e164.startswith("+1") else None
        imported_at = imports.get(area) if area else None
        if imported_at:
            phone.dnc_status = "listed" if phone.e164 in listed else "clear"
            phone.dnc_checked_at = imported_at
        else:
            phone.dnc_status = "unknown"
        decision = call_decision(
            e164=phone.e164,
            suppressed=supp.phone_suppressed(phone.e164, phone.business_id),
            dnc_status=phone.dnc_status,
            published_as_business=phone.published_as_business,
            business_evidence=phone.published_as_business_evidence,
            dnc_imported_at=imported_at,
            now=now,
            max_age_days=max_age_days,
            consented=phone.consent_at is not None,
        )
        phone.call_ok = decision.ok
        counts["call_ok" if decision.ok else "call_blocked"] += 1

    for email in session.scalars(select(Email)):
        decision = email_decision(
            email=email.email,
            suppressed=supp.email_suppressed(email.email, email.business_id),
            mx_valid=email.mx_valid,
            verification_status=email.verification_status,
        )
        email.email_ok = decision.ok
        counts["email_ok" if decision.ok else "email_blocked"] += 1

    session.flush()
    return dict(counts)


def phone_decision_for(phone: Phone, supp: SuppressionIndex, imports: dict[str, datetime], max_age_days: int = 31) -> Decision:
    area = phone.e164[2:5] if phone.e164.startswith("+1") else None
    return call_decision(
        e164=phone.e164,
        suppressed=supp.phone_suppressed(phone.e164, phone.business_id),
        dnc_status=phone.dnc_status,
        published_as_business=phone.published_as_business,
        business_evidence=phone.published_as_business_evidence,
        dnc_imported_at=imports.get(area) if area else None,
        max_age_days=max_age_days,
        consented=phone.consent_at is not None,
    )


def email_decision_for(email: Email, supp: SuppressionIndex) -> Decision:
    return email_decision(
        email=email.email,
        suppressed=supp.email_suppressed(email.email, email.business_id),
        mx_valid=email.mx_valid,
        verification_status=email.verification_status,
    )


def business_suppressed(business: Business, supp: SuppressionIndex) -> bool:
    if business.id in supp.business_ids:
        return True
    if business.website:
        domain = normalize_suppression_value(business.website, "domain")
        if domain in supp.domains:
            return True
    return False
