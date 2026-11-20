"""Connector registry."""

from __future__ import annotations

from prodata.connectors.base import BaseConnector
from prodata.connectors.osm_overpass import OsmOverpassConnector
from prodata.connectors.tx_tdlr_licenses import TdlrLicensesConnector
from prodata.connectors.tx_tsbpe_plumbing import TsbpePlumbingConnector

CONNECTORS: dict[str, type[BaseConnector]] = {
    c.source_id: c for c in (TsbpePlumbingConnector, TdlrLicensesConnector, OsmOverpassConnector)
}


def get_connector_class(source_id: str) -> type[BaseConnector]:
    try:
        return CONNECTORS[source_id]
    except KeyError:
        raise KeyError(f"Unknown source '{source_id}'. Known: {', '.join(CONNECTORS)}") from None
