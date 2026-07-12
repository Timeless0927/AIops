"""Gateway HTTP metrics assembled from domain owner snapshots."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from apps.service_http import metrics_body as service_metrics_body

from . import APP_NAME
from .connector_commands import ConnectorCommands
from .connector_identity import ConnectorIdentity
from .diagnosis_delivery import DiagnosisDelivery
from .gateway_db import GatewayDatabase
from .investigation_event_http import sse_connections


def metrics_body(
    database: GatewayDatabase,
    *,
    handler_type: type[BaseHTTPRequestHandler] | None,
) -> bytes:
    sse = (
        "# HELP aiops_gateway_sse_connections Current authenticated Investigation SSE connections\n"
        "# TYPE aiops_gateway_sse_connections gauge\n"
        f"aiops_gateway_sse_connections {sse_connections()}\n"
    )
    return (
        service_metrics_body(APP_NAME, handler_type)
        + ConnectorIdentity(database).metrics().encode()
        + DiagnosisDelivery(database).metrics().encode()
        + ConnectorCommands(database).metrics().encode()
        + sse.encode()
    )
