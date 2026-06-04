"""Connector routing boundary for the K8s Gateway."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectorRoute:
    """Resolved route to an online Connector stream."""

    cluster_id: str
    connector_id: str
    session_id: str
