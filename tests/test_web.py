"""Web app smoke tests (need the test database)."""

import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from prodata.models import Business, OutreachStatus, Suppression
from prodata.web.app import app

from .test_connectors import mock_tsbpe
from .test_pipeline_db import collect_tsbpe


@respx.mock
def test_pages_render_and_outreach_flow(db, make_client, market, trade_map):
    mock_tsbpe()
    collect_tsbpe(db, make_client, market, trade_map)
    web = TestClient(app)

    for path in ("/", "/pros", "/pros?trade=plumbing&q=acme", "/compliance", "/sources"):
        resp = web.get(path)
        assert resp.status_code == 200, path

    pros = web.get("/pros?q=acme").text
    assert "Acme Plumbing LLC" in pros and "Trinity" not in pros
    assert "Blocked" in pros  # no DNC data yet

    with db() as s:
        acme_id = s.scalar(select(Business.id).where(Business.normalized_name == "acme plumbing"))
    detail = web.get(f"/pros/{acme_id}").text
    assert "RMP-10001" in detail and "no DNC data for this area code" in detail
    assert "Where this came from" in detail

    resp = web.post(
        f"/pros/{acme_id}/outreach",
        data={"channel": "phone", "status": "do_not_contact", "notes": "asked not to be called", "contacted_now": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with db() as s:
        assert s.get(OutreachStatus, (acme_id, "phone")).status == "do_not_contact"
        assert s.scalar(select(Suppression.value).where(Suppression.value_type == "business_id")) == str(acme_id)

    csv_body = web.get("/pros.csv?trade=plumbing").text
    assert "Acme Plumbing" not in csv_body and "Trinity Pipe" in csv_body


def test_bad_suppression_value_shows_error(db):
    web = TestClient(app)
    resp = web.post("/compliance/suppress", data={"value": "nope", "value_type": "phone", "reason": "opt_out"})
    assert resp.status_code == 200 and "Not a valid phone number" in resp.text
