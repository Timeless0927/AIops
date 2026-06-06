"""Outbound Gateway stream client boundary for the Cluster Connector."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectorRegistration:
    """Connector registration payload sent to Gateway."""

    connector_id: str
    cluster_id: str
    namespace_scope: tuple[str, ...]
    capabilities: tuple[str, ...]
