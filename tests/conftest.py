from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from prodata.config import ROOT, load_market_config, load_trade_map
from prodata.http_client import HttpClient, RateLimiter

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB_URL = os.environ.get(
    "PRODATA_TEST_DATABASE_URL", "postgresql+psycopg://prodata:prodata@localhost:5440/prodata_test"
)
UA = "ProDataBot/0.1 (+mailto:test@example.com)"


@pytest.fixture
def market():
    return load_market_config()


@pytest.fixture
def trade_map():
    return load_trade_map()


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def _make(**kw) -> HttpClient:
        kw.setdefault("user_agent", UA)
        kw.setdefault("cache_dir", tmp_path / "cache")
        kw.setdefault("rate_limiter", RateLimiter(0))
        kw.setdefault("backoff_base_seconds", 0)
        c = HttpClient(**kw)
        clients.append(c)
        return c

    return _make


# ---- database (skipped when the test database is not running) ------------------------


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL, connect_args={"connect_timeout": 3})
    try:
        with eng.connect():
            pass
    except OperationalError:
        pytest.skip("test database not reachable — run `docker compose up -d`")

    from alembic import command
    from alembic.config import Config

    from prodata.models import Base

    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = TEST_DB_URL
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine, monkeypatch):
    from prodata import db as db_module
    from prodata.models import Base

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    # Everything that opens its own session (web app, CLI helpers) uses the test DB.
    monkeypatch.setattr(db_module, "get_engine", lambda url=None: engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
