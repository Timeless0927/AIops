"""Temporary Gateway database and Connector Enrollment assembly."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from .connector_enrollments import ConnectorEnrollments
from .gateway_db import GatewayDatabase


class GatewayV1Store:
    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        clock: Callable[[], float] = time.time,
        credential_factory: Callable[[], str] | None = None,
        id_factory: Callable[[str], str] | None = None,
        database: GatewayDatabase | None = None,
    ) -> None:
        self._database = database or GatewayDatabase(db_path)
        self.connector_enrollments = ConnectorEnrollments(
            self._database,
            clock=clock,
            credential_factory=credential_factory,
            id_factory=id_factory,
        )

    @property
    def db_path(self) -> Path:
        return self._database.db_path

    @property
    def database(self) -> GatewayDatabase:
        return self._database
