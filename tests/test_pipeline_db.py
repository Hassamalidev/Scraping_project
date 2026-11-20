"""End-to-end pipeline tests against the Postgres test database (skipped if it isn't up)."""

import asyncio
import csv
import io

import respx
from sqlalchemy import func, select

from prodata.models import Business, BusinessSource, License, Phone, PipelineRun, RawRecord, Source
from prodata.pipeline.collect import run_collect
from prodata.pipeline.compliance import add_suppression, import_dnc_file, refresh_flags
from prodata.pipeline.export import PROS_COLUMNS, ProFilters, export_rows, to_csv

from .conftest import FIXTURES
from .test_connectors import mock_tsbpe

RMP_URL = "https://tsbpe.texas.gov/wp-content/uploads/licensee/RMP_List.csv"


def collect_tsbpe(db, make_client, market, trade_map, **client_kw):
    async def go():
        client = make_client(**client_kw)
        try:
            return await run_collect(db, "tx_tsbpe_plumbing", client, market, trade_map)
        finally:
            await client.aclose()

    return asyncio.run(go())


@respx.mock
def test_collect_stores_raw_and_loads_businesses(db, make_client, market, trade_map):
    mock_tsbpe()
    result = collect_tsbpe(db, make_client, market, trade_map)
    assert result["status"] == "ok"
    c = result["counts"]
    assert c["fetched"] == 4 and c["new_or_changed"] == 4
    assert c["parse_rate"] >= 0.95
    assert c["out_of_market"] == 1  # Houston
    assert c["businesses_created"] == 3

    with db() as s:
        assert s.scalar(select(func.count(RawRecord.id))) == 4  # raw kept even out of market
        assert all(len(h) == 64 for h in s.scalars(select(RawRecord.content_hash)))
        biz = s.scalar(select(Business).where(Business.normalized_name == "acme plumbing"))
        assert biz.legal_name == "Acme Plumbing LLC" and biz.city == "Dallas"
        lic = s.scalar(select(License).where(License.business_id == biz.id))
        assert lic.license_number == "RMP-10001" and lic.raw_record_id
        phone = s.scalar(select(Phone).where(Phone.business_id == biz.id))
        assert phone.e164 == "+12145550142" and phone.published_as_business
        assert phone.dnc_status == "unknown" and phone.call_ok is False
        # provenance: every business links to its raw record
        assert s.scalar(select(func.count()).select_from(BusinessSource)) == 3
        run = s.scalar(select(PipelineRun))
        assert run.status == "ok" and run.finished_at is not None
        assert s.scalar(select(Source.last_status)) == "ok"


@respx.mock
def test_rerun_is_incremental(db, make_client, market, trade_map, tmp_path):
    mock_tsbpe()
    collect_tsbpe(db, make_client, market, trade_map)
    # Second run with an empty cache so the file is really re-downloaded.
    again = collect_tsbpe(db, make_client, market, trade_map, cache_dir=tmp_path / "cache2")
    assert again["counts"]["unchanged"] == 4
    assert again["counts"].get("new_or_changed", 0) == 0

    # One row changes → only that row is reprocessed, and it updates the same business.
    changed = (FIXTURES / "tsbpe_rmp.csv").read_text().replace("100 MAIN STREET", "200 MAIN STREET")
    respx.get(RMP_URL).respond(200, text=changed)
    third = collect_tsbpe(db, make_client, market, trade_map, cache_dir=tmp_path / "cache3")
    assert third["counts"]["new_or_changed"] == 1
    assert third["counts"]["businesses_updated"] == 1
    with db() as s:
        assert s.scalar(select(func.count(RawRecord.id))) == 5  # old version kept, never deleted
        assert s.scalar(select(func.count(Business.id))) == 3
        assert s.scalar(select(Business.address_line1).where(Business.normalized_name == "acme plumbing")) == "200 Main St"


@respx.mock
def test_suppressed_values_never_exported(db, make_client, market, trade_map, tmp_path):
    mock_tsbpe()
    collect_tsbpe(db, make_client, market, trade_map)
    with db() as s:
        trinity = s.scalar(select(Business).where(Business.normalized_name == "trinity pipe and drain"))
        add_suppression(s, "(214) 555-0142", "phone", "opt_out")
        add_suppression(s, str(trinity.id), "business_id", "dnc_request")
        s.commit()
        rows = export_rows(s, ProFilters(trade="plumbing"))
    body = to_csv(rows, PROS_COLUMNS)
    assert "+12145550142" not in body
    assert "Trinity" not in body
    header = next(csv.reader(io.StringIO(body)))
    assert header == PROS_COLUMNS
    names = {r["business_name"] for r in rows}
    assert names == {"Acme Plumbing LLC", "Sam Smith"}
    acme = next(r for r in rows if r["business_name"] == "Acme Plumbing LLC")
    assert acme["phone_e164"] == "" and acme["call_ok"] is False


@respx.mock
def test_dnc_import_enables_calling_for_business_lines(db, make_client, market, trade_map, tmp_path):
    mock_tsbpe()
    collect_tsbpe(db, make_client, market, trade_map)
    dnc = tmp_path / "214.txt"
    dnc.write_text("2145550142\n2145550000\n")  # Acme's number is on the registry
    with db() as s:
        result = import_dnc_file(s, dnc)
        s.commit()
        assert result == {"numbers": 2, "area_codes": 1}
        phone = s.scalar(select(Phone).where(Phone.e164 == "+12145550142"))
        assert phone.dnc_status == "listed"
        # Listed, but published on the official licence record as the business line.
        assert phone.call_ok is True
        # 817 has no DNC import yet → still blocked.
        assert s.scalar(select(Phone.call_ok).where(Phone.e164 == "+18175550199")) is False
        add_suppression(s, "+12145550142", "phone", "opt_out")
        s.commit()
        assert s.scalar(select(Phone.call_ok).where(Phone.e164 == "+12145550142")) is False
        assert refresh_flags(s)["call_blocked"] == 2
