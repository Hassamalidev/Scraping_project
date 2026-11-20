"""Web app tests (need the test database)."""

from decimal import Decimal

import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from prodata.config import get_settings
from prodata.models import Business, JobRequest, OutreachStatus, Phone, Suppression
from prodata.web.app import app

from .test_connectors import mock_tsbpe
from .test_pipeline_db import collect_tsbpe

REQUEST = {
    "trade": "plumbing",
    "details": "Leak under the kitchen sink.\nWater is off.",
    "name": "dana homeowner",
    "phone": "(214) 555-0199",
    "city": "dallas",
    "zip": "75201",
    "timing": "asap",
    "consent": "1",
}


@respx.mock
def test_pages_render_and_worker_call_log(db, make_client, market, trade_map):
    mock_tsbpe()
    collect_tsbpe(db, make_client, market, trade_map)
    web = TestClient(app)

    for path in ("/", "/workers", "/workers?trade=plumbing&q=acme", "/workers?show=interested", "/clients",
                 "/clients?status=all", "/deals", "/compliance", "/sources", "/request", "/join", "/workers/new", "/clients/new"):
        resp = web.get(path)
        assert resp.status_code == 200, path

    page = web.get("/workers?q=acme").text
    assert "Acme Plumbing LLC" in page and "Trinity" not in page
    assert "(214) 555-0142" in page  # the number is shown…
    assert "Don't call yet: no DNC data" in page  # …but the Call button is held back until DNC data exists

    with db() as s:
        acme_id = s.scalar(select(Business.id).where(Business.normalized_name == "acme plumbing"))
    assert web.get(f"/pros/{acme_id}", follow_redirects=False).headers["location"] == f"/workers/{acme_id}"
    detail = web.get(f"/workers/{acme_id}").text
    assert "RMP-10001" in detail and "Where this came from" in detail

    resp = web.post(
        f"/workers/{acme_id}/status",
        data={"status": "do_not_contact", "notes": "asked not to be called", "called_now": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with db() as s:
        assert s.get(OutreachStatus, (acme_id, "phone")).status == "do_not_contact"
        assert s.scalar(select(Suppression.value).where(Suppression.value_type == "business_id")) == str(acme_id)
    assert "Hidden: asked not to be contacted" in web.get("/workers?q=acme").text

    csv_body = web.get("/workers.csv?trade=plumbing").text
    assert "Acme Plumbing" not in csv_body and "Trinity Pipe" in csv_body


def test_client_request_form_to_paid_deal(db):
    web = TestClient(app)
    # consent box is required
    resp = web.post("/request", data={**REQUEST, "consent": ""})
    assert resp.status_code == 400 and "tick the box" in resp.text
    resp = web.post("/request", data={**REQUEST, "phone": "123"})
    assert resp.status_code == 400 and "valid US phone number" in resp.text

    resp = web.post("/request", data=REQUEST, headers={"user-agent": "pytest-browser"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/thanks?who=client"
    with db() as s:
        job = s.scalar(select(JobRequest))
        assert job.name == "Dana Homeowner" and job.city == "Dallas" and job.phone_e164 == "+12145550199"
        assert job.details == "Leak under the kitchen sink.\nWater is off."
        assert job.origin == "request_form" and job.consent_version == "client-v1"
        assert "may call me" in job.consent_text and job.consent_user_agent == "pytest-browser"
        job_id = job.id

    listing = web.get("/clients").text
    assert "Dana Homeowner" in listing and 'href="tel:+12145550199"' in listing  # callable right away
    assert '<span class="count">1</span>' in web.get("/").text  # new-request badge

    # a worker signs up for plumbing and shows up as a suggestion
    resp = web.post("/join", data={"name": "Sam Pipes", "phone": "214-555-0177", "trades": ["plumbing"], "city": "Dallas", "consent": "1"}, follow_redirects=False)
    assert resp.status_code == 303
    with db() as s:
        sam = s.scalar(select(Business).where(Business.contact_name == "Sam Pipes"))
        assert sam.signed_up_at is not None
        phone = s.scalar(select(Phone).where(Phone.business_id == sam.id))
        assert phone.consent_at is not None and phone.call_ok
    detail = web.get(f"/clients/{job_id}").text
    assert "Sam Pipes" in detail and "Signed up, wants work" in detail

    web.post(f"/clients/{job_id}/assign", data={"worker_id": str(sam.id)})
    bad = web.post(f"/clients/{job_id}/deal", data={"client_price": "abc"}, follow_redirects=True)
    assert "is not an amount" in bad.text
    web.post(f"/clients/{job_id}/deal", data={"client_price": "$450", "worker_price": "300", "payment_method": "zelle", "payment_status": "paid"})
    with db() as s:
        job = s.get(JobRequest, job_id)
        assert job.status == "matched" and job.worker_id == sam.id
        assert job.my_cut == Decimal("150.00") and job.payment_status == "paid" and job.paid_at is not None
    deals = web.get("/deals").text
    assert "$450" in deals and "$150" in deals and "Zelle" in deals

    # client asks not to be contacted → number suppressed
    web.post(f"/clients/{job_id}/status", data={"status": "do_not_contact"})
    with db() as s:
        assert s.scalar(select(Suppression.value).where(Suppression.value_type == "phone")) == "+12145550199"
    assert "tel:+12145550199" not in web.get(f"/clients/{job_id}").text


def test_join_links_to_existing_business_by_phone(db, make_client, market, trade_map):
    with respx.mock:
        mock_tsbpe()
        collect_tsbpe(db, make_client, market, trade_map)
    web = TestClient(app)
    web.post("/join", data={"name": "Ann Acme", "phone": "(214) 555-0142", "trades": ["plumbing", "handyman"], "consent": "1"})
    with db() as s:
        assert s.scalar(select(func.count()).select_from(Business).where(Business.normalized_name == "acme plumbing")) == 1
        acme = s.scalar(select(Business).where(Business.normalized_name == "acme plumbing"))
        assert acme.legal_name == "Acme Plumbing LLC"  # licence data not overwritten
        assert acme.signed_up_at is not None and acme.contact_name == "Ann Acme"
        assert s.scalar(select(Phone.call_ok).where(Phone.business_id == acme.id))  # consent → callable without DNC data


def test_manual_client_needs_confirmation_and_honeypot(db):
    web = TestClient(app)
    data = {k: v for k, v in REQUEST.items() if k != "consent"}
    assert web.post("/clients/new", data=data).status_code == 400
    resp = web.post("/clients/new", data={**data, "contacted_me": "1"}, follow_redirects=False)
    assert resp.status_code == 303
    web.post("/request", data={**REQUEST, "website": "spam.example"})
    with db() as s:
        jobs = s.scalars(select(JobRequest)).all()
        assert len(jobs) == 1 and jobs[0].origin == "contacted_me"


def test_admin_password_protects_everything_but_public_forms(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_password", "s3cret")
    web = TestClient(app)
    assert web.get("/").status_code == 401
    assert web.get("/clients").status_code == 401
    assert web.get("/request").status_code == 200
    assert web.get("/join").status_code == 200
    assert web.get("/static/style.css").status_code == 200
    assert web.get("/", auth=("admin", "wrong")).status_code == 401
    assert web.get("/", auth=("admin", "s3cret")).status_code == 200


def test_bad_suppression_value_shows_error(db):
    web = TestClient(app)
    resp = web.post("/compliance/suppress", data={"value": "nope", "value_type": "phone", "reason": "opt_out"})
    assert resp.status_code == 200 and "Not a valid phone number" in resp.text
