"""`prodata` command line (spec section 8)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from prodata.config import ROOT, get_settings, load_market_config, load_trade_map
from prodata.logs import configure_logging

app = typer.Typer(help="Tradebook — Pro Data Engine", no_args_is_help=True, add_completion=False)
sources_app = typer.Typer(help="Data sources", no_args_is_help=True)
dnc_app = typer.Typer(help="National DNC Registry data", no_args_is_help=True)
suppress_app = typer.Typer(help="Suppression list (permanent, overrides everything)", no_args_is_help=True)
db_app = typer.Typer(help="Database", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(dnc_app, name="dnc")
app.add_typer(suppress_app, name="suppress")
app.add_typer(db_app, name="db")


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    configure_logging("DEBUG" if verbose else "INFO")


def _pending(step: str, milestone: str) -> None:
    typer.echo(f"`{step}` is planned for milestone {milestone} and is not implemented yet.")
    raise typer.Exit(0)


def _check_market(market_id: str | None):
    cfg = load_market_config()
    if market_id and market_id != cfg.market.id:
        typer.echo(f"Market '{market_id}' is not configured (config/market.yaml has '{cfg.market.id}').", err=True)
        raise typer.Exit(2)
    return cfg


# ---- db -------------------------------------------------------------------------


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply database migrations."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    typer.echo("Database is up to date.")


# ---- sources -------------------------------------------------------------------


@sources_app.command("list")
def sources_list() -> None:
    """List available connectors and their last run."""
    from prodata.connectors import CONNECTORS

    last: dict = {}
    try:
        from sqlalchemy import select

        from prodata.db import session_scope
        from prodata.models import Source

        with session_scope() as s:
            last = {src.source_id: src for src in s.scalars(select(Source))}
    except Exception:
        typer.echo("(database not reachable — showing connectors only)\n")

    typer.echo(f"{'SOURCE':<22}{'TIER':<6}{'TRADES':<20}{'LAST RUN':<22}STATUS")
    for sid, cls in CONNECTORS.items():
        src = last.get(sid)
        when = src.last_run_at.strftime("%Y-%m-%d %H:%M") if src and src.last_run_at else "never"
        typer.echo(f"{sid:<22}{cls.tier:<6}{','.join(cls.trades):<20}{when:<22}{(src.last_status if src else '') or ''}")


@sources_app.command("probe")
def sources_probe(source_id: str) -> None:
    """Fetch a small sample from a source and show how its columns map."""
    from prodata.connectors import get_connector_class
    from prodata.http_client import build_client

    cfg = load_market_config()

    async def go():
        async with build_client(cfg) as client:
            connector = get_connector_class(source_id)(client, cfg, load_trade_map())
            return await connector.probe()

    typer.echo(json.dumps(asyncio.run(go()), indent=2, default=str))


# ---- collect --------------------------------------------------------------------


@app.command()
def collect(
    source: Annotated[Optional[str], typer.Option("--source", help="Source id")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Run every source")] = False,
    url: Annotated[Optional[str], typer.Option(help="Override the source's file/API URL")] = None,
) -> None:
    """Collect, store raw records, parse and load."""
    from prodata.connectors import CONNECTORS

    if not source and not all_:
        typer.echo("Pass --source <id> or --all.", err=True)
        raise typer.Exit(2)
    ids = list(CONNECTORS) if all_ else [source]
    failed = False
    for sid in ids:
        result = _collect_one(sid, {"url": url} if url else None)
        failed |= result["status"] != "ok"
        typer.echo(json.dumps({"source": sid, **result}, indent=2, default=str))
    _refresh_flags()
    raise typer.Exit(1 if failed else 0)


def _collect_one(source_id: str, options: dict | None) -> dict:
    from prodata.db import make_sessionmaker
    from prodata.http_client import build_client
    from prodata.pipeline.collect import run_collect

    cfg = load_market_config()

    async def go():
        async with build_client(cfg) as client:
            return await run_collect(make_sessionmaker(), source_id, client, cfg, load_trade_map(), options)

    return asyncio.run(go())


def _refresh_flags() -> dict:
    from prodata.db import session_scope
    from prodata.pipeline.compliance import refresh_flags

    cfg = load_market_config()
    with session_scope() as s:
        return refresh_flags(s, cfg.compliance.dnc_max_age_days)


# ---- later pipeline stages -------------------------------------------------------


@app.command()
def normalize() -> None:
    """Normalise phones, names and addresses (done at load time for now)."""
    typer.echo("Phones (E.164), names and street abbreviations are normalised during `collect`.")
    _pending("normalize (usaddress parsing)", "M3")


@app.command()
def geocode() -> None:
    """Geocode addresses (Census batch geocoder) and compute distances."""
    _pending("geocode", "M3")


@app.command()
def dedupe(review: Annotated[bool, typer.Option("--review")] = False) -> None:
    """Entity resolution (rule 1, licence number, already runs during collect)."""
    _pending("dedupe rules 2–5", "M4")


@app.command()
def enrich(limit: int = 500) -> None:
    """Website discovery and crawling."""
    _pending("enrich", "M5")


@app.command()
def verify() -> None:
    """Refresh compliance flags (licence/website/email verification lands in M6)."""
    typer.echo(json.dumps(_refresh_flags(), indent=2))


@app.command()
def score() -> None:
    """Compute quality scores."""
    _pending("score", "M7")


# ---- compliance -------------------------------------------------------------------


@dnc_app.command("import")
def dnc_import(path: Path) -> None:
    """Load a National DNC Registry area-code file."""
    from prodata.db import session_scope
    from prodata.pipeline.compliance import import_dnc_file

    with session_scope() as s:
        result = import_dnc_file(s, path)
    typer.echo(f"Imported {result['numbers']:,} numbers across {result['area_codes']} area code(s). Flags refreshed.")


@suppress_app.command("add")
def suppress_add(
    value: str,
    type_: Annotated[str, typer.Option("--type", help="phone|email|domain|business_id")],
    reason: Annotated[str, typer.Option(help="opt_out|dnc_request|bounce|complaint|manual")] = "opt_out",
    note: Annotated[Optional[str], typer.Option()] = None,
) -> None:
    """Add a value to the permanent suppression list."""
    from prodata.db import session_scope
    from prodata.pipeline.compliance import add_suppression

    try:
        with session_scope() as s:
            row = add_suppression(s, value, type_, reason, note)
            typer.echo(f"Suppressed {row.value_type} {row.value} ({row.reason}).")
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


@suppress_app.command("list")
def suppress_list() -> None:
    """Show the suppression list."""
    from sqlalchemy import select

    from prodata.db import session_scope
    from prodata.models import Suppression

    with session_scope() as s:
        for row in s.scalars(select(Suppression).order_by(Suppression.added_at.desc())):
            typer.echo(f"{row.added_at:%Y-%m-%d}  {row.value_type:<12}{row.value:<32}{row.reason:<12}{row.note or ''}")


# ---- export & stats -----------------------------------------------------------------


@app.command()
def export(
    market: Annotated[str, typer.Option()],
    trade: Annotated[Optional[str], typer.Option()] = None,
    min_score: Annotated[Optional[int], typer.Option("--min-score")] = None,
    format_: Annotated[str, typer.Option("--format", help="csv|email-tool")] = "csv",
) -> None:
    """Write an outreach-ready CSV (suppressed values are always excluded)."""
    from prodata.db import session_scope
    from prodata.pipeline.export import ProFilters, write_export

    cfg = _check_market(market)
    if format_ not in ("csv", "email-tool"):
        typer.echo("--format must be csv or email-tool", err=True)
        raise typer.Exit(2)
    _refresh_flags()
    with session_scope() as s:
        path, n = write_export(s, cfg.market.id, ProFilters(trade=trade, min_score=min_score), format_, get_settings().exports_dir)
    typer.echo(f"Wrote {n:,} rows to {path}")


@app.command()
def stats(market: Annotated[Optional[str], typer.Option()] = None) -> None:
    """Coverage report."""
    from prodata.db import session_scope
    from prodata.pipeline.export import stats as compute_stats

    _check_market(market)
    with session_scope() as s:
        typer.echo(json.dumps(compute_stats(s), indent=2))


@app.command()
def run(market: Annotated[str, typer.Option()]) -> None:
    """Full pipeline (the stages built so far)."""
    from prodata.connectors import CONNECTORS

    _check_market(market)
    for sid in CONNECTORS:
        result = _collect_one(sid, None)
        typer.echo(f"{sid}: {result['status']} {json.dumps(result['counts'], default=str)}")
    typer.echo(f"flags: {_refresh_flags()}")
    typer.echo("Remaining stages (geocode, dedupe 2–5, enrich, verify, score) arrive in later milestones.")


# ---- web ---------------------------------------------------------------------------


@app.command()
def web(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Start the Tradebook web app."""
    import uvicorn

    uvicorn.run("prodata.web.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
