"""Pros list queries and CSV exports (spec 6.9).

Exclusion rules applied to every export, no matter who calls it:
- businesses on the suppression list (by id or website domain) or marked
  do_not_contact are dropped entirely;
- suppressed phone numbers / emails are never written;
- call_ok / email_ok are recomputed at export time (suppression re-checked).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from prodata.models import Business, BusinessSource, BusinessTrade, Email, License, OutreachStatus, Phone, RawRecord
from prodata.pipeline.compliance import (
    SuppressionIndex,
    business_suppressed,
    dnc_import_times,
    email_decision_for,
    phone_decision_for,
)

PROS_COLUMNS = [
    "business_id", "business_name", "dba", "trades", "license_number", "license_status", "license_expires_on",
    "address", "city", "zip", "distance_mi",
    "website", "email", "email_ok", "phone_e164", "phone_label", "line_type", "call_ok",
    "quality_score", "sources", "last_verified_at",
]
EMAIL_TOOL_COLUMNS = ["business_name", "email", "city", "trade", "personalization"]


@dataclass
class ProFilters:
    trade: str | None = None
    q: str | None = None
    city: str | None = None
    county: str | None = None
    min_score: int | None = None
    has_phone: bool = False
    call_ok_only: bool = False
    outreach: str | None = None


def pros_query(f: ProFilters) -> Select:
    stmt = select(Business)
    if f.trade:
        stmt = stmt.where(exists().where(and_(BusinessTrade.business_id == Business.id, BusinessTrade.trade == f.trade)))
    if f.q:
        like = f"%{f.q.strip()}%"
        lic_match = exists().where(and_(License.business_id == Business.id, License.license_number.ilike(like)))
        phone_digits = "".join(ch for ch in f.q if ch.isdigit())
        conds = [Business.legal_name.ilike(like), Business.dba_name.ilike(like), lic_match]
        if len(phone_digits) >= 4:
            conds.append(exists().where(and_(Phone.business_id == Business.id, Phone.e164.like(f"%{phone_digits}%"))))
        stmt = stmt.where(or_(*conds))
    if f.city:
        stmt = stmt.where(func.lower(Business.city) == f.city.strip().lower())
    if f.county:
        stmt = stmt.where(func.lower(Business.county) == f.county.strip().lower())
    if f.min_score is not None:
        stmt = stmt.where(Business.quality_score >= f.min_score)
    if f.has_phone:
        stmt = stmt.where(exists().where(Phone.business_id == Business.id))
    if f.call_ok_only:
        stmt = stmt.where(exists().where(and_(Phone.business_id == Business.id, Phone.call_ok.is_(True))))
    if f.outreach:
        if f.outreach == "not_contacted":
            stmt = stmt.where(
                ~exists().where(and_(OutreachStatus.business_id == Business.id, OutreachStatus.status != "not_contacted"))
            )
        else:
            stmt = stmt.where(exists().where(and_(OutreachStatus.business_id == Business.id, OutreachStatus.status == f.outreach)))
    return stmt


def _with_children(stmt: Select) -> Select:
    return stmt.options(
        selectinload(Business.licenses),
        selectinload(Business.phones),
        selectinload(Business.emails),
        selectinload(Business.trades),
        selectinload(Business.sources).selectinload(BusinessSource.raw_record),
        selectinload(Business.outreach),
    )


def export_rows(session: Session, f: ProFilters, max_age_days: int = 31) -> list[dict]:
    supp = SuppressionIndex.load(session)
    imports = dnc_import_times(session)
    stmt = _with_children(pros_query(f)).order_by(Business.quality_score.desc().nulls_last(), Business.legal_name)
    rows = []
    for biz in session.scalars(stmt):
        if business_suppressed(biz, supp):
            continue
        phones = [(p, phone_decision_for(p, supp, imports, max_age_days)) for p in biz.phones]
        phones = [(p, d) for p, d in phones if not supp.phone_suppressed(p.e164, biz.id)]
        phones.sort(key=lambda pd: (not pd[1].ok, pd[0].label != "office"))
        emails = [(e, email_decision_for(e, supp)) for e in biz.emails]
        emails = [(e, d) for e, d in emails if not supp.email_suppressed(e.email, biz.id)]
        emails.sort(key=lambda ed: (not ed[1].ok, ed[0].kind != "role"))

        lic = _best_license(biz.licenses, f.trade)
        phone, phone_d = phones[0] if phones else (None, None)
        email, email_d = emails[0] if emails else (None, None)
        sources = sorted({s.raw_record.source_id for s in biz.sources})
        verified = max((l.verified_at for l in biz.licenses if l.verified_at), default=None)

        rows.append(
            {
                "business_id": biz.id,
                "business_name": biz.legal_name,
                "dba": biz.dba_name or "",
                "trades": ";".join(sorted({t.trade for t in biz.trades})),
                "license_number": lic.license_number if lic else "",
                "license_status": (lic.status or "") if lic else "",
                "license_expires_on": lic.expires_on.isoformat() if lic and lic.expires_on else "",
                "address": " ".join(x for x in (biz.address_line1, biz.address_line2) if x),
                "city": biz.city or "",
                "zip": biz.zip or "",
                "distance_mi": f"{biz.distance_to_metro_center_mi:.1f}" if biz.distance_to_metro_center_mi is not None else "",
                "website": biz.website or "",
                "email": email.email if email else "",
                "email_ok": bool(email_d and email_d.ok),
                "phone_e164": phone.e164 if phone else "",
                "phone_label": phone.label if phone else "",
                "line_type": (phone.line_type or "") if phone else "",
                "call_ok": bool(phone_d and phone_d.ok),
                "quality_score": biz.quality_score if biz.quality_score is not None else "",
                "sources": ";".join(sources),
                "last_verified_at": verified.isoformat() if verified else "",
                "_call_reason": phone_d.reason if phone_d else "no phone",
                "_trade_for_tool": f.trade or (sorted({t.trade for t in biz.trades}) or [""])[0],
            }
        )
    return rows


def _best_license(licenses: list[License], trade: str | None) -> License | None:
    if not licenses:
        return None
    pool = [l for l in licenses if not trade or l.trade == trade] or licenses
    return sorted(pool, key=lambda l: (l.expires_on or date.min), reverse=True)[0]


def email_tool_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if not r["email"] or not r["email_ok"]:
            continue
        trade = r["_trade_for_tool"].replace("_", " ")
        snippet = f"licensed {trade} business in {r['city']}" if r["license_number"] else f"{trade} business in {r['city']}"
        out.append({"business_name": r["business_name"], "email": r["email"], "city": r["city"], "trade": trade, "personalization": snippet})
    return out


def to_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in r.items()})
    return buf.getvalue()


def write_export(session: Session, market_id: str, f: ProFilters, fmt: str, exports_dir: Path) -> tuple[Path, int]:
    rows = export_rows(session, f)
    if fmt == "email-tool":
        rows, columns, prefix = email_tool_rows(rows), EMAIL_TOOL_COLUMNS, "email_tool"
    else:
        columns, prefix = PROS_COLUMNS, "pros"
    folder = exports_dir / market_id / (f.trade or "all")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{prefix}_{date.today().isoformat()}.csv"
    path.write_text(to_csv(rows, columns), encoding="utf-8", newline="")
    return path, len(rows)


# ---- stats -------------------------------------------------------------------------


def stats(session: Session) -> dict:
    total = session.scalar(select(func.count(Business.id))) or 0
    by_trade = dict(
        session.execute(
            select(BusinessTrade.trade, func.count(func.distinct(BusinessTrade.business_id))).group_by(BusinessTrade.trade)
        ).all()
    )
    licensed = session.scalar(select(func.count(func.distinct(License.business_id)))) or 0
    with_phone = session.scalar(select(func.count(func.distinct(Phone.business_id)))) or 0
    call_ok = session.scalar(select(func.count(func.distinct(Phone.business_id))).where(Phone.call_ok.is_(True))) or 0
    email_ok = session.scalar(select(func.count(func.distinct(Email.business_id))).where(Email.email_ok.is_(True))) or 0
    coverage = dict(
        session.execute(
            select(RawRecord.source_id, func.count(func.distinct(BusinessSource.business_id)))
            .join(BusinessSource, BusinessSource.raw_record_id == RawRecord.id)
            .group_by(RawRecord.source_id)
        ).all()
    )

    def pct(n):
        return round(100 * n / total, 1) if total else 0.0

    return {
        "businesses": total,
        "by_trade": by_trade,
        "licensed": licensed,
        "licensed_pct": pct(licensed),
        "with_phone": with_phone,
        "with_phone_pct": pct(with_phone),
        "call_ok": call_ok,
        "call_ok_pct": pct(call_ok),
        "email_ok": email_ok,
        "email_ok_pct": pct(email_ok),
        "source_coverage": coverage,
    }
