"""Configuration: `.env` settings plus the YAML market and trade-map files."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    """Secrets and machine-specific values, read from the environment / `.env`."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://prodata:prodata@localhost:5440/prodata"
    contact_email: str = "ops@example.com"
    prodata_cache_dir: Path = ROOT / ".cache" / "http"
    socrata_app_token: str = ""
    exports_dir: Path = ROOT / "exports"
    # Name shown on the public "Request a pro" and "Join" pages.
    brand_name: str = "Tradebook"
    # When set, every page except the public forms asks for this password (user "admin").
    admin_password: str = ""


class Metro(BaseModel):
    name: str
    center: tuple[float, float]
    radius_miles: float


class Market(BaseModel):
    id: str
    state: str
    metros: list[Metro]
    counties: list[str] = Field(default_factory=list)
    zip_prefixes: list[str] = Field(default_factory=list)

    def county_in_market(self, county: str | None) -> bool:
        if not county:
            return False
        wanted = {c.strip().lower() for c in self.counties}
        return county.strip().lower().removesuffix(" county") in wanted

    def zip_in_market(self, zip_code: str | None) -> bool:
        if not zip_code:
            return False
        return any(zip_code.strip().startswith(p) for p in self.zip_prefixes)


class CrawlConfig(BaseModel):
    user_agent: str = "ProDataBot/0.1 (+mailto:{contact_email})"
    max_rps_per_domain: float = 1.0
    max_pages_per_site: int = 8
    respect_robots_txt: bool = True
    cache_ttl_hours: float = 20


class ComplianceConfig(BaseModel):
    dnc_max_age_days: int = 31


class ScoringConfig(BaseModel):
    min_export_score: int = 50


class MarketConfig(BaseModel):
    market: Market
    trades: list[str]
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)

    def user_agent(self, contact_email: str) -> str:
        return self.crawl.user_agent.format(contact_email=contact_email)


def _key(label: str) -> str:
    """Normalise a licence-class label for lookup: lowercase, single spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/ ]+", " ", label.lower())).strip()


class TradeMap:
    """Source licence class → internal trade. Returns (known, trade)."""

    def __init__(self, data: dict[str, dict[str, str | None]]):
        self._maps = {src: {_key(k): v for k, v in (m or {}).items()} for src, m in data.items()}

    def lookup(self, source_id: str, label: str | None) -> tuple[bool, str | None]:
        if not label:
            return False, None
        mapping = self._maps.get(source_id, {})
        key = _key(label)
        if key in mapping:
            return True, mapping[key]
        return False, None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_market_config(path: Path | None = None) -> MarketConfig:
    path = path or CONFIG_DIR / "market.yaml"
    return MarketConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_trade_map(path: Path | None = None) -> TradeMap:
    path = path or CONFIG_DIR / "trade_map.yaml"
    return TradeMap(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
