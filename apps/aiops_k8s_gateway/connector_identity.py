"""Connector credential and registered Cluster identity."""

from __future__ import annotations

import sqlite3
import time

from aiops.domain.identity import IdentityError

from .gateway_db import GatewayDatabase, token_hash


class ConnectorIdentity:
    def __init__(self, database: GatewayDatabase) -> None:
        self._database = database

    def authenticate(
        self,
        credential: str,
        connector_id: str,
        cluster_id: str,
        *,
        require_registered: bool = False,
    ) -> None:
        with self._database.connect() as conn:
            self.authenticate_in(
                conn,
                credential,
                connector_id,
                cluster_id,
                require_registered=require_registered,
            )

    def authenticate_in(
        self,
        conn: sqlite3.Connection,
        credential: str,
        connector_id: str,
        cluster_id: str,
        *,
        require_registered: bool = False,
    ) -> None:
        row = conn.execute(
            "SELECT connector_id, cluster_id FROM connector_enrollments WHERE credential_hash = ? AND active = 1",
            (token_hash(credential),),
        ).fetchone()
        if row is None:
            raise IdentityError("invalid_connector_credential", "Connector credential is invalid or revoked")
        if row["connector_id"] != connector_id or row["cluster_id"] != cluster_id:
            raise IdentityError("identity_mismatch", "Connector identity does not match its Enrollment")
        if require_registered and conn.execute(
            "SELECT 1 FROM clusters WHERE cluster_id = ? AND connector_id = ?",
            (cluster_id, connector_id),
        ).fetchone() is None:
            raise IdentityError("not_registered", "Connector must register before discovery")

    def is_cluster_registered_in(self, conn: sqlite3.Connection, cluster_id: str) -> bool:
        return conn.execute("SELECT 1 FROM clusters WHERE cluster_id = ?", (cluster_id,)).fetchone() is not None

    def metrics(self, *, now: float | None = None) -> str:
        with self._database.connect() as conn:
            last_heartbeat = conn.execute("SELECT MIN(last_heartbeat) FROM clusters").fetchone()[0]
        age = max(0.0, (time.time() if now is None else now) - float(last_heartbeat)) if last_heartbeat else 0.0
        return (
            "# HELP aiops_gateway_connector_heartbeat_age_seconds Age of the stalest Connector heartbeat\n"
            "# TYPE aiops_gateway_connector_heartbeat_age_seconds gauge\n"
            f"aiops_gateway_connector_heartbeat_age_seconds {age:.1f}\n"
        )
