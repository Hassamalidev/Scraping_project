import asyncio
import json
from datetime import date

import pytest
import respx

from prodata.connectors.base import ParseSkip, SourceError
from prodata.connectors.tx_tdlr_licenses import TdlrLicensesConnector
from prodata.connectors.tx_tsbpe_plumbing import LIST_PAGE, TsbpePlumbingConnector, find_rmp_link

from .conftest import FIXTURES

RMP_URL = "https://tsbpe.texas.gov/wp-content/uploads/licensee/RMP_List.csv"


def collect(connector):
    async def go():
        out = []
        for job in connector.discover(connector.market):
            async for raw in connector.fetch(job):
                out.append(raw)
        await connector.client.aclose()
        return out

    return asyncio.run(go())


def mock_tsbpe():
    respx.get("https://tsbpe.texas.gov/robots.txt").respond(404)
    respx.get(LIST_PAGE).respond(200, text=(FIXTURES / "tsbpe_list_page.html").read_text())
    respx.get(RMP_URL).respond(200, content=(FIXTURES / "tsbpe_rmp.csv").read_bytes())


# ---- TSBPE ---------------------------------------------------------------------------


def test_finds_rmp_link_not_other_lists():
    html = (FIXTURES / "tsbpe_list_page.html").read_text()
    assert find_rmp_link(html) == RMP_URL


def test_missing_rmp_link_stops_with_clear_error():
    with pytest.raises(SourceError, match="Responsible Master Plumber"):
        find_rmp_link("<a href='/other.csv'>Journeyman</a>")


@respx.mock
def test_tsbpe_fetch_and_parse(make_client, market, trade_map):
    mock_tsbpe()
    conn = TsbpePlumbingConnector(make_client(), market, trade_map)
    raws = collect(conn)
    assert len(raws) == 4  # row without a licence number is not a record
    assert all(r.url == RMP_URL and len(r.content_hash) == 64 for r in raws)

    pros = [p for r in raws for p in conn.parse(r)]
    assert len(pros) == 4  # ≥ 95% of rows parse (all valid ones here)
    acme = pros[0]
    assert acme.legal_name == "ACME PLUMBING LLC"
    assert acme.city == "DALLAS" and acme.zip == "75201" and acme.county == "DALLAS"
    assert acme.phones[0].raw == "(214) 555-0142"
    assert acme.phones[0].evidence == "on license record"
    assert acme.license.agency == "TSBPE" and acme.license.license_number == "RMP-10001"
    assert acme.license.expires_on == date(2027, 3, 31)
    assert acme.license.qualifier_name == "JANE ROE"
    assert acme.trades == ["plumbing"]
    assert acme.in_market

    by_id = {p.license.license_number: p for p in pros}
    assert by_id["RMP-10002"].zip == "76102"
    assert not by_id["RMP-10003"].in_market  # Harris County is outside DFW
    sole = by_id["RMP-10004"]  # no company name → licensee name, marked as such
    assert sole.legal_name == "SAM SMITH" and sole.entity_type == "sole_proprietor_or_unknown"


def test_tsbpe_unmapped_class_is_skipped_and_logged(make_client, market, trade_map):
    from datetime import datetime, timezone

    from prodata.connectors.base import RawRecordIn

    conn = TsbpePlumbingConnector(make_client(), market, trade_map)
    raw = RawRecordIn(
        source_id=conn.source_id,
        external_id="X|Water Supply Protection Specialist",
        url=RMP_URL,
        payload={"License Number": "X", "License Type": "Water Supply Protection Specialist", "Name": "A B"},
        fetched_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ParseSkip):
        list(conn.parse(raw))
    assert conn.unmapped_classes == {"Water Supply Protection Specialist": 1}


# ---- TDLR ----------------------------------------------------------------------------


def test_tdlr_query_is_limited_to_contractors_in_market(make_client, market, trade_map):
    conn = TdlrLicensesConnector(make_client(), market, trade_map)
    where = conn.where_clause()
    assert "'A/C CONTRACTOR'" in where and "'ELECTRICAL CONTRACTOR'" in where
    assert "TECHNICIAN" not in where and "JOURNEYMAN" not in where
    assert "upper(business_county) in (" in where and "'TARRANT'" in where


@respx.mock
def test_tdlr_fetch_pages_and_parse(make_client, market, trade_map, monkeypatch):
    import prodata.connectors.tx_tdlr_licenses as mod

    monkeypatch.setattr(mod, "PAGE_SIZE", 2)
    rows = json.loads((FIXTURES / "tdlr_page.json").read_text())
    respx.get("https://data.texas.gov/robots.txt").respond(404)
    route = respx.get(url__startswith="https://data.texas.gov/resource/7358-krk7.json").mock(
        side_effect=lambda req: respx.MockResponse(200, json=rows[:2] if "offset=0" in str(req.url) else rows[2:])
    )
    conn = TdlrLicensesConnector(make_client(), market, trade_map)
    raws = collect(conn)
    assert route.call_count == 2  # paged until a short page
    assert len(raws) == 3

    ac = list(conn.parse(raws[0]))[0]
    assert ac.legal_name == "NORTH TEXAS AIR LLC"
    assert ac.city == "DALLAS" and ac.zip == "75202"
    assert ac.trades == ["hvac"] and ac.license.agency == "TDLR"
    assert ac.license.classification == "Class A"
    # Only the business phone is imported, never the owner's phone.
    assert [p.raw for p in ac.phones] == ["2145550177"]

    elec = list(conn.parse(raws[1]))[0]
    assert elec.trades == ["electrical"] and elec.city == "ARLINGTON"

    with pytest.raises(ParseSkip):  # individual licence → not a business
        list(conn.parse(raws[2]))
