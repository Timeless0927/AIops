"""Console Next cluster registry and connector health state."""

from __future__ import annotations

import os
import random
import sqlite3
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, TypeVar

JSON = dict[str, Any]
T = TypeVar("T")

ENVIRONMENTS = {"prod", "staging", "dev", "test"}
SECRET_KEYS = ("token", "secret", "password", "internal_url", "database_path", "db_path")
STALE_AFTER_SECONDS = 120
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.02
_WRITE_RETRY_MAX_S = 0.15

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cluster_configs (
    cluster_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    environment TEXT NOT NULL,
    default_namespace_scope TEXT NOT NULL,
    owner_team TEXT NOT NULL,
    automatic_actions_enabled INTEGER NOT NULL,
    openobserve_config_ref TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_runtime (
    cluster_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    connector_status TEXT NOT NULL,
    openobserve_status TEXT NOT NULL,
    scope_field_mapping_health TEXT NOT NULL,
    recent_query_health TEXT NOT NULL,
    failure_summary TEXT NOT NULL,
    last_heartbeat REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class ClusterServiceError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "clusters.db"
    return _project_root() / "data" / "clusters.db"


class ClusterDB:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=1.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
            self._conn.close()
            self._conn = None

    def list_clusters(self) -> list[JSON]:
        configs = {row["cluster_id"]: row for row in self._fetchall("SELECT * FROM cluster_configs ORDER BY cluster_id")}
        runtime = {row["cluster_id"]: row for row in self._fetchall("SELECT * FROM cluster_runtime ORDER BY cluster_id")}
        return [self._project(cluster_id, configs.get(cluster_id), runtime.get(cluster_id)) for cluster_id in sorted(set(configs) | set(runtime))]

    def upsert_config(self, payload: JSON, *, actor_id: str, cluster_id: str | None = None) -> JSON:
        _reject_secret_keys(payload)
        normalized = _normalize_config(payload, cluster_id=cluster_id, actor_id=actor_id)
        now = time.time()

        def _write(conn: sqlite3.Connection) -> None:
            exists = conn.execute("SELECT cluster_id FROM cluster_configs WHERE cluster_id = ?", (normalized["cluster_id"],)).fetchone()
            created_at = now
            if exists:
                row = conn.execute("SELECT created_at FROM cluster_configs WHERE cluster_id = ?", (normalized["cluster_id"],)).fetchone()
                created_at = float(row["created_at"])
            conn.execute(
                """
                INSERT OR REPLACE INTO cluster_configs (
                    cluster_id, display_name, environment, default_namespace_scope,
                    owner_team, automatic_actions_enabled, openobserve_config_ref,
                    updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["cluster_id"],
                    normalized["display_name"],
                    normalized["environment"],
                    normalized["default_namespace_scope"],
                    normalized["owner_team"],
                    1 if normalized["automatic_actions_enabled"] else 0,
                    normalized["openobserve_config_ref"],
                    actor_id,
                    created_at,
                    now,
                ),
            )

        self._execute_write(_write)
        return self.get_cluster(normalized["cluster_id"])

    def get_cluster(self, cluster_id: str) -> JSON:
        cluster = _clean_id(cluster_id)
        config = self._fetchone("SELECT * FROM cluster_configs WHERE cluster_id = ?", (cluster,))
        runtime = self._fetchone("SELECT * FROM cluster_runtime WHERE cluster_id = ?", (cluster,))
        if config is None and runtime is None:
            raise ClusterServiceError("not_found", "cluster not found", status=HTTPStatus.NOT_FOUND)
        return self._project(cluster, config, runtime)

    def report_runtime(self, payload: JSON) -> JSON:
        cluster_id = _clean_id(payload.get("cluster_id") or payload.get("cluster"))
        if not cluster_id:
            raise ClusterServiceError("invalid_request", "cluster_id is required")
        connector_id = _clean_id(payload.get("connector_id")) or "gateway"
        now = time.time()
        runtime = {
            "cluster_id": cluster_id,
            "connector_id": connector_id,
            "connector_status": _health(payload.get("connector_status") or "online"),
            "openobserve_status": _health(payload.get("openobserve_status") or "unknown"),
            "scope_field_mapping_health": _health(payload.get("scope_field_mapping_health") or "unknown"),
            "recent_query_health": _health(payload.get("recent_query_health") or "unknown"),
            "failure_summary": _safe_summary(payload.get("failure_summary")),
            "last_heartbeat": _float(payload.get("last_heartbeat"), default=now),
            "updated_at": now,
        }

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO cluster_runtime (
                    cluster_id, connector_id, connector_status, openobserve_status,
                    scope_field_mapping_health, recent_query_health, failure_summary,
                    last_heartbeat, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runtime["cluster_id"],
                    runtime["connector_id"],
                    runtime["connector_status"],
                    runtime["openobserve_status"],
                    runtime["scope_field_mapping_health"],
                    runtime["recent_query_health"],
                    runtime["failure_summary"],
                    runtime["last_heartbeat"],
                    runtime["updated_at"],
                ),
            )

        self._execute_write(_write)
        return self.get_cluster(cluster_id)

    def mutation_disabled_reason(self, cluster_id: str) -> str | None:
        cluster = self.get_cluster(cluster_id)
        if cluster["configuration_status"] != "configured":
            return None
        if cluster["runtime_state"]["connector_status"] != "online":
            return "connector_offline"
        return None

    def _project(self, cluster_id: str, config: JSON | None, runtime: JSON | None) -> JSON:
        configured = config is not None
        runtime_state = _runtime_state(runtime)
        if configured and runtime_state["connector_status"] == "unknown":
            runtime_state["connector_status"] = "offline"
            runtime_state["failure_summary"] = runtime_state["failure_summary"] or "no connector heartbeat"
        mutation_disabled_reason = None
        if configured and runtime_state["connector_status"] != "online":
            mutation_disabled_reason = "connector_offline"
        return {
            "cluster_id": cluster_id,
            "display_name": str((config or {}).get("display_name") or cluster_id),
            "environment": str((config or {}).get("environment") or "prod"),
            "effective_environment": str((config or {}).get("environment") or "prod"),
            "default_namespace_scope": str((config or {}).get("default_namespace_scope") or ""),
            "owner_team": str((config or {}).get("owner_team") or ""),
            "automatic_actions_enabled": bool((config or {}).get("automatic_actions_enabled")) if configured else False,
            "openobserve_config_ref": str((config or {}).get("openobserve_config_ref") or ""),
            "configuration_status": "configured" if configured else "unconfigured",
            "mutation_enabled": configured and mutation_disabled_reason is None,
            "mutation_disabled_reason": mutation_disabled_reason,
            "runtime_state": runtime_state,
            "updated_at": (config or runtime or {}).get("updated_at"),
        }

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        last_err: Exception | None = None
        for attempt in range(_WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    if self._conn is None:
                        raise sqlite3.ProgrammingError("database connection is closed")
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        self._conn.rollback()
                        raise
                return result
            except sqlite3.OperationalError as exc:
                if ("locked" in str(exc).lower() or "busy" in str(exc).lower()) and attempt < _WRITE_MAX_RETRIES - 1:
                    last_err = exc
                    time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))
                    continue
                raise
        raise last_err or sqlite3.OperationalError("database is locked after max retries")

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> JSON | None:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("database connection is closed")
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[JSON]:
        with self._lock:
            if self._conn is None:
                raise sqlite3.ProgrammingError("database connection is closed")
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def list_clusters() -> list[JSON]:
    return _DB.list_clusters()


def upsert_config(payload: JSON, *, actor_id: str, cluster_id: str | None = None) -> JSON:
    return _DB.upsert_config(payload, actor_id=actor_id, cluster_id=cluster_id)


def report_runtime(payload: JSON) -> JSON:
    return _DB.report_runtime(payload)


def mutation_disabled_reason(cluster_id: str) -> str | None:
    return _DB.mutation_disabled_reason(cluster_id)


def _normalize_config(payload: JSON, *, cluster_id: str | None, actor_id: str) -> JSON:
    cluster = _clean_id(cluster_id or payload.get("cluster_id") or payload.get("id") or payload.get("cluster"))
    if not cluster:
        raise ClusterServiceError("invalid_request", "cluster_id is required")
    openobserve_config_ref = str(payload.get("openobserve_config_ref") or "").strip()
    if _looks_like_url_or_path(openobserve_config_ref):
        raise ClusterServiceError("secret_field_forbidden", "OpenObserve config reference must not be a URL or path")
    return {
        "cluster_id": cluster,
        "display_name": str(payload.get("display_name") or cluster).strip() or cluster,
        "environment": _environment(payload.get("environment")),
        "default_namespace_scope": str(payload.get("default_namespace_scope") or "").strip(),
        "owner_team": str(payload.get("owner_team") or "").strip(),
        "automatic_actions_enabled": bool(payload.get("automatic_actions_enabled")),
        "openobserve_config_ref": openobserve_config_ref,
        "updated_by": actor_id,
    }


def _runtime_state(runtime: JSON | None) -> JSON:
    if runtime is None:
        return {
            "connector_id": "",
            "connector_status": "unknown",
            "openobserve_status": "unknown",
            "scope_field_mapping_health": "unknown",
            "recent_query_health": "unknown",
            "failure_summary": "",
            "last_heartbeat": None,
            "updated_at": None,
        }
    state = dict(runtime)
    if time.time() - float(state.get("last_heartbeat") or 0) > STALE_AFTER_SECONDS:
        state["connector_status"] = "offline"
        state["failure_summary"] = state.get("failure_summary") or "stale connector heartbeat"
    return state


def _reject_secret_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"url", "path"} or any(marker in lowered for marker in SECRET_KEYS) or lowered.endswith("_url") or lowered.endswith("_path"):
                raise ClusterServiceError("secret_field_forbidden", "secret-like cluster fields are not editable or visible")
            _reject_secret_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_keys(item)


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _environment(value: Any) -> str:
    env = str(value or "prod").strip().lower()
    if env not in ENVIRONMENTS:
        raise ClusterServiceError("invalid_environment", "environment must be prod, staging, dev, or test")
    return env


def _health(value: Any) -> str:
    text = str(value or "unknown").strip().lower().replace(" ", "_")
    return text if text in {"online", "offline", "degraded", "ok", "failed", "unknown", "healthy", "unhealthy"} else "unknown"


def _safe_summary(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in ("token", "secret", "password", "authorization", "api-key", "database", "internal_url", "url", "://", "_path")):
        return "[redacted]"
    return text[:240]


def _looks_like_url_or_path(value: str) -> bool:
    lowered = value.lower()
    return bool(value) and ("://" in lowered or lowered.startswith(("/", "\\")) or "/var/" in lowered or "\\\\" in value)


def _float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_DB = ClusterDB()
