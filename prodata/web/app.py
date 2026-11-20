"""Tradebook web app: browse pros, record outreach, manage suppression, run sources.

Server-rendered (FastAPI + Jinja2), no front-end build step. Every list and export
goes through prodata.pipeline.export, so the UI can never show a "callable"
number that the CLI export would block.
"""

from __future__ import annotations

import math
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

from prodata.config import ROOT, load_market_config, load_trade_map
from prodata.connectors import CONNECTORS
from prodata.db import make_sessionmaker, session_scope
from prodata.models import (
    OUTREACH_STATUSES,
    SUPPRESSION_REASONS,
    SUPPRESSION_TYPES,
    Business,
    BusinessSource,
    DncImport,
    OutreachStatus,
    PipelineRun,
    Source,
    Suppression,
    utcnow,
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
from prodata.pipeline.export import (
    EMAIL_TOOL_COLUMNS,
    PROS_COLUMNS,
    ProFilters,
    email_tool_rows,
    export_rows,
    pros_query,
    stats,
    to_csv,
)
from prodata.pipeline.normalize import display_case, format_phone

HERE = Path(__file__).parent
PAGE_SIZE = 50
TRADE_LABELS = {
    "plumbing": "Plumbing",
    "flooring_carpet": "Flooring & carpet",
    "handyman": "Handyman",
    "carpentry": "Carpentry",
    "painting": "Painting",
    "roofing": "Roofing",
    "hvac": "HVAC",
    "electrical": "Electrical",
}

app = FastAPI(title="Tradebook", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def _fmt_dt(value):
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%b %d, %Y %H:%M")
    return value.strftime("%b %d, %Y")


def _ago(value):
    if not value:
        return "never"
    delta = datetime.now(timezone.utc) - value
    days = delta.days
    if days >= 1:
        return f"{days}d ago"
    hours = delta.seconds // 3600
    if hours:
        return f"{hours}h ago"
    return f"{max(1, delta.seconds // 60)}m ago"


templates.env.filters["phone"] = format_phone
templates.env.filters["name"] = lambda v: display_case(v) if v else v
templates.env.filters["dt"] = _fmt_dt
templates.env.filters["ago"] = _ago
templates.env.filters["trade"] = lambda t: TRADE_LABELS.get(t, (t or "").replace("_", " ").title())
templates.env.filters["num"] = lambda n: f"{n:,}" if isinstance(n, int) else n
templates.env.globals["TRADE_LABELS"] = TRADE_LABELS

_running: set[str] = set()
_running_lock = threading.Lock()


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    cfg = load_market_config()
    ctx.setdefault("nav", "")
    return templates.TemplateResponse(request, name, {"market": cfg.market, "running": set(_running), **ctx})


def _filters_from(request: Request) -> ProFilters:
    q = request.query_params
    min_score = q.get("min_score")
    return ProFilters(
        trade=q.get("trade") or None,
        q=q.get("q") or None,
        city=q.get("city") or None,
        county=q.get("county") or None,
        min_score=int(min_score) if min_score and min_score.isdigit() else None,
        has_phone=q.get("has_phone") == "1",
        call_ok_only=q.get("call_ok") == "1",
        outreach=q.get("outreach") or None,
    )


# ---- overview ------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    cfg = load_market_config()
    with session_scope() as s:
        st = stats(s)
        sources = {src.source_id: src for src in s.scalars(select(Source))}
        runs = s.scalars(select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(8)).all()
        imports = dnc_import_times(s)
        suppressed = s.scalar(select(func.count(Suppression.id))) or 0
        outreach = dict(
            s.execute(select(OutreachStatus.status, func.count(func.distinct(OutreachStatus.business_id))).group_by(OutreachStatus.status)).all()
        )
    max_age = cfg.compliance.dnc_max_age_days
    now = datetime.now(timezone.utc)
    stale = [a for a, ts in imports.items() if (now - ts).days > max_age]
    return render(
        request,
        "overview.html",
        nav="overview",
        st=st,
        connectors=CONNECTORS,
        sources=sources,
        runs=runs,
        dnc_imports=imports,
        dnc_stale=stale,
        dnc_max_age=max_age,
        suppressed=suppressed,
        outreach=outreach,
    )


# ---- pros ------------------------------------------------------------------------------


@app.get("/pros", response_class=HTMLResponse)
def pros(request: Request, page: int = 1):
    f = _filters_from(request)
    with session_scope() as s:
        base = pros_query(f)
        total = s.scalar(select(func.count()).select_from(base.subquery())) or 0
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(page, 1), pages)
        stmt = (
            base.options(
                selectinload(Business.licenses),
                selectinload(Business.phones),
                selectinload(Business.emails),
                selectinload(Business.trades),
                selectinload(Business.outreach),
            )
            .order_by(Business.quality_score.desc().nulls_last(), Business.legal_name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        supp = SuppressionIndex.load(s)
        imports = dnc_import_times(s)
        rows = []
        for biz in s.scalars(stmt):
            phone = next(iter(sorted(biz.phones, key=lambda p: (not p.call_ok, p.label != "office"))), None)
            email = next(iter(sorted(biz.emails, key=lambda e: (not e.email_ok, e.kind != "role"))), None)
            rows.append(
                {
                    "biz": biz,
                    "phone": phone,
                    "phone_decision": phone_decision_for(phone, supp, imports) if phone else None,
                    "email": email,
                    "license": biz.licenses[0] if biz.licenses else None,
                    "trades": sorted({t.trade for t in biz.trades}),
                    "outreach": {o.channel: o.status for o in biz.outreach},
                    "suppressed": business_suppressed(biz, supp),
                }
            )
        cities = [c for c in s.scalars(select(Business.city).where(Business.city.is_not(None)).distinct().order_by(Business.city))]

    params = {k: v for k, v in request.query_params.items() if k != "page" and v}
    return render(
        request,
        "pros.html",
        nav="pros",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        f=f,
        cities=cities,
        qs=urlencode(params),
        outreach_statuses=OUTREACH_STATUSES,
    )


@app.get("/pros.csv")
def pros_csv(request: Request, format: str = "csv"):
    f = _filters_from(request)
    cfg = load_market_config()
    with session_scope() as s:
        refresh_flags(s, cfg.compliance.dnc_max_age_days)
        rows = export_rows(s, f, cfg.compliance.dnc_max_age_days)
    if format == "email-tool":
        body, prefix = to_csv(email_tool_rows(rows), EMAIL_TOOL_COLUMNS), "email_tool"
    else:
        body, prefix = to_csv(rows, PROS_COLUMNS), "pros"
    name = f"{prefix}_{cfg.market.id}_{f.trade or 'all'}_{datetime.now():%Y-%m-%d}.csv"
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/pros/{business_id}", response_class=HTMLResponse)
def pro_detail(request: Request, business_id: int):
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
            raise HTTPException(404, "Business not found")
        supp = SuppressionIndex.load(s)
        imports = dnc_import_times(s)
        phones = [(p, phone_decision_for(p, supp, imports)) for p in biz.phones]
        emails = [(e, email_decision_for(e, supp)) for e in biz.emails]
        outreach = {o.channel: o for o in biz.outreach}
        suppressed = business_suppressed(biz, supp)
        raw = sorted((bs.raw_record for bs in biz.sources), key=lambda r: r.fetched_at, reverse=True)
        sources_by_id = {src.source_id: src for src in s.scalars(select(Source))}
    return render(
        request,
        "pro_detail.html",
        nav="pros",
        biz=biz,
        phones=phones,
        emails=emails,
        outreach=outreach,
        suppressed=suppressed,
        raw_records=raw,
        sources_by_id=sources_by_id,
        outreach_statuses=OUTREACH_STATUSES,
        trades=sorted({t.trade for t in biz.trades}),
        back=request.headers.get("referer") if "/pros?" in (request.headers.get("referer") or "") else "/pros",
    )


@app.post("/pros/{business_id}/outreach")
def save_outreach(
    business_id: int,
    channel: str = Form(...),
    status: str = Form(...),
    notes: str = Form(""),
    contacted_now: str | None = Form(None),
):
    if channel not in ("email", "phone") or status not in OUTREACH_STATUSES:
        raise HTTPException(400, "Bad channel or status")
    with session_scope() as s:
        row = s.get(OutreachStatus, (business_id, channel))
        if row is None:
            row = OutreachStatus(business_id=business_id, channel=channel)
            s.add(row)
        row.status = status
        row.notes = notes.strip() or None
        if contacted_now:
            row.last_contact_at = utcnow()
        s.flush()
        if status == "do_not_contact":
            add_suppression(s, str(business_id), "business_id", "manual", f"Marked do-not-contact ({channel})")
    return RedirectResponse(f"/pros/{business_id}?saved=1#outreach", status_code=303)


@app.post("/pros/{business_id}/suppress")
def suppress_value(business_id: int, value: str = Form(...), value_type: str = Form(...), reason: str = Form("opt_out")):
    with session_scope() as s:
        add_suppression(s, value, value_type, reason, f"From business #{business_id}")
    return RedirectResponse(f"/pros/{business_id}#contact", status_code=303)


# ---- compliance ------------------------------------------------------------------------


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
    return RedirectResponse("/compliance?" + urlencode({"msg": f"Suppressed {value}"}), status_code=303)


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


# ---- sources ---------------------------------------------------------------------------


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
