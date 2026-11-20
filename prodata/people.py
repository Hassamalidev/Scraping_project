"""Workers and clients: sign-ups, job requests, matching and deals.

Two kinds of people live in the app:

- Workers are businesses. Some are found in public business data (licence files,
  OpenStreetMap); others sign up themselves on the public "Join" page.
- Clients are people who want work done. They only come in through the public
  "Request a pro" form, which records their consent, or are entered by the operator
  after the client contacted them. Clients are never scraped.

Money is only recorded here (price, worker pay, payment method, paid or not).
Payments themselves happen outside the app.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from prodata.connectors.base import ParsedPhone, ParsedPro, RawRecordIn
from prodata.models import (
    CLIENT_STATUSES,
    Business,
    BusinessTrade,
    JobRequest,
    OutreachStatus,
    Phone,
    Source,
    utcnow,
)
from prodata.pipeline.collect import store_raw
from prodata.pipeline.compliance import Decision, SuppressionIndex, dnc_import_times, phone_decision_for
from prodata.pipeline.load import load_parsed
from prodata.pipeline.normalize import clean_text, display_case, normalize_phone, normalize_zip

# trade id -> (worker label, "needs a ..." label)
TRADES: dict[str, tuple[str, str]] = {
    "plumbing": ("Plumber", "Plumbing"),
    "carpentry": ("Carpenter", "Carpentry"),
    "handyman": ("Handyman", "Handyman jobs"),
    "flooring_carpet": ("Flooring & carpet", "Flooring or carpet"),
    "painting": ("Painter", "Painting"),
    "roofing": ("Roofer", "Roofing"),
    "electrical": ("Electrician", "Electrical"),
    "hvac": ("HVAC / AC tech", "Heating or AC"),
}
TIMINGS = {
    "asap": "As soon as possible",
    "this_week": "This week",
    "this_month": "This month",
    "flexible": "I'm flexible",
}

CLIENT_CONSENT_VERSION = "client-v1"
CLIENT_CONSENT = (
    "I agree that {brand} may call me at the phone number above about this request "
    "and share my request with local pros who can do the job. I can ask them to stop at any time."
)
WORKER_CONSENT_VERSION = "worker-v1"
WORKER_CONSENT = (
    "I agree that {brand} may call me at the phone number above about jobs in my trade. "
    "I can ask them to stop at any time."
)
CONTACTED_ME_VERSION = "contacted-me"
CONTACTED_ME = "Entered by the operator: this person contacted us directly and asked to be called back."

SIGNUP_SOURCES = {
    "signup_form": ("Join form (workers signed up themselves)", 0),
    "added_by_me": ("Added by you", 0),
}


class FormError(ValueError):
    """User-facing validation problem."""


@dataclass
class ConsentMeta:
    ip: str | None = None
    user_agent: str | None = None
    page_url: str | None = None


PLURALS = {
    "plumbing": "Plumbers",
    "carpentry": "Carpenters",
    "handyman": "Handymen",
    "flooring_carpet": "Flooring & carpet pros",
    "painting": "Painters",
    "roofing": "Roofers",
    "electrical": "Electricians",
    "hvac": "HVAC / AC techs",
}


def worker_plural(trade: str | None) -> str:
    return PLURALS.get(trade or "", worker_label(trade) + "s")


def worker_label(trade: str | None) -> str:
    return TRADES.get(trade or "", (None,))[0] or (trade or "").replace("_", " ").title()


def need_label(trade: str | None) -> str:
    return TRADES.get(trade or "", (None, None))[1] or (trade or "").replace("_", " ").title()


def parse_money(value: str | None) -> Decimal | None:
    value = (value or "").strip().replace("$", "").replace(",", "")
    if not value:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise FormError(f"“{value}” is not an amount. Use numbers like 450 or 450.00.") from None
    if amount < 0:
        raise FormError("Amounts can't be negative.")
    return amount.quantize(Decimal("0.01"))


def _clean(value: str | None, limit: int = 2000, multiline: bool = False) -> str | None:
    if multiline:
        lines = (line.strip() for line in str(value or "").strip().splitlines())
        value = "\n".join(lines).strip() or None
    else:
        value = clean_text(value)
    return value[:limit] if value else None


def person_case(value: str | None) -> str | None:
    """'dana smith' / 'DANA SMITH' → 'Dana Smith'; mixed case is left as typed."""
    if not value:
        return value
    if value.islower() or value.isupper():
        return " ".join(w.capitalize() for w in value.split())
    return value


def _require_phone(raw: str | None) -> str:
    e164 = normalize_phone(raw or "")
    if not e164:
        raise FormError("Please enter a valid US phone number, e.g. (214) 555-0123.")
    return e164


def _ensure_signup_source(session: Session, source_id: str) -> None:
    if session.scalar(select(Source.id).where(Source.source_id == source_id)) is None:
        name, tier = SIGNUP_SOURCES[source_id]
        session.add(Source(source_id=source_id, name=name, tier=tier, url=None, terms_notes="Consent recorded per entry."))
        session.flush()


# ---- workers --------------------------------------------------------------------


def register_worker(
    session: Session,
    *,
    name: str,
    phone: str,
    trades: list[str],
    business_name: str | None = None,
    email: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    about: str | None = None,
    origin: str = "signup_form",
    consent_text: str,
    consent_version: str,
    meta: ConsentMeta | None = None,
    state: str | None = None,
) -> Business:
    """A worker who asked to hear about jobs. Links to an existing business with the same phone."""
    name = _clean(name, 200)
    if not name:
        raise FormError("Please enter your name.")
    e164 = _require_phone(phone)
    trades = [t for t in trades if t in TRADES]
    if not trades:
        raise FormError("Pick at least one kind of work you do.")
    email = (email or "").strip().lower() or None
    if email and "@" not in email:
        raise FormError("That email address doesn't look right.")
    meta = meta or ConsentMeta()
    now = utcnow()

    _ensure_signup_source(session, origin)
    payload = {
        "name": name,
        "business_name": _clean(business_name, 200),
        "phone": phone,
        "email": email,
        "trades": trades,
        "city": person_case(_clean(city, 120)),
        "zip": normalize_zip(zip_code),
        "about": _clean(about, multiline=True),
        "consent_text": consent_text,
        "consent_version": consent_version,
        "consent_at": now.isoformat(),
        "ip": meta.ip,
        "user_agent": meta.user_agent,
        "page_url": meta.page_url,
    }
    external_id = f"{origin}-{uuid.uuid4().hex[:12]}"
    raw_id = store_raw(
        session,
        RawRecordIn(source_id=origin, external_id=external_id, url=meta.page_url or "", payload=payload, fetched_at=now),
    )
    pro = ParsedPro(
        source_id=origin,
        external_id=external_id,
        legal_name=payload["business_name"] or name,
        city=payload["city"],
        state=state,
        zip=payload["zip"],
        phones=[ParsedPhone(raw=phone, label="mobile", evidence=None)],
        emails=[email] if email else [],
        trades=trades,
        trade_evidence="signup",
    )
    business, _ = load_parsed(session, pro, raw_id)
    business.signed_up_at = business.signed_up_at or now
    business.contact_name = business.contact_name or person_case(name)

    supp = SuppressionIndex.load(session)
    imports = dnc_import_times(session)
    for p in session.scalars(select(Phone).where(Phone.business_id == business.id, Phone.e164 == e164)):
        p.consent_at = p.consent_at or now
        p.call_ok = phone_decision_for(p, supp, imports).ok
    status = session.get(OutreachStatus, (business.id, "phone"))
    if status is None:
        session.add(OutreachStatus(business_id=business.id, channel="phone", status="interested"))
    elif status.status in ("not_contacted", "contacted", "replied", "declined"):
        status.status = "interested"
    session.flush()
    return business


INTERESTED = ("interested", "signed_up")


def workers_query(*, trade: str | None = None, q: str | None = None, city: str | None = None, show: str = "all"):
    stmt = select(Business)
    if trade:
        stmt = stmt.where(exists().where(and_(BusinessTrade.business_id == Business.id, BusinessTrade.trade == trade)))
    if q:
        like = f"%{q.strip()}%"
        conds = [Business.legal_name.ilike(like), Business.dba_name.ilike(like), Business.contact_name.ilike(like)]
        digits = "".join(ch for ch in q if ch.isdigit())
        if len(digits) >= 4:
            conds.append(exists().where(and_(Phone.business_id == Business.id, Phone.e164.like(f"%{digits}%"))))
        stmt = stmt.where(or_(*conds))
    if city:
        stmt = stmt.where(func.lower(Business.city) == city.strip().lower())
    interested = or_(
        Business.signed_up_at.is_not(None),
        exists().where(and_(OutreachStatus.business_id == Business.id, OutreachStatus.status.in_(INTERESTED))),
    )
    if show == "interested":
        stmt = stmt.where(interested)
    elif show == "not_called":
        stmt = stmt.where(
            Business.signed_up_at.is_(None),
            ~exists().where(and_(OutreachStatus.business_id == Business.id, OutreachStatus.status != "not_contacted")),
        )
    elif show == "callable":
        stmt = stmt.where(exists().where(and_(Phone.business_id == Business.id, Phone.call_ok.is_(True))))
    order = case((interested, 0), else_=1)
    return stmt.order_by(order, Business.signed_up_at.desc().nulls_last(), Business.quality_score.desc().nulls_last(), Business.legal_name)


def is_interested(biz: Business) -> bool:
    return biz.signed_up_at is not None or any(o.status in INTERESTED for o in biz.outreach)


def worker_counts(session: Session) -> dict:
    total = session.scalar(select(func.count(Business.id))) or 0
    interested = (
        session.scalar(
            select(func.count(Business.id)).where(
                or_(
                    Business.signed_up_at.is_not(None),
                    exists().where(and_(OutreachStatus.business_id == Business.id, OutreachStatus.status.in_(INTERESTED))),
                )
            )
        )
        or 0
    )
    by_trade = dict(
        session.execute(
            select(BusinessTrade.trade, func.count(func.distinct(BusinessTrade.business_id))).group_by(BusinessTrade.trade)
        ).all()
    )
    return {"total": total, "interested": interested, "by_trade": by_trade}


# ---- clients ----------------------------------------------------------------------


def create_job_request(
    session: Session,
    *,
    name: str,
    phone: str,
    trade: str,
    details: str | None = None,
    email: str | None = None,
    address: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    timing: str | None = None,
    budget: str | None = None,
    origin: str,
    consent_text: str,
    consent_version: str,
    meta: ConsentMeta | None = None,
) -> JobRequest:
    name = _clean(name, 200)
    if not name:
        raise FormError("Please enter your name.")
    e164 = _require_phone(phone)
    if trade not in TRADES:
        raise FormError("Pick the kind of work you need.")
    email = (email or "").strip().lower() or None
    if email and "@" not in email:
        raise FormError("That email address doesn't look right.")
    if origin not in ("request_form", "contacted_me"):
        raise ValueError(f"unknown origin {origin}")
    meta = meta or ConsentMeta()
    job = JobRequest(
        name=person_case(name),
        phone_e164=e164,
        email=email,
        trade=trade,
        details=_clean(details, multiline=True),
        address=_clean(address, 300),
        city=person_case(_clean(city, 120)),
        zip=normalize_zip(zip_code),
        timing=timing if timing in TIMINGS else None,
        budget=_clean(budget, 200),
        origin=origin,
        consent_text=consent_text,
        consent_version=consent_version,
        consent_at=utcnow(),
        consent_ip=meta.ip,
        consent_user_agent=(meta.user_agent or "")[:500] or None,
        consent_page_url=meta.page_url,
        status="new",
        payment_status="unpaid",
    )
    session.add(job)
    session.flush()
    return job


def client_call_decision(job: JobRequest, supp: SuppressionIndex) -> Decision:
    if job.status == "do_not_contact" or job.phone_e164 in supp.phones:
        return Decision(False, "asked not to be contacted")
    if job.origin == "request_form":
        return Decision(True, "asked to be called (request form)")
    return Decision(True, "contacted you directly")


def clients_query(*, trade: str | None = None, status: str | None = None, q: str | None = None):
    stmt = select(JobRequest).options(selectinload(JobRequest.worker))
    if trade:
        stmt = stmt.where(JobRequest.trade == trade)
    if status == "open":
        stmt = stmt.where(JobRequest.status.in_(("new", "called", "matched")))
    elif status in CLIENT_STATUSES:
        stmt = stmt.where(JobRequest.status == status)
    if q:
        like = f"%{q.strip()}%"
        conds = [JobRequest.name.ilike(like), JobRequest.city.ilike(like), JobRequest.details.ilike(like)]
        digits = "".join(ch for ch in q if ch.isdigit())
        if len(digits) >= 4:
            conds.append(JobRequest.phone_e164.like(f"%{digits}%"))
        stmt = stmt.where(or_(*conds))
    order = case((JobRequest.status == "new", 0), (JobRequest.status.in_(("called", "matched")), 1), else_=2)
    return stmt.order_by(order, JobRequest.created_at.desc())


def client_counts(session: Session) -> dict:
    by_status = dict(session.execute(select(JobRequest.status, func.count()).group_by(JobRequest.status)).all())
    open_by_trade = dict(
        session.execute(
            select(JobRequest.trade, func.count())
            .where(JobRequest.status.in_(("new", "called", "matched")))
            .group_by(JobRequest.trade)
        ).all()
    )
    return {
        "total": sum(by_status.values()),
        "new": by_status.get("new", 0),
        "open": sum(by_status.get(s, 0) for s in ("new", "called", "matched")),
        "by_status": by_status,
        "open_by_trade": open_by_trade,
    }


def suggest_workers(session: Session, job: JobRequest, limit: int = 8) -> list[Business]:
    """Workers for this job's trade: interested first, then same city, then the rest."""
    stmt = (
        workers_query(trade=job.trade)
        .options(selectinload(Business.phones), selectinload(Business.outreach), selectinload(Business.trades))
        .limit(200)
    )
    candidates = list(session.scalars(stmt))
    supp = SuppressionIndex.load(session)
    candidates = [b for b in candidates if b.id not in supp.business_ids]
    city = (job.city or "").lower()
    candidates.sort(key=lambda b: (not is_interested(b), (b.city or "").lower() != city, not any(p.call_ok for p in b.phones)))
    return candidates[:limit]


# ---- deals ------------------------------------------------------------------------


def deal_totals(session: Session, since: datetime | None = None) -> dict:
    stmt = select(JobRequest).where(JobRequest.client_price.is_not(None))
    if since:
        stmt = stmt.where(JobRequest.created_at >= since)
    agreed = cut = collected = owed = Decimal("0")
    count = 0
    for job in session.scalars(stmt):
        count += 1
        agreed += job.client_price or 0
        if job.my_cut is not None:
            cut += job.my_cut
            if job.payment_status == "paid":
                collected += job.my_cut
            else:
                owed += job.my_cut
    return {"count": count, "agreed": agreed, "cut": cut, "collected": collected, "owed": owed}


def month_start(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
