"""collect → store raw → parse → load, for one connector.

Incremental: a raw record is only parsed when its (source, external id, content
hash) is new, so re-running against an unchanged file does no work.
Every run is logged in `pipeline_runs`.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from prodata.config import MarketConfig, TradeMap
from prodata.connectors import get_connector_class
from prodata.connectors.base import BaseConnector, ParseSkip, RawRecordIn
from prodata.http_client import HttpClient
from prodata.logs import log
from prodata.models import PipelineRun, RawRecord, Source, utcnow
from prodata.pipeline.load import load_parsed

COMMIT_EVERY = 500


def ensure_source(session: Session, connector: type[BaseConnector]) -> Source:
    src = session.scalar(select(Source).where(Source.source_id == connector.source_id))
    if src is None:
        src = Source(source_id=connector.source_id)
        session.add(src)
    src.name = connector.name
    src.tier = connector.tier
    src.url = connector.url
    src.terms_notes = connector.terms_notes
    session.flush()
    return src


def store_raw(session: Session, raw: RawRecordIn) -> int | None:
    """Insert a raw record; return its id, or None when this exact version exists."""
    stmt = (
        insert(RawRecord)
        .values(
            source_id=raw.source_id,
            external_id=raw.external_id,
            url=raw.url,
            payload=raw.payload,
            content_hash=raw.content_hash,
            fetched_at=raw.fetched_at,
        )
        .on_conflict_do_nothing(constraint="uq_raw_record_version")
        .returning(RawRecord.id)
    )
    return session.execute(stmt).scalar()


async def run_collect(
    session_factory: sessionmaker[Session],
    source_id: str,
    client: HttpClient,
    market: MarketConfig,
    trade_map: TradeMap,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cls = get_connector_class(source_id)
    connector = cls(client, market, trade_map, options)
    counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    errors: list[dict[str, str]] = []

    with session_factory() as session:
        ensure_source(session, cls)
        run = PipelineRun(stage="collect", source_id=source_id, status="running", counts={})
        session.add(run)
        session.commit()
        run_id = run.id

    session = session_factory()
    status = "ok"
    try:
        for job in connector.discover(market):
            async for raw in connector.fetch(job):
                counts["fetched"] += 1
                raw_id = store_raw(session, raw)
                if raw_id is None:
                    counts["unchanged"] += 1
                    continue
                counts["new_or_changed"] += 1
                try:
                    for pro in connector.parse(raw):
                        counts["parsed"] += 1
                        if not pro.in_market:
                            counts["out_of_market"] += 1
                            continue
                        _, created = load_parsed(session, pro, raw_id)
                        counts["businesses_created" if created else "businesses_updated"] += 1
                except ParseSkip as skip:
                    skipped[skip.reason] += 1
                    counts["skipped"] += 1
                except ValidationError as exc:
                    counts["parse_errors"] += 1
                    if len(errors) < 50:
                        errors.append({"external_id": raw.external_id, "error": str(exc)[:300]})
                if counts["fetched"] % COMMIT_EVERY == 0:
                    session.commit()
                    log.info("collect.progress", source=source_id, **dict(counts))
        session.commit()
    except Exception as exc:  # stop and report — never work around a failing source
        session.rollback()
        status = "failed"
        errors.append({"error": f"{type(exc).__name__}: {exc}"})
        log.error("collect.failed", source=source_id, error=str(exc))
    finally:
        session.close()

    summary = dict(counts)
    if skipped:
        summary["skip_reasons"] = dict(skipped.most_common(20))
    if connector.unmapped_classes:
        summary["unmapped_classes"] = dict(sorted(connector.unmapped_classes.items(), key=lambda kv: -kv[1])[:30])
    attempted = counts["new_or_changed"]
    if attempted:
        summary["parse_rate"] = round((attempted - counts["parse_errors"]) / attempted, 4)

    with session_factory() as session:
        run = session.get(PipelineRun, run_id)
        run.finished_at = utcnow()
        run.status = status
        run.counts = summary
        run.errors = errors or None
        src = session.scalar(select(Source).where(Source.source_id == source_id))
        src.last_run_at = run.finished_at
        src.last_status = status
        session.commit()

    log.info("collect.done", source=source_id, status=status, **{k: v for k, v in summary.items() if not isinstance(v, dict)})
    return {"run_id": run_id, "status": status, "counts": summary, "errors": errors}
