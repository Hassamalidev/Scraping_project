from datetime import datetime, timedelta, timezone

from prodata.pipeline.compliance import SMS_OK, SuppressionIndex, call_decision, email_decision

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
FRESH = NOW - timedelta(days=3)
STALE = NOW - timedelta(days=32)


def call(**kw):
    args = dict(
        e164="+12145550142",
        suppressed=False,
        dnc_status="clear",
        published_as_business=False,
        business_evidence=None,
        dnc_imported_at=FRESH,
        now=NOW,
    )
    args.update(kw)
    return call_decision(**args)


def test_clear_number_with_fresh_dnc_data_can_be_called():
    assert call().ok


def test_no_dnc_import_blocks_calling():
    d = call(dnc_imported_at=None, dnc_status="unknown")
    assert not d.ok and "no DNC data" in d.reason


def test_dnc_data_older_than_31_days_blocks_calling():
    assert not call(dnc_imported_at=STALE).ok
    # also when the number is a published business line
    assert not call(dnc_imported_at=STALE, published_as_business=True, business_evidence="on license record").ok
    # exactly 31 days is still allowed
    assert call(dnc_imported_at=NOW - timedelta(days=31)).ok


def test_listed_number_is_blocked_unless_confirmed_business_line():
    assert not call(dnc_status="listed").ok
    assert not call(dnc_status="listed", published_as_business=True, business_evidence=None).ok
    assert call(dnc_status="listed", published_as_business=True, business_evidence="on license record").ok


def test_unknown_status_is_blocked():
    assert not call(dnc_status="unknown").ok


def test_suppression_overrides_everything():
    d = call(suppressed=True, published_as_business=True, business_evidence="on license record")
    assert not d.ok and d.reason == "on suppression list"


def test_invalid_number_is_blocked():
    assert not call(e164="+1555").ok


def test_sms_is_never_ok_in_phase_1():
    assert SMS_OK is False


def test_email_rules():
    base = dict(email="info@acme.com", suppressed=False)
    assert email_decision(**base, mx_valid=True, verification_status=None).ok
    assert email_decision(**base, mx_valid=None, verification_status="valid").ok
    assert not email_decision(**base, mx_valid=None, verification_status=None).ok  # not verified yet
    assert not email_decision(**base, mx_valid=False, verification_status=None).ok
    assert not email_decision(**base, mx_valid=True, verification_status="invalid").ok  # hard bounce
    assert not email_decision(email="info@acme.com", suppressed=True, mx_valid=True, verification_status="valid").ok


def test_suppression_index_matching():
    idx = SuppressionIndex(phones={"+12145550142"}, emails={"bob@x.com"}, domains={"acme.com"}, business_ids={7})
    assert idx.phone_suppressed("+12145550142")
    assert idx.phone_suppressed("+18175550199", business_id=7)
    assert not idx.phone_suppressed("+18175550199", business_id=8)
    assert idx.email_suppressed("BOB@x.com")
    assert idx.email_suppressed("anyone@acme.com")
    assert not idx.email_suppressed("info@other.com")
