"""Tradebook web app.

Two sides:
- Workers: pros found in public business data, plus workers who signed up on /join.
- Clients: people who asked for work on /request (consent recorded), or who contacted
  you and you entered them yourself.
Deals record the price, the worker's pay and how the client paid. Payment itself
happens outside the app.

/request and /join are public. Every other page is for the operator and asks for
ADMIN_PASSWORD when one is set.
"""

from __future__ import annotations

import base64
import binascii
import math
import secrets
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from prodata.config import ROOT, get_settings, load_market_config, load_trade_map
from prodata.connectors import CONNECTORS
from prodata.db import make_sessionmaker, session_scope
from prodata.models import (
    CLIENT_STATUSES,
    OUTREACH_STATUSES,
    PAYMENT_METHODS,
    PAYMENT_STATUSES,
    SUPPRESSION_REASONS,
    SUPPRESSION_TYPES,
    Business,
    BusinessSource,
    DncImport,
    JobRequest,
    OutreachStatus,
    PipelineRun,
    Source,
    Suppression,
    utcnow,
)
from prodata.people import (
    CLIENT_CONSENT,
    CLIENT_CONSENT_VERSION,
    CONTACTED_ME,
    CONTACTED_ME_VERSION,
    TIMINGS,
    TRADES,
    WORKER_CONSENT,
    WORKER_CONSENT_VERSION,
    ConsentMeta,
    FormError,
    client_call_decision,
    client_counts,
    clients_query,
    create_job_request,
    deal_totals,
    is_interested,
    month_start,
    need_label,
    parse_money,
    register_worker,
    suggest_workers,
    worker_counts,
    worker_label,
    worker_plural,
    workers_query,
)
from prodata.pipeline.compliance import (
    SuppressionIndex,
    add_suppression,
    business_suppressed,
    dnc_import_times,
    email_decision_for,
    import_dnc_file,
    phone_decision_for,
    refresh_flags,
)
from prodata.pipeline.export import EMAIL_TOOL_COLUMNS, PROS_COLUMNS, ProFilters, email_tool_rows, export_rows, to_csv
from prodata.pipeline.normalize import display_case, format_phone

HERE = Path(__file__).parent
PAGE_SIZE = 40
PUBLIC_PREFIXES = ("/request", "/join", "/thanks", "/static/")

WORKER_STATUS_LABELS = {
    "not_contacted": "Not called yet",
    "contacted": "Called, no answer",
    "replied": "Talked, follow up",
    "interested": "Interested",
    "signed_up": "Working with us",
    "declined": "Not interested",
    "do_not_contact": "Do not contact",
}
CLIENT_STATUS_LABELS = {
    "new": "New request",
    "called": "Called",
    "matched": "Worker found",
    "done": "Job done",
    "lost": "Lost",
    "do_not_contact": "Do not contact",
}
PAYMENT_METHOD_LABELS = {
    "cash": "Cash",
    "bank_transfer": "Bank transfer",
    "zelle": "Zelle",
    "venmo": "Venmo",
    "cash_app": "Cash App",
    "paypal": "PayPal",
    "check": "Check",
    "other": "Other",
}
PAYMENT_STATUS_LABELS = {"unpaid": "Not paid", "deposit": "Deposit paid", "paid": "Paid in full"}

app = FastAPI(title="Tradebook", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ---- admin password -----------------------------------------------------------------


@app.middleware("http")
async def require_admin(request: Request, call_next):
    password = get_settings().admin_password
    path = request.url.path
    if not password or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            user, given = "", ""
        if secrets.compare_digest(user.encode(), b"admin") and secrets.compare_digest(given.encode(), password.encode()):
            return await call_next(request)
    return Response("Sign in required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Tradebook"'})


# ---- template helpers ---------------------------------------------------------------


def _fmt_dt(value):
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%b %d, %Y %H:%M")
    return value.strftime("%b %d, %Y")


def _fmt_day(value):
    if not value:
        return "—"
    return value.astimezone().strftime("%b %d") if isinstance(value, datetime) else value.strftime("%b %d")


def _ago(value):
    if not value:
        return "never"
    delta = datetime.now(timezone.utc) - value
    if delta.days >= 30:
        return _fmt_day(value)
    if delta.days >= 1:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    if hours:
        return f"{hours}h ago"
    return f"{max(1, delta.seconds // 60)}m ago"


def _money(value):
    if value is None or value == "":
        return "—"
    value = float(value)
    return f"${value:,.0f}" if value == int(value) else f"${value:,.2f}"


def _initials(name: str | None) -> str:
    words = [w for w in (name or "").replace("&", " ").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


templates.env.filters.update(
    phone=format_phone,
    name=lambda v: display_case(v) if v else v,
    dt=_fmt_dt,
    day=_fmt_day,
    ago=_ago,
    money=_money,
    initials=_initials,
    trade=worker_label,
    plural=worker_plural,
    need=need_label,
    num=lambda n: f"{n:,}" if isinstance(n, int) else n,
    tel=lambda e164: "tel:" + (e164 or ""),
)
templates.env.globals.update(
    TRADES=TRADES,
    TIMINGS=TIMINGS,
    WORKER_STATUS_LABELS=WORKER_STATUS_LABELS,
    CLIENT_STATUS_LABELS=CLIENT_STATUS_LABELS,
    PAYMENT_METHOD_LABELS=PAYMENT_METHOD_LABELS,
    PAYMENT_STATUS_LABELS=PAYMENT_STATUS_LABELS,
)

_running: set[str] = set()
_running_lock = threading.Lock()


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    cfg = load_market_config()
    ctx.setdefault("nav", "")
    brand = get_settings().brand_name
    if not name.startswith("public_"):
        with session_scope() as s:
            ctx["new_clients_badge"] = s.scalar(select(func.count(JobRequest.id)).where(JobRequest.status == "new")) or 0
    return templates.TemplateResponse(
        request,
        name,
        {"market": cfg.market, "brand": brand, "running": set(_running), **ctx},
        status_code=status_code,
    )


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _meta(request: Request) -> ConsentMeta:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return ConsentMeta(
        ip=forwarded or (request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent"),
        page_url=str(request.url),
    )


def _pick_phone(biz: Business):
    return next(iter(sorted(biz.phones, key=lambda p: (not p.call_ok, p.consent_at is None, p.label != "office"))), None)


def _worker_rows(session, businesses) -> list[dict]:
    supp = SuppressionIndex.load(session)
    imports = dnc_import_times(session)
    rows = []
    for biz in businesses:
        phone = _pick_phone(biz)
        rows.append(
            {
                "biz": biz,
                "phone": phone,
                "decision": phone_decision_for(phone, supp, imports) if phone else None,
                "trades": sorted({t.trade for t in biz.trades}, key=lambda t: list(TRADES).index(t) if t in TRADES else 99),
                "status": next((o.status for o in biz.outreach if o.channel == "phone"), "not_contacted"),
                "interested": is_interested(biz),
                "signed_up": biz.signed_up_at is not None,
                "licensed": bool(biz.licenses),
                "suppressed": business_suppressed(biz, supp),
            }
        )
    return rows


# ---- home ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with session_scope() as s:
        workers = worker_counts(s)
        clients = client_counts(s)
        month = deal_totals(s, since=month_start())
        all_time = deal_totals(s)
        recent = s.scalars(clients_query(status="open").limit(6)).all()
        supp = SuppressionIndex.load(s)
        recent_rows = [(j, client_call_decision(j, supp)) for j in recent]
        new_workers = s.scalars(
            select(Business)
            .where(Business.signed_up_at.is_not(None))
            .options(selectinload(Business.phones), selectinload(Business.trades))
            .order_by(Business.signed_up_at.desc())
            .limit(5)
        ).all()
        dnc_loaded = bool(dnc_import_times(s))
    return render(
        request,
        "home.html",
        nav="home",
        workers=workers,
        clients=clients,
        month=month,
        all_time=all_time,
        recent=recent_rows,
        new_workers=new_workers,
        dnc_loaded=dnc_loaded,
        base_url=_base_url(request),
    )


# ---- workers ------------------------------------------------------------------------


@app.get("/workers", response_class=HTMLResponse)
def workers(request: Request, page: int = 1):
    qp = request.query_params
    trade = qp.get("trade") or None
    show = qp.get("show") or "all"
    q = qp.get("q") or None
    city = qp.get("city") or None
    with session_scope() as s:
        base = workers_query(trade=trade, q=q, city=city, show=show)
        total = s.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(page, 1), pages)
        stmt = base.options(
            selectinload(Business.licenses),
            selectinload(Business.phones),
            selectinload(Business.trades),
            selectinload(Business.outreach),
        ).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        rows = _worker_rows(s, s.scalars(stmt))
        counts = worker_counts(s)
        cities = list(s.scalars(select(Business.city).where(Business.city.is_not(None)).distinct().order_by(Business.city)))
    params = {k: v for k, v in qp.items() if k != "page" and v}
    return render(
        request,
        "workers.html",
        nav="workers",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        trade=trade,
        show=show,
        q=q or "",
        city=city or "",
        cities=cities,
        counts=counts,
        qs=urlencode(params),
        base_url=_base_url(request),
    )


@app.get("/workers.csv")
def workers_csv(request: Request, format: str = "csv"):
    qp = request.query_params
    f = ProFilters(trade=qp.get("trade") or None, q=qp.get("q") or None, city=qp.get("city") or None)
    cfg = load_market_config()
    with session_scope() as s:
        refresh_flags(s, cfg.compliance.dnc_max_age_days)
        rows = export_rows(s, f, cfg.compliance.dnc_max_age_days)
    if format == "email-tool":
        body, prefix = to_csv(email_tool_rows(rows), EMAIL_TOOL_COLUMNS), "email_tool"
    else:
        body, prefix = to_csv(rows, PROS_COLUMNS), "workers"
    name = f"{prefix}_{cfg.market.id}_{f.trade or 'all'}_{datetime.now():%Y-%m-%d}.csv"
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/workers/new", response_class=HTMLResponse)
def worker_new(request: Request):
    return render(request, "worker_new.html", nav="workers", form={}, error=None)


@app.post("/workers/new", response_class=HTMLResponse)
async def worker_new_save(request: Request):
    form = await request.form()
    if not form.get("contacted_me"):
        return render(
            request, "worker_new.html", nav="workers", form=form, status_code=400,
            error="Tick the box to confirm this worker contacted you or agreed to be called.",
        )
    try:
        with session_scope() as s:
            biz = register_worker(
                s,
                name=form.get("name", ""),
                business_name=form.get("business_name"),
                phone=form.get("phone", ""),
                email=form.get("email"),
                trades=form.getlist("trades"),
                city=form.get("city"),
                zip_code=form.get("zip"),
                about=form.get("about"),
                origin="added_by_me",
                consent_text=CONTACTED_ME,
                consent_version=CONTACTED_ME_VERSION,
                meta=_meta(request),
                state=load_market_config().market.state,
            )
            biz_id = biz.id
    except FormError as exc:
        return render(request, "worker_new.html", nav="workers", form=form, error=str(exc), status_code=400)
    return RedirectResponse(f"/workers/{biz_id}?saved=added", status_code=303)


@app.get("/workers/{business_id}", response_class=HTMLResponse)
def worker_detail(request: Request, business_id: int):
    with session_scope() as s:
        biz = s.scalar(
            select(Business)
            .where(Business.id == business_id)
            .options(
                selectinload(Business.licenses),
                selectinload(Business.phones),
                selectinload(Business.emails),
                selectinload(Business.trades),
                selectinload(Business.outreach),
                selectinload(Business.sources).selectinload(BusinessSource.raw_record),
            )
        )
        if not biz:
            raise HTTPException(404, "Worker not found")
        supp = SuppressionIndex.load(s)
        imports = dnc_import_times(s)
        phones = sorted(
            ((p, phone_decision_for(p, supp, imports)) for p in biz.phones), key=lambda pd: (not pd[1].ok, pd[0].label != "office")
        )
        emails = [(e, email_decision_for(e, supp)) for e in biz.emails]
        outreach = next((o for o in biz.outreach if o.channel == "phone"), None)
        suppressed = business_suppressed(biz, supp)
        raw = sorted((bs.raw_record for bs in biz.sources), key=lambda r: r.fetched_at, reverse=True)
        sources_by_id = {src.source_id: src for src in s.scalars(select(Source))}
        jobs = s.scalars(select(JobRequest).where(JobRequest.worker_id == biz.id).order_by(JobRequest.created_at.desc())).all()
        open_jobs = s.scalars(
            select(JobRequest)
            .where(JobRequest.trade.in_({t.trade for t in biz.trades}), JobRequest.status.in_(("new", "called")), JobRequest.worker_id.is_(None))
            .order_by(JobRequest.created_at.desc())
            .limit(5)
        ).all()
    referer = request.headers.get("referer") or ""
    return render(
        request,
        "worker_detail.html",
        nav="workers",
        biz=biz,
        phones=phones,
        emails=emails,
        outreach=outreach,
        suppressed=suppressed,
        interested=is_interested(biz),
        raw_records=raw,
        sources_by_id=sources_by_id,
        outreach_statuses=OUTREACH_STATUSES,
        trades=sorted({t.trade for t in biz.trades}),
        jobs=jobs,
        open_jobs=open_jobs,
        back=referer if "/workers?" in referer else "/workers",
    )


@app.post("/workers/{business_id}/status")
def worker_status(business_id: int, status: str = Form(...), notes: str = Form(""), called_now: str | None = Form(None)):
    if status not in OUTREACH_STATUSES:
        raise HTTPException(400, "Bad status")
    with session_scope() as s:
        if not s.get(Business, business_id):
            raise HTTPException(404)
        row = s.get(OutreachStatus, (business_id, "phone"))
        if row is None:
            row = OutreachStatus(business_id=business_id, channel="phone")
            s.add(row)
        row.status = status
        row.notes = notes.strip() or None
        if called_now:
            row.last_contact_at = utcnow()
        s.flush()
        if status == "do_not_contact":
            add_suppression(s, str(business_id), "business_id", "manual", "Marked do-not-contact")
    return RedirectResponse(f"/workers/{business_id}?saved=status#call-log", status_code=303)


@app.post("/workers/{business_id}/suppress")
def worker_suppress(business_id: int, value: str = Form(...), value_type: str = Form(...), reason: str = Form("opt_out")):
    with session_scope() as s:
        add_suppression(s, value, value_type, reason, f"From worker #{business_id}")
    return RedirectResponse(f"/workers/{business_id}#contact", status_code=303)


# old links
@app.get("/pros")
def pros_redirect(request: Request):
    return RedirectResponse("/workers" + (f"?{request.url.query}" if request.url.query else ""), status_code=301)


@app.get("/pros/{business_id}")
def pro_redirect(business_id: int):
    return RedirectResponse(f"/workers/{business_id}", status_code=301)


# ---- clients ------------------------------------------------------------------------


@app.get("/clients", response_class=HTMLResponse)
def clients(request: Request, page: int = 1):
    qp = request.query_params
    trade = qp.get("trade") or None
    status = qp.get("status") or "open"
    q = qp.get("q") or None
    with session_scope() as s:
        base = clients_query(trade=trade, status=None if status == "all" else status, q=q)
        total = s.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(page, 1), pages)
        supp = SuppressionIndex.load(s)
        rows = [(j, client_call_decision(j, supp)) for j in s.scalars(base.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))]
        counts = client_counts(s)
    params = {k: v for k, v in qp.items() if k != "page" and v}
    return render(
        request,
        "clients.html",
        nav="clients",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        trade=trade,
        status=status,
        q=q or "",
        counts=counts,
        qs=urlencode(params),
        base_url=_base_url(request),
    )


@app.get("/clients/new", response_class=HTMLResponse)
def client_new(request: Request):
    return render(request, "client_new.html", nav="clients", form={}, error=None)


@app.post("/clients/new", response_class=HTMLResponse)
async def client_new_save(request: Request):
    form = await request.form()
    if not form.get("contacted_me"):
        return render(
            request, "client_new.html", nav="clients", form=form, status_code=400,
            error="Tick the box to confirm this person contacted you and asked to be called back.",
        )
    try:
        with session_scope() as s:
            job = create_job_request(
                s,
                name=form.get("name", ""),
                phone=form.get("phone", ""),
                email=form.get("email"),
                trade=form.get("trade", ""),
                details=form.get("details"),
                address=form.get("address"),
                city=form.get("city"),
                zip_code=form.get("zip"),
                timing=form.get("timing"),
                budget=form.get("budget"),
                origin="contacted_me",
                consent_text=CONTACTED_ME,
                consent_version=CONTACTED_ME_VERSION,
                meta=_meta(request),
            )
            job_id = job.id
    except FormError as exc:
        return render(request, "client_new.html", nav="clients", form=form, error=str(exc), status_code=400)
    return RedirectResponse(f"/clients/{job_id}?saved=added", status_code=303)


@app.get("/clients/{job_id}", response_class=HTMLResponse)
def client_detail(request: Request, job_id: int, error: str | None = None):
    with session_scope() as s:
        job = s.scalar(select(JobRequest).where(JobRequest.id == job_id).options(selectinload(JobRequest.worker)))
        if not job:
            raise HTTPException(404, "Client not found")
        supp = SuppressionIndex.load(s)
        decision = client_call_decision(job, supp)
        suggestions = _worker_rows(s, suggest_workers(s, job)) if not job.worker_id else []
        worker_row = None
        if job.worker_id:
            w = s.scalar(
                select(Business)
                .where(Business.id == job.worker_id)
                .options(selectinload(Business.phones), selectinload(Business.trades), selectinload(Business.outreach), selectinload(Business.licenses))
            )
            worker_row = _worker_rows(s, [w])[0] if w else None
        others = s.scalars(
            select(JobRequest).where(JobRequest.phone_e164 == job.phone_e164, JobRequest.id != job.id).order_by(JobRequest.created_at.desc())
        ).all()
    referer = request.headers.get("referer") or ""
    return render(
        request,
        "client_detail.html",
        nav="clients",
        job=job,
        decision=decision,
        suggestions=suggestions,
        worker_row=worker_row,
        others=others,
        statuses=CLIENT_STATUSES,
        payment_methods=PAYMENT_METHODS,
        payment_statuses=PAYMENT_STATUSES,
        error=error,
        back=referer if "/clients?" in referer else "/clients",
    )


@app.post("/clients/{job_id}/status")
def client_status(job_id: int, status: str = Form(...), notes: str = Form(""), called_now: str | None = Form(None)):
    if status not in CLIENT_STATUSES:
        raise HTTPException(400, "Bad status")
    with session_scope() as s:
        job = s.get(JobRequest, job_id)
        if not job:
            raise HTTPException(404)
        job.status = status
        job.notes = notes.strip() or None
        if called_now:
            job.last_called_at = utcnow()
        if status == "do_not_contact":
            add_suppression(s, job.phone_e164, "phone", "opt_out", f"Client #{job.id} asked not to be contacted")
    return RedirectResponse(f"/clients/{job_id}?saved=status", status_code=303)


@app.post("/clients/{job_id}/assign")
def client_assign(job_id: int, worker_id: str = Form("")):
    with session_scope() as s:
        job = s.get(JobRequest, job_id)
        if not job:
            raise HTTPException(404)
        if worker_id.isdigit():
            if not s.get(Business, int(worker_id)):
                raise HTTPException(404, "Worker not found")
            job.worker_id = int(worker_id)
            if job.status in ("new", "called"):
                job.status = "matched"
        else:
            job.worker_id = None
            if job.status == "matched":
                job.status = "called"
    return RedirectResponse(f"/clients/{job_id}?saved=worker", status_code=303)


@app.post("/clients/{job_id}/deal")
def client_deal(
    job_id: int,
    client_price: str = Form(""),
    worker_price: str = Form(""),
    payment_method: str = Form(""),
    payment_status: str = Form("unpaid"),
):
    try:
        cp, wp = parse_money(client_price), parse_money(worker_price)
    except FormError as exc:
        return RedirectResponse(f"/clients/{job_id}?" + urlencode({"error": str(exc)}) + "#deal", status_code=303)
    if payment_status not in PAYMENT_STATUSES:
        raise HTTPException(400, "Bad payment status")
    with session_scope() as s:
        job = s.get(JobRequest, job_id)
        if not job:
            raise HTTPException(404)
        job.client_price, job.worker_price = cp, wp
        job.payment_method = payment_method if payment_method in PAYMENT_METHODS else None
        if payment_status == "paid" and job.payment_status != "paid":
            job.paid_at = utcnow()
        elif payment_status != "paid":
            job.paid_at = None
        job.payment_status = payment_status
    return RedirectResponse(f"/clients/{job_id}?saved=deal#deal", status_code=303)


# ---- deals --------------------------------------------------------------------------


@app.get("/deals", response_class=HTMLResponse)
def deals(request: Request):
    show = request.query_params.get("show") or "all"
    with session_scope() as s:
        stmt = (
            select(JobRequest)
            .where(JobRequest.client_price.is_not(None))
            .options(selectinload(JobRequest.worker))
            .order_by(JobRequest.created_at.desc())
        )
        if show == "unpaid":
            stmt = stmt.where(JobRequest.payment_status != "paid")
        elif show == "paid":
            stmt = stmt.where(JobRequest.payment_status == "paid")
        rows = s.scalars(stmt).all()
        month = deal_totals(s, since=month_start())
        all_time = deal_totals(s)
        by_method = dict(
            s.execute(
                select(JobRequest.payment_method, func.sum(JobRequest.client_price))
                .where(JobRequest.payment_status == "paid", JobRequest.client_price.is_not(None))
                .group_by(JobRequest.payment_method)
            ).all()
        )
        waiting = s.scalar(
            select(func.count(JobRequest.id)).where(JobRequest.status == "matched", JobRequest.client_price.is_(None))
        ) or 0
    return render(request, "deals.html", nav="deals", rows=rows, show=show, month=month, all_time=all_time, by_method=by_method, waiting=waiting)


# ---- public forms -------------------------------------------------------------------


@app.get("/request", response_class=HTMLResponse)
def public_request(request: Request):
    brand = get_settings().brand_name
    trade = request.query_params.get("trade")
    return render(request, "public_request.html", form={"trade": trade}, error=None, consent=CLIENT_CONSENT.format(brand=brand))


@app.post("/request", response_class=HTMLResponse)
async def public_request_save(request: Request):
    form = await request.form()
    brand = get_settings().brand_name
    consent = CLIENT_CONSENT.format(brand=brand)
    if form.get("website"):  # honeypot: humans never see this field
        return RedirectResponse("/thanks?who=client", status_code=303)
    if not form.get("consent"):
        return render(request, "public_request.html", form=form, consent=consent, status_code=400,
                      error="Please tick the box so we're allowed to call you back.")
    try:
        with session_scope() as s:
            create_job_request(
                s,
                name=form.get("name", ""),
                phone=form.get("phone", ""),
                email=form.get("email"),
                trade=form.get("trade", ""),
                details=form.get("details"),
                address=form.get("address"),
                city=form.get("city"),
                zip_code=form.get("zip"),
                timing=form.get("timing"),
                budget=form.get("budget"),
                origin="request_form",
                consent_text=consent,
                consent_version=CLIENT_CONSENT_VERSION,
                meta=_meta(request),
            )
    except FormError as exc:
        return render(request, "public_request.html", form=form, consent=consent, error=str(exc), status_code=400)
    return RedirectResponse("/thanks?who=client", status_code=303)


@app.get("/join", response_class=HTMLResponse)
def public_join(request: Request):
    brand = get_settings().brand_name
    return render(request, "public_join.html", form={}, error=None, consent=WORKER_CONSENT.format(brand=brand))


@app.post("/join", response_class=HTMLResponse)
async def public_join_save(request: Request):
    form = await request.form()
    brand = get_settings().brand_name
    consent = WORKER_CONSENT.format(brand=brand)
    if form.get("website"):
        return RedirectResponse("/thanks?who=worker", status_code=303)
    if not form.get("consent"):
        return render(request, "public_join.html", form=form, consent=consent, status_code=400,
                      error="Please tick the box so we're allowed to call you about jobs.")
    try:
        with session_scope() as s:
            register_worker(
                s,
                name=form.get("name", ""),
                business_name=form.get("business_name"),
                phone=form.get("phone", ""),
                email=form.get("email"),
                trades=form.getlist("trades"),
                city=form.get("city"),
                zip_code=form.get("zip"),
                about=form.get("about"),
                origin="signup_form",
                consent_text=consent,
                consent_version=WORKER_CONSENT_VERSION,
                meta=_meta(request),
                state=load_market_config().market.state,
            )
    except FormError as exc:
        return render(request, "public_join.html", form=form, consent=consent, error=str(exc), status_code=400)
    return RedirectResponse("/thanks?who=worker", status_code=303)


@app.get("/thanks", response_class=HTMLResponse)
def thanks(request: Request, who: str = "client"):
    return render(request, "public_thanks.html", who="worker" if who == "worker" else "client")


# ---- do-not-call / suppression ------------------------------------------------------


@app.get("/compliance", response_class=HTMLResponse)
def compliance(request: Request, error: str | None = None, msg: str | None = None):
    cfg = load_market_config()
    with session_scope() as s:
        rows = s.scalars(select(Suppression).order_by(Suppression.added_at.desc()).limit(500)).all()
        imports = s.scalars(select(DncImport).order_by(DncImport.imported_at.desc()).limit(100)).all()
    return render(
        request,
        "compliance.html",
        nav="compliance",
        rows=rows,
        imports=imports,
        types=SUPPRESSION_TYPES,
        reasons=SUPPRESSION_REASONS,
        max_age=cfg.compliance.dnc_max_age_days,
        now=datetime.now(timezone.utc),
        error=error,
        msg=msg,
    )


@app.post("/compliance/suppress")
def add_suppression_form(value: str = Form(...), value_type: str = Form(...), reason: str = Form(...), note: str = Form("")):
    try:
        with session_scope() as s:
            add_suppression(s, value, value_type, reason, note.strip() or None)
    except ValueError as exc:
        return RedirectResponse("/compliance?" + urlencode({"error": str(exc)}), status_code=303)
    return RedirectResponse("/compliance?" + urlencode({"msg": f"Added {value} to the do-not-contact list"}), status_code=303)


@app.post("/compliance/dnc")
def upload_dnc(file: UploadFile = File(...)):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / Path(file.filename or "dnc.txt").name
        with path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        with session_scope() as s:
            result = import_dnc_file(s, path)
    msg = f"Imported {result['numbers']:,} numbers across {result['area_codes']} area code(s)."
    return RedirectResponse("/compliance?" + urlencode({"msg": msg}), status_code=303)


# ---- data sources -------------------------------------------------------------------


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    with session_scope() as s:
        sources = {src.source_id: src for src in s.scalars(select(Source))}
        runs = s.scalars(select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(30)).all()
    docs = {sid: (ROOT / "docs" / "sources" / f"{sid}.md").exists() for sid in CONNECTORS}
    return render(request, "sources.html", nav="sources", connectors=CONNECTORS, sources=sources, runs=runs, docs=docs)


@app.get("/sources/{source_id}/doc", response_class=HTMLResponse)
def source_doc(request: Request, source_id: str):
    if source_id not in CONNECTORS:
        raise HTTPException(404)
    path = ROOT / "docs" / "sources" / f"{source_id}.md"
    if not path.exists():
        raise HTTPException(404, "No documentation yet")
    return render(request, "doc.html", nav="sources", title=CONNECTORS[source_id].name, text=path.read_text(encoding="utf-8"))


def _run_source_in_background(source_id: str) -> None:
    import asyncio

    from prodata.http_client import build_client
    from prodata.pipeline.collect import run_collect

    cfg = load_market_config()
    try:

        async def go():
            async with build_client(cfg) as client:
                await run_collect(make_sessionmaker(), source_id, client, cfg, load_trade_map())

        asyncio.run(go())
        with session_scope() as s:
            refresh_flags(s, cfg.compliance.dnc_max_age_days)
    finally:
        with _running_lock:
            _running.discard(source_id)


@app.post("/sources/{source_id}/run")
def run_source(source_id: str):
    if source_id not in CONNECTORS:
        raise HTTPException(404)
    with _running_lock:
        if source_id not in _running:
            _running.add(source_id)
            threading.Thread(target=_run_source_in_background, args=(source_id,), daemon=True).start()
    return RedirectResponse("/sources", status_code=303)
