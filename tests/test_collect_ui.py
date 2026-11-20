"""The "Collect workers" page: start collection from the browser and see the result."""

import threading

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import prodata.http_client
import prodata.web.app as web_app
from prodata.models import Business

from .test_connectors import mock_tsbpe


@pytest.fixture
def web(db, make_client, monkeypatch):
    # run collection in a thread like the app does, but wait for it to finish
    def spawn_and_wait(target, *args):
        t = threading.Thread(target=target, args=args)
        t.start()
        t.join()

    monkeypatch.setattr(web_app, "_spawn", spawn_and_wait)
    monkeypatch.setattr(prodata.http_client, "build_client", lambda *a, **kw: make_client())
    return TestClient(web_app.app)


def test_empty_workers_page_offers_collect_button(web):
    page = web.get("/workers").text
    assert "No workers yet" in page and 'action="/collect"' in page and "Collect workers now" in page
    assert "uv run prodata" not in page
    collect = web.get("/collect").text
    assert "Licensed plumbers" in collect and "Not collected yet" in collect and "Start collecting" in collect


@respx.mock
def test_collect_button_runs_source_and_reports_counts(web, db):
    mock_tsbpe()
    resp = web.post("/collect", data={"source": ["tx_tsbpe_plumbing"]}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/collect?started=1"
    with db() as s:
        assert s.scalar(select(func.count(Business.id))) == 3
    page = web.get("/collect?started=1").text
    assert "3 new workers added" in page
    assert "<b>3</b> new" in page and "Done" in page
    assert 'http-equiv="refresh"' not in page  # nothing running any more
    assert "Collect again" in page


@respx.mock
def test_unreachable_source_shows_plain_message(web):
    respx.get("https://tsbpe.texas.gov/robots.txt").mock(side_effect=httpx.ConnectError("getaddrinfo failed"))
    web.post("/collect", data={"source": ["tx_tsbpe_plumbing"]})
    page = web.get("/collect").text
    assert "Didn&#39;t finish" in page or "Didn't finish" in page
    assert "Couldn&#39;t connect to the website" in page or "Couldn't connect to the website" in page


def test_error_messages_are_plain_language():
    f = web_app._friendly_error
    html_406 = "SourceError: HTTP 406 from Overpass: <html><head><title>406 Not Acceptable</title></head></html>"
    assert f(html_406).startswith("The website refused requests from this internet connection (HTTP 406)")
    assert "<" not in f("SourceError: something <b>odd</b>")
    assert "busy" in f("SourceError: HTTP 504 from Overpass: gateway timeout")
    assert "Couldn't connect" in f("SourceUnreachable: Could not reach tsbpe.texas.gov to read robots.txt")
