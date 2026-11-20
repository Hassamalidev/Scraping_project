"""Load a ParsedPro into the business tables, with provenance on every row.

Licence numbers are the first dedupe rule (spec 6.4 rule 1): a record whose
(state, agency, licence number) already exists updates that business instead of
creating a new one. The other dedupe rules arrive in milestone M4.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from prodata.connectors.base import ParsedPro
from prodata.models import Business, BusinessSource, BusinessTrade, Email, License, Phone, utcnow
from prodata.pipeline.normalize import display_case, normalize_name, normalize_phone, standardize_street

ROLE_PREFIXES = {"info", "office", "service", "sales", "contact", "admin", "support", "hello", "team", "help", "dispatch", "billing", "estimates", "jobs"}

BUSINESS_FIELDS = ("address_line1", "address_line2", "city", "state", "zip", "county")


def email_kind(address: str) -> str:
    return "role" if address.split("@", 1)[0].lower() in ROLE_PREFIXES else "named"


def load_parsed(session: Session, pro: ParsedPro, raw_record_id: int) -> tuple[Business, bool]:
    """Returns (business, created)."""
    created = False
    business: Business | None = None
    lic: License | None = None

    if pro.license:
        lic = session.scalar(
            select(License).where(
                License.state == pro.license.state,
                License.agency == pro.license.agency,
                License.license_number == pro.license.license_number,
            )
        )
        if lic:
            business = lic.business

    values = {
        "legal_name": display_case(pro.legal_name),
        "dba_name": display_case(pro.dba_name),
        "normalized_name": normalize_name(pro.dba_name or pro.legal_name),
        "entity_type": pro.entity_type,
        "address_line1": standardize_street(display_case(pro.address_line1)),
        "address_line2": display_case(pro.address_line2),
        "city": display_case(pro.city),
        "state": pro.state,
        "zip": pro.zip,
        "county": display_case(pro.county),
        "website": pro.website,
    }

    if business is None:
        business = Business(**values, status="unknown")
        session.add(business)
        session.flush()
        created = True
        method = "new"
    else:
        # The licence record is the authoritative source for identity and address.
        for key, value in values.items():
            if value is not None and getattr(business, key) != value:
                setattr(business, key, value)
        business.updated_at = utcnow()
        method = "license_number"

    if pro.license:
        fields = pro.license.model_dump()
        if lic is None:
            lic = License(business_id=business.id, raw_record_id=raw_record_id, **fields)
            session.add(lic)
        else:
            for key, value in fields.items():
                setattr(lic, key, value)
            lic.raw_record_id = raw_record_id

    if not session.get(BusinessSource, (business.id, raw_record_id)):
        session.add(
            BusinessSource(
                business_id=business.id,
                raw_record_id=raw_record_id,
                match_method=method,
                match_confidence=1.0,
            )
        )

    for trade in pro.trades:
        if not session.get(BusinessTrade, (business.id, trade, "license")):
            session.add(BusinessTrade(business_id=business.id, trade=trade, evidence="license", raw_record_id=raw_record_id))

    for parsed_phone in pro.phones:
        e164 = normalize_phone(parsed_phone.raw)
        if not e164:
            continue
        existing = session.scalar(select(Phone).where(Phone.business_id == business.id, Phone.e164 == e164))
        if existing:
            existing.raw_record_id = raw_record_id
            continue
        session.add(
            Phone(
                business_id=business.id,
                e164=e164,
                raw_value=parsed_phone.raw,
                label=parsed_phone.label,
                published_as_business=parsed_phone.evidence is not None,
                published_as_business_evidence=parsed_phone.evidence,
                dnc_status="unknown",
                call_ok=False,  # set only by the compliance step
                raw_record_id=raw_record_id,
            )
        )

    for address in pro.emails:
        address = address.strip().lower()
        if session.scalar(select(Email).where(Email.business_id == business.id, Email.email == address)):
            continue
        session.add(
            Email(
                business_id=business.id,
                email=address,
                kind=email_kind(address),
                email_ok=False,  # set only after verification
                raw_record_id=raw_record_id,
            )
        )

    session.flush()
    return business, created
