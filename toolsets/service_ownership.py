"""Service ownership and team routing cache backed by BlueKing CMDB."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib import error, request

from toolsets.topology_store import normalize_service_identity


DEFAULT_STALE_AFTER_SECONDS = 3600
DEFAULT_TEAM = "sre"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS service_ownership (
    service_key TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    service_name TEXT,
    owner_team TEXT,
    notification_channel TEXT,
    rbac_scope TEXT,
    approval_scope TEXT,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    observed_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_service_ownership_owner_team
ON service_ownership(owner_team);
"""


class CMDBClientUnavailable(RuntimeError):
    """Raised when BlueKing CMDB cannot be queried in a controlled way."""


@dataclass(frozen=True)
class CMDBServiceOwner:
    service_id: str
    service_name: str | None
    owner_team: str | None
    notification_channel: str | None = None
    rbac_scope: str | None = None
    approval_scope: str | None = None
    source: str = "bk_cmdb"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "service_ownership.db"
    return _project_root() / "data" / "service_ownership.db"


def _now() -> float:
    return time.time()


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _cmdb_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    cmdb = config.get("cmdb")
    if isinstance(cmdb, dict):
        return cmdb
    sre = config.get("sre")
    if isinstance(sre, dict) and isinstance(sre.get("cmdb"), dict):
        return sre["cmdb"]
    return {}


def _default_team(config: dict[str, Any] | None) -> str:
    cmdb = _cmdb_config(config)
    return str(cmdb.get("default_team") or DEFAULT_TEAM).strip() or DEFAULT_TEAM


def _default_notification_channel(config: dict[str, Any] | None) -> str | None:
    cmdb = _cmdb_config(config)
    value = cmdb.get("default_notification_channel")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _stale_after_seconds(config: dict[str, Any] | None) -> int:
    cmdb = _cmdb_config(config)
    try:
        value = int(cmdb.get("stale_after_seconds", DEFAULT_STALE_AFTER_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_STALE_AFTER_SECONDS
    return max(0, value)


def _as_owner(row: dict[str, Any], *, source: str | None = None) -> dict[str, Any]:
    owner_team = _first_text(row.get("owner_team"), row.get("team"), row.get("owner"))
    service_id = _first_text(row.get("service_id"), row.get("bk_biz_id"), row.get("id"), row.get("service_key"))
    service_name = _first_text(row.get("service_name"), row.get("name"), row.get("service"))
    notification_channel = _first_text(row.get("notification_channel"), row.get("channel"), row.get("chat_id"))
    rbac_scope = _first_text(row.get("rbac_scope"))
    approval_scope = _first_text(row.get("approval_scope"))
    if not owner_team or not service_id:
        return {}
    return {
        "service_id": service_id,
        "service_name": service_name,
        "owner_team": owner_team,
        "notification_channel": notification_channel,
        "rbac_scope": rbac_scope or f"team:{owner_team}",
        "approval_scope": approval_scope or f"team:{owner_team}",
        "source": source or str(row.get("source") or "bk_cmdb"),
    }


def _owner_from_record(record: CMDBServiceOwner | dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    if isinstance(record, CMDBServiceOwner):
        raw = {
            "service_id": record.service_id,
            "service_name": record.service_name,
            "owner_team": record.owner_team,
            "notification_channel": record.notification_channel,
            "rbac_scope": record.rbac_scope,
            "approval_scope": record.approval_scope,
            "source": record.source,
        }
    elif isinstance(record, dict):
        raw = record
    else:
        return None
    normalized = _as_owner(raw)
    return normalized or None


def build_service_candidates(alert: dict[str, Any]) -> list[str]:
    """Build normalized service keys from alert labels and target fields."""
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    cluster = _first_text(alert.get("cluster"), alert.get("cluster_id"), labels.get("cluster"), labels.get("cluster_id"))
    namespace = _first_text(alert.get("namespace"), labels.get("namespace"))
    raw_services = [
        alert.get("service"),
        alert.get("service_name"),
        labels.get("service"),
        labels.get("service_name"),
        labels.get("app.kubernetes.io/name"),
        labels.get("app"),
        alert.get("workload_name"),
        labels.get("workload"),
        labels.get("deployment"),
        labels.get("deployment_name"),
        alert.get("app_id"),
        labels.get("app_id"),
    ]
    candidates: list[str] = []
    for raw_service in raw_services:
        if not isinstance(raw_service, str) or not raw_service.strip():
            continue
        identity = normalize_service_identity(cluster, namespace, raw_service)
        candidates.append(identity.service_id)
    if not candidates:
        identity = normalize_service_identity(cluster, namespace, None)
        candidates.append(identity.service_id)
    return list(dict.fromkeys(candidates))


class ConfigCMDBClient:
    """Minimal CMDB client: static mappings first, optional HTTP endpoint second."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = _cmdb_config(config)
        self._mapping = self._load_mapping(self.config)

    def _load_mapping(self, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        mapping: dict[str, dict[str, Any]] = {}
        items = config.get("service_ownership") or config.get("services") or []
        if isinstance(items, dict):
            iterable = []
            for key, value in items.items():
                if isinstance(value, dict):
                    iterable.append({"service_key": key, **value})
        elif isinstance(items, list):
            iterable = [item for item in items if isinstance(item, dict)]
        else:
            iterable = []
        for item in iterable:
            key = _first_text(item.get("service_key"))
            if key:
                mapping[key] = item
        return mapping

    async def lookup_service_owner(self, candidates: Iterable[str]) -> CMDBServiceOwner | dict[str, Any] | None:
        for candidate in candidates:
            if candidate in self._mapping:
                return self._mapping[candidate]
        endpoint = _first_text(self.config.get("endpoint"), self.config.get("url"))
        if not endpoint:
            return None
        return await asyncio.to_thread(self._lookup_http, endpoint, list(candidates))

    def _lookup_http(self, endpoint: str, candidates: list[str]) -> dict[str, Any] | None:
        token = _first_text(self.config.get("token"), os.getenv("BK_CMDB_TOKEN"))
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = json.dumps({"service_keys": candidates}, ensure_ascii=False).encode("utf-8")
        req = request.Request(endpoint, data=payload, headers=headers, method="POST")
        timeout = float(self.config.get("timeout_seconds") or 3.0)
        try:
            with request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - configured internal endpoint
                body = response.read().decode("utf-8")
        except (OSError, error.URLError, TimeoutError) as exc:
            raise CMDBClientUnavailable(str(exc)) from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CMDBClientUnavailable("invalid cmdb response") from exc
        if isinstance(data, dict):
            result = data.get("data") if isinstance(data.get("data"), dict) else data
            if isinstance(result, dict):
                return result
        return None


class ServiceOwnershipStore:
    """SQLite cache for CMDB service ownership records."""

    def __init__(self, db_path: Path | None = None, *, stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.stale_after_seconds = stale_after_seconds
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.close()
            self._conn = None

    def upsert_ownership(
        self,
        *,
        service_key: str,
        service_id: str,
        service_name: str | None = None,
        owner_team: str | None = None,
        notification_channel: str | None = None,
        rbac_scope: str | None = None,
        approval_scope: str | None = None,
        source: str = "bk_cmdb",
        confidence: float = 0.95,
        observed_at: float | None = None,
    ) -> None:
        observed = observed_at if observed_at is not None else _now()
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            self._conn.execute(
                """
                INSERT INTO service_ownership (
                    service_key, service_id, service_name, owner_team, notification_channel,
                    rbac_scope, approval_scope, source, confidence, observed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_key) DO UPDATE SET
                    service_id = excluded.service_id,
                    service_name = excluded.service_name,
                    owner_team = excluded.owner_team,
                    notification_channel = excluded.notification_channel,
                    rbac_scope = excluded.rbac_scope,
                    approval_scope = excluded.approval_scope,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    observed_at = excluded.observed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    service_key,
                    service_id,
                    service_name,
                    owner_team,
                    notification_channel,
                    rbac_scope,
                    approval_scope,
                    source,
                    confidence,
                    observed,
                    _now(),
                ),
            )

    def get_cached_ownership(self, service_key: str) -> dict[str, Any] | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("数据库连接已关闭")
            row = self._conn.execute(
                "SELECT * FROM service_ownership WHERE service_key = ?",
                (service_key,),
            ).fetchone()
        return dict(row) if row is not None else None


_STORE: ServiceOwnershipStore | None = None


def get_store(config: dict[str, Any] | None = None) -> ServiceOwnershipStore:
    global _STORE
    if _STORE is None:
        _STORE = ServiceOwnershipStore(stale_after_seconds=_stale_after_seconds(config))
    return _STORE


async def resolve_alert_ownership(
    alert: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    store: ServiceOwnershipStore | None = None,
    cmdb_client: Any | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Resolve alert ownership from CMDB, cache, or default team fallback."""
    current_time = now if now is not None else _now()
    effective_store = store or get_store(config)
    if "stale_after_seconds" in _cmdb_config(config):
        effective_store.stale_after_seconds = _stale_after_seconds(config)
    client = cmdb_client or ConfigCMDBClient(config)
    candidates = build_service_candidates(alert)
    warnings: list[str] = []
    owner: dict[str, Any] | None = None

    try:
        owner = _owner_from_record(await client.lookup_service_owner(candidates))
    except CMDBClientUnavailable:
        warnings.append("cmdb_unavailable")

    service_key = candidates[0]
    if owner is not None:
        effective_store.upsert_ownership(
            service_key=service_key,
            service_id=owner["service_id"],
            service_name=owner.get("service_name"),
            owner_team=owner.get("owner_team"),
            notification_channel=owner.get("notification_channel"),
            rbac_scope=owner.get("rbac_scope"),
            approval_scope=owner.get("approval_scope"),
            source=owner.get("source") or "bk_cmdb",
            confidence=0.95,
            observed_at=current_time,
        )
        return {
            **owner,
            "service_key": service_key,
            "ownership_source": owner.get("source") or "bk_cmdb",
            "ownership_status": "owned",
            "confidence": 0.95,
            "warnings": warnings,
        }

    cached = next(
        (row for candidate in candidates if (row := effective_store.get_cached_ownership(candidate)) is not None),
        None,
    )
    if cached is not None:
        age = max(0.0, current_time - float(cached.get("observed_at") or 0.0))
        if age <= effective_store.stale_after_seconds:
            owner_team = str(cached.get("owner_team") or "").strip()
            return {
                "service_key": str(cached["service_key"]),
                "service_id": str(cached["service_id"]),
                "service_name": cached.get("service_name"),
                "owner_team": owner_team,
                "notification_channel": cached.get("notification_channel"),
                "rbac_scope": cached.get("rbac_scope") or f"team:{owner_team}",
                "approval_scope": cached.get("approval_scope") or f"team:{owner_team}",
                "ownership_source": "cache",
                "ownership_status": "owned" if owner_team else "unowned",
                "confidence": 0.75,
                "warnings": warnings,
            }
        warnings.append("ownership_cache_stale")

    warnings.append("cmdb_owner_missing")
    default_team = _default_team(config)
    return {
        "service_key": service_key,
        "service_id": service_key,
        "service_name": service_key.rsplit("/", 1)[-1],
        "owner_team": default_team,
        "notification_channel": _default_notification_channel(config),
        "rbac_scope": f"team:{default_team}",
        "approval_scope": f"team:{default_team}",
        "ownership_source": "default_team",
        "ownership_status": "unowned",
        "confidence": 0.1 if "cmdb_unavailable" in warnings else 0.2,
        "warnings": list(dict.fromkeys(warnings)),
    }
