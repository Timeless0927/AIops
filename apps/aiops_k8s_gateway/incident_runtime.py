"""Gateway runtime adapter for time-based Incident lifecycle transitions."""

from __future__ import annotations

import logging
import os
import threading

from .connector_identity import ConnectorIdentity
from .gateway_db import GatewayDatabase
from .incident import IncidentService
from .connector_commands import ConnectorCommands
from .resource_catalog import ResourceCatalog


def incident_service(database: GatewayDatabase) -> IncidentService:
    return IncidentService(
        database,
        ResourceCatalog(database),
        ConnectorIdentity(database),
        stabilization_seconds=float(os.getenv("AIOPS_INCIDENT_STABILIZATION_SECONDS", "300")),
        reopen_seconds=float(os.getenv("AIOPS_INCIDENT_REOPEN_SECONDS", "86400")),
        diagnosis_request_ttl_seconds=float(os.getenv("AIOPS_DIAGNOSIS_REQUEST_TTL_SECONDS", "900")),
    )


def start_incident_reconciler(
    incidents: IncidentService,
    *,
    connector_commands: ConnectorCommands | None = None,
    interval_seconds: float = 1.0,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    stop = stop_event or threading.Event()

    def reconcile() -> None:
        while not stop.is_set():
            try:
                if connector_commands is not None:
                    connector_commands.reconcile_unknown_outcomes()
                incidents.reconcile_due()
            except Exception:
                logging.exception("Incident lifecycle reconciliation failed")
            stop.wait(interval_seconds)

    thread = threading.Thread(target=reconcile, name="incident-reconciler", daemon=True)
    thread.start()
    return thread
