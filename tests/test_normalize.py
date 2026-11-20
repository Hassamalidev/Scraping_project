from datetime import date

from prodata.pipeline.normalize import (
    display_case,
    normalize_name,
    normalize_phone,
    parse_date,
    split_city_state_zip,
    standardize_street,
)


def test_phone_to_e164():
    assert normalize_phone("(214) 555-0142") == "+12145550142"
    assert normalize_phone("214.555.0142") == "+12145550142"
    assert normalize_phone("+1 214 555 0142") == "+12145550142"


def test_invalid_phone_dropped():
    assert normalize_phone("not listed") is None
    assert normalize_phone("555-0142") is None
    assert normalize_phone("") is None
    assert normalize_phone(None) is None


def test_name_normalisation():
    assert normalize_name("A & B Plumbing Co., L.L.C.") == "a and b plumbing"
    assert normalize_name("ACME PLUMBING, INC.") == "acme plumbing"
    assert normalize_name("Trinity  Pipe &  Drain LLC") == "trinity pipe and drain"
    # a name that *is* a suffix word is kept
    assert normalize_name("Co") == "co"


def test_city_state_zip():
    assert split_city_state_zip("FORT WORTH, TX 76102-1234") == ("FORT WORTH", "TX", "76102")
    assert split_city_state_zip("DALLAS TX 75201") == ("DALLAS", "TX", "75201")
    assert split_city_state_zip("somewhere") == ("somewhere", None, None)


def test_dates():
    assert parse_date("03/31/2027") == date(2027, 3, 31)
    assert parse_date("2027-03-31T00:00:00.000") == date(2027, 3, 31)
    assert parse_date("03312027") == date(2027, 3, 31)
    assert parse_date("n/a") is None


def test_display_and_street():
    assert display_case("ACME PLUMBING LLC") == "Acme Plumbing LLC"
    assert display_case("Already Mixed") == "Already Mixed"
    assert standardize_street("100 Main Street Suite 4") == "100 Main St Ste 4"
