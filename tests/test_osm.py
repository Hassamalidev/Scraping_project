import asyncio
import json

import respx
from sqlalchemy import func, select

from prodata.connectors.osm_overpass import ENDPOINT, OsmOverpassConnector, build_query
from prodata.models import Business, BusinessSource, BusinessTrade, Phone
from prodata.pipeline.collect import run_collect

from .conftest import FIXTURES
from .test_connectors import collect, mock_tsbpe
from .test_pipeline_db import collect_tsbpe


def mock_overpass():
    respx.get("https://overpass-api.de/robots.txt").respond(404)
    respx.get(url__startswith=ENDPOINT).respond(200, json=json.loads((FIXTURES / "overpass_dfw.json").read_text()))


def test_query_covers_carpenters_and_radius():
    q = build_query((32.7767, -96.7970), 40)
    assert '"craft"="carpenter"' in q and '"craft"="handyman"' in q and '"shop"="flooring"' in q
    assert "around:64373,32.7767,-96.797" in q
    assert q.startswith("[out:json]") and q.endswith("out center tags;")


@respx.mock
def test_parse_osm_elements(make_client, market, trade_map):
    mock_overpass()
    conn = OsmOverpassConnector(make_client(), market, trade_map)
    raws = collect(conn)
    assert len(raws) == 5  # the element without tags is ignored
    assert raws[0].url == "https://www.openstreetmap.org/node/1001"

    parsed, skipped = [], []
    for r in raws:
        try:
            parsed += list(conn.parse(r))
        except Exception as exc:  # ParseSkip
            skipped.append(str(exc))
    assert {p.legal_name for p in parsed} == {"Oak & Pine Carpentry", "Fix-It Fort Worth", "ACME Plumbing"}
    assert any("closed" in s for s in skipped) and any("no business name" in s for s in skipped)

    oak = parsed[0]
    assert oak.trades == ["carpentry"] and oak.trade_evidence == "osm"
    assert oak.website == "https://oakpine.example"
    assert oak.emails == ["info@oakpine.example"]
    assert oak.address_line1 == "50 Elm Street" and oak.zip == "75226"
    assert oak.phones[0].evidence is None  # crowd-sourced → not a confirmed business line
    fix = parsed[1]
    assert fix.trades == ["handyman"] and (fix.lat, fix.lon) == (32.75, -97.33)


@respx.mock
def test_osm_links_to_licensed_business_by_phone(db, make_client, market, trade_map):
    mock_tsbpe()
    collect_tsbpe(db, make_client, market, trade_map)
    mock_overpass()

    async def go():
        client = make_client()
        try:
            return await run_collect(db, "osm_overpass", client, market, trade_map)
        finally:
            await client.aclose()

    result = asyncio.run(go())
    assert result["status"] == "ok", result
    assert result["counts"]["businesses_created"] == 2  # carpenter + handyman
    assert result["counts"]["businesses_updated"] == 1  # ACME matched by phone

    with db() as s:
        acme = s.scalar(select(Business).where(Business.normalized_name == "acme plumbing"))
        # licence data is not overwritten by OSM
        assert acme.legal_name == "Acme Plumbing LLC" and acme.address_line1 == "100 Main St"
        assert acme.geom is not None and acme.distance_to_metro_center_mi < 1
        methods = set(s.scalars(select(BusinessSource.match_method).where(BusinessSource.business_id == acme.id)))
        assert methods == {"new", "phone"}
        assert s.scalar(select(func.count()).select_from(Phone).where(Phone.business_id == acme.id)) == 1

        oak = s.scalar(select(Business).where(Business.normalized_name == "oak and pine carpentry"))
        assert oak.city == "Dallas" and oak.website == "https://oakpine.example"
        assert s.scalar(select(BusinessTrade.evidence).where(BusinessTrade.business_id == oak.id)) == "osm"
