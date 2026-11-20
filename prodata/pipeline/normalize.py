"""Normalisation helpers (spec 6.2): phones, names, addresses, dates."""

from __future__ import annotations

import re
from datetime import date, datetime

import phonenumbers

LEGAL_SUFFIXES = {
    "llc", "l l c", "inc", "incorporated", "co", "company", "corp", "corporation",
    "ltd", "limited", "lp", "llp", "pllc", "pc",
}

STREET_ABBREVIATIONS = {
    "street": "St", "st.": "St", "avenue": "Ave", "ave.": "Ave", "boulevard": "Blvd",
    "road": "Rd", "drive": "Dr", "lane": "Ln", "court": "Ct", "parkway": "Pkwy",
    "highway": "Hwy", "suite": "Ste", "north": "N", "south": "S", "east": "E", "west": "W",
    "freeway": "Fwy", "circle": "Cir", "place": "Pl", "trail": "Trl",
}


def normalize_phone(raw: str | None, region: str = "US") -> str | None:
    """Return E.164 or None for invalid numbers."""
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", raw)
    if len(re.sub(r"\D", "", digits)) < 10:
        return None
    try:
        num = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def format_phone(e164: str | None) -> str:
    """Human display: (214) 555-0100."""
    if not e164:
        return ""
    try:
        return phonenumbers.format_number(phonenumbers.parse(e164), phonenumbers.PhoneNumberFormat.NATIONAL)
    except phonenumbers.NumberParseException:
        return e164


def normalize_name(name: str | None) -> str:
    """Lowercase, '&'→'and', strip punctuation and legal suffixes, collapse spaces."""
    if not name:
        return ""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # "L.L.C." becomes "l l c" once dots are stripped — rejoin before suffix removal
    s = re.sub(r"\bl l (c|p)\b", r"ll\1", s)
    words = s.split(" ")
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def clean_text(value) -> str | None:
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s or None


def standardize_street(line: str | None) -> str | None:
    line = clean_text(line)
    if not line:
        return None
    out = []
    for word in line.split(" "):
        out.append(STREET_ABBREVIATIONS.get(word.lower(), word))
    return " ".join(out)


_CSZ = re.compile(r"^(?P<city>.*?)[,\s]+(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5})(?:-?\d{4})?$")


def split_city_state_zip(value: str | None) -> tuple[str | None, str | None, str | None]:
    """'DALLAS, TX 75201-1234' → ('DALLAS', 'TX', '75201')."""
    value = clean_text(value)
    if not value:
        return None, None, None
    m = _CSZ.match(value)
    if not m:
        return value, None, None
    return m.group("city").strip(" ,") or None, m.group("state").upper(), m.group("zip")


def normalize_zip(value) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    m = re.match(r"(\d{5})", value)
    return m.group(1) if m else None


_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m%d%Y", "%Y%m%d", "%m/%d/%y", "%m-%d-%Y")


def parse_date(value) -> date | None:
    value = clean_text(value)
    if not value:
        return None
    if "T" in value:  # ISO timestamp, e.g. Socrata floating timestamp
        try:
            return datetime.fromisoformat(value.replace("Z", "")).date()
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def display_case(value: str | None) -> str | None:
    """'ACME PLUMBING LLC' → 'Acme Plumbing LLC' (keeps short all-caps tokens)."""
    if not value:
        return value
    if value != value.upper():
        return value
    keep = {"LLC", "LLP", "LP", "PLLC", "PC", "HVAC", "AC", "A/C", "TX", "USA", "II", "III", "IV", "DBA"}
    words = []
    for w in value.split(" "):
        words.append(w if w in keep else w.capitalize())
    return " ".join(words)
