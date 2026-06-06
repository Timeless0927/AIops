"""Service catalog and topology store for V1 weak-dependency evidence."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - hermes image fallback
    try:
        from hermes_agent.tools.registry import registry  # type: ignore
    except ImportError:  # pragma: no cover - local tests without Hermes package
        class _NoopRegistry:
            def register(self, **_: Any) -> None:
                return None

        registry = _NoopRegistry()


SERVICE_ID_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
K8S_DNS_RE = re.compile(
    r"\b(?P<service>[a-z0-9]([-a-z0-9]*[a-z0-9])?)"
    r"\.(?P<namespace>[a-z0-9]([-a-z0-9]*[a-z0-9])?)"
    r"(?:\.svc(?:\.cluster\.local)?)?\b"
)
DEFAULT_STALE_AFTER_SECONDS = 300


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "topology.db"
    return _project_root() / "data" / "topology.db"


def _now() -> float:
    return time.time()


def _normalize_part(value: str | None, *, default: str | None = None) -> str:
    normalized = (value or default or "").strip().lower()
    normalized = normalized.replace("_", "-")
    normalized = re.sub(r"[^a-z0-9.-]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-.")
    return normalized


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


@dataclass(frozen=True)
class ServiceIdentity:
    cluster_id: str
    namespace: str
    service: str
    warnings: tuple[str, ...] = ()

    @property
    def service_id(self) -> str:
        return f"{self.cluster_id}/{self.namespace}/{self.service}"


@dataclass(frozen=True)
class ServiceRecord:
    cluster_id: str
    namespace: str
    service: str
    service_category: str = "unknown"
    workload_kind: str | None = None
    workload_name: str | None = None
    app_label: str | None = None
    identity_warnings: tuple[str, ...] = ()
    source: str = "manual"
    observed_at: float = field(default_factory=_now)


@dataclass(frozen=True)
class ServiceEdge:
    from_cluster_id: str
    from_namespace: str
    from_service: str
    to_cluster_id: str
    to_namespace: str
    to_service: str
    edge_type: str
    source: str
    confidence: float
    warnings: tuple[str, ...] = ()
    observed_at: float = field(default_factory=_now)


@dataclass(frozen=True)
class KubernetesWorkload:
    kind: str
    name: str
    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    config_map_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class KubernetesService:
    name: str
    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    selector: dict[str, str] = field(default_factory=dict)
    service_type: str = "ClusterIP"


@dataclass(frozen=True)
class KubernetesConfigMap:
    name: str
    namespace: str
    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class KubernetesInventory:
    cluster_id: str
    services: tuple[KubernetesService, ...] = ()
    workloads: tuple[KubernetesWorkload, ...] = ()
    config_maps: tuple[KubernetesConfigMap, ...] = ()

    @classmethod
    def from_kubernetes_client(
        cls,
        cluster_id: str,
        *,
        namespace: str | None = None,
        kube_client: Any | None = None,
        apps_client: Any | None = None,
    ) -> "KubernetesInventory":
        try:
            if kube_client is None or apps_client is None:
                from kubernetes import client, config  # type: ignore

                config.load_incluster_config()
                kube_client = client.CoreV1Api()
                apps_client = client.AppsV1Api()
        except Exception as exc:
            raise RuntimeError(f"backend_unavailable: {exc}") from exc

        if namespace:
            svc_items = kube_client.list_namespaced_service(namespace).items
            cm_items = kube_client.list_namespaced_config_map(namespace).items
            deployments = apps_client.list_namespaced_deployment(namespace).items
            statefulsets = apps_client.list_namespaced_stateful_set(namespace).items
        else:
            svc_items = kube_client.list_service_for_all_namespaces().items
            cm_items = kube_client.list_config_map_for_all_namespaces().items
            deployments = apps_client.list_deployment_for_all_namespaces().items
            statefulsets = apps_client.list_stateful_set_for_all_namespaces().items

        services = tuple(_service_from_k8s_item(item) for item in svc_items)
        config_maps = tuple(_config_map_from_k8s_item(item) for item in cm_items)
        workloads = tuple(
            [_workload_from_k8s_item("Deployment", item) for item in deployments]
            + [_workload_from_k8s_item("StatefulSet", item) for item in statefulsets]
        )
        return cls(cluster_id=cluster_id, services=services, workloads=workloads, config_maps=config_maps)


def normalize_service_identity(
    cluster_id: str | None,
    namespace: str | None,
    service: str | None,
) -> ServiceIdentity:
    warnings: list[str] = []
    normalized_cluster = _normalize_part(cluster_id)
    normalized_namespace = _normalize_part(namespace)
    normalized_service = _normalize_part(service)

    if not normalized_cluster:
        normalized_cluster = "default"
        warnings.append("cluster_id_missing")
    if not normalized_namespace:
        normalized_namespace = "default"
        warnings.append("namespace_missing")
    if not normalized_service:
        normalized_service = "unknown-service"
        warnings.append("service_missing")

    for label, original, normalized in (
        ("cluster_id", cluster_id, normalized_cluster),
        ("namespace", namespace, normalized_namespace),
        ("service", service, normalized_service),
    ):
        if original and original.strip() != normalized:
            warnings.append(f"{label}_normalized")

    if not SERVICE_ID_RE.match(normalized_service):
        warnings.append("service_identity_invalid")

    return ServiceIdentity(
        cluster_id=normalized_cluster,
        namespace=normalized_namespace,
        service=normalized_service,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _service_key(cluster_id: str, namespace: str, service: str) -> tuple[str, str, str]:
    identity = normalize_service_identity(cluster_id, namespace, service)
    return identity.cluster_id, identity.namespace, identity.service


def _workload_from_k8s_item(kind: str, item: Any) -> KubernetesWorkload:
    metadata = getattr(item, "metadata", None)
    spec = getattr(item, "spec", None)
    template = getattr(spec, "template", None)
    pod_spec = getattr(template, "spec", None)
    labels = dict(getattr(metadata, "labels", None) or {})
    env: dict[str, str] = {}
    config_map_refs: list[str] = []

    for container in getattr(pod_spec, "containers", None) or []:
        for env_var in getattr(container, "env", None) or []:
            name = getattr(env_var, "name", None)
            value = getattr(env_var, "value", None)
            if name and value:
                env[name] = value
        for env_from in getattr(container, "env_from", None) or []:
            cm_ref = getattr(env_from, "config_map_ref", None)
            cm_name = getattr(cm_ref, "name", None)
            if cm_name:
                config_map_refs.append(cm_name)

    return KubernetesWorkload(
        kind=kind,
        name=getattr(metadata, "name", ""),
        namespace=getattr(metadata, "namespace", "default"),
        labels=labels,
        env=env,
        config_map_refs=tuple(config_map_refs),
    )


def _service_from_k8s_item(item: Any) -> KubernetesService:
    metadata = getattr(item, "metadata", None)
    spec = getattr(item, "spec", None)
    return KubernetesService(
        name=getattr(metadata, "name", ""),
        namespace=getattr(metadata, "namespace", "default"),
        labels=dict(getattr(metadata, "labels", None) or {}),
        selector=dict(getattr(spec, "selector", None) or {}),
        service_type=getattr(spec, "type", "ClusterIP") or "ClusterIP",
    )


def _config_map_from_k8s_item(item: Any) -> KubernetesConfigMap:
    metadata = getattr(item, "metadata", None)
    return KubernetesConfigMap(
        name=getattr(metadata, "name", ""),
        namespace=getattr(metadata, "namespace", "default"),
        data=dict(getattr(item, "data", None) or {}),
    )


class TopologyStore:
    """SQLite store for service catalog records and weak topology edges."""

    def __init__(self, db_path: Path | None = None, *, stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS) -> None:
        self.db_path = db_path or _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.stale_after_seconds = stale_after_seconds
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS services (
                cluster_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                service TEXT NOT NULL,
                service_category TEXT NOT NULL,
                workload_kind TEXT,
                workload_name TEXT,
                app_label TEXT,
                identity_warnings_json TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (cluster_id, namespace, service)
            );

            CREATE TABLE IF NOT EXISTS service_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_cluster_id TEXT NOT NULL,
                from_namespace TEXT NOT NULL,
                from_service TEXT NOT NULL,
                to_cluster_id TEXT NOT NULL,
                to_namespace TEXT NOT NULL,
                to_service TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL,
                warnings_json TEXT NOT NULL,
                observed_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (
                    from_cluster_id,
                    from_namespace,
                    from_service,
                    to_cluster_id,
                    to_namespace,
                    to_service,
                    edge_type,
                    source
                )
            );

            CREATE INDEX IF NOT EXISTS idx_service_edges_from
                ON service_edges(from_cluster_id, from_namespace, from_service);
            CREATE INDEX IF NOT EXISTS idx_service_edges_to
                ON service_edges(to_cluster_id, to_namespace, to_service);
            """
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> "TopologyStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def upsert_service(self, record: ServiceRecord) -> ServiceIdentity:
        identity = normalize_service_identity(record.cluster_id, record.namespace, record.service)
        warnings = tuple(dict.fromkeys([*identity.warnings, *record.identity_warnings]))
        now_ts = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO services (
                    cluster_id, namespace, service, service_category,
                    workload_kind, workload_name, app_label,
                    identity_warnings_json, source, observed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cluster_id, namespace, service) DO UPDATE SET
                    service_category=excluded.service_category,
                    workload_kind=excluded.workload_kind,
                    workload_name=excluded.workload_name,
                    app_label=excluded.app_label,
                    identity_warnings_json=excluded.identity_warnings_json,
                    source=excluded.source,
                    observed_at=excluded.observed_at,
                    updated_at=excluded.updated_at
                """,
                (
                    identity.cluster_id,
                    identity.namespace,
                    identity.service,
                    record.service_category,
                    record.workload_kind,
                    record.workload_name,
                    record.app_label,
                    _json_dumps(list(warnings)),
                    record.source,
                    record.observed_at,
                    now_ts,
                ),
            )
        return identity

    def add_edge(self, edge: ServiceEdge) -> None:
        from_identity = normalize_service_identity(edge.from_cluster_id, edge.from_namespace, edge.from_service)
        to_identity = normalize_service_identity(edge.to_cluster_id, edge.to_namespace, edge.to_service)
        warnings = tuple(dict.fromkeys([*from_identity.warnings, *to_identity.warnings, *edge.warnings]))
        confidence = max(0.0, min(1.0, edge.confidence))
        now_ts = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO service_edges (
                    from_cluster_id, from_namespace, from_service,
                    to_cluster_id, to_namespace, to_service,
                    edge_type, source, confidence, warnings_json, observed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    from_cluster_id, from_namespace, from_service,
                    to_cluster_id, to_namespace, to_service, edge_type, source
                ) DO UPDATE SET
                    confidence=excluded.confidence,
                    warnings_json=excluded.warnings_json,
                    observed_at=excluded.observed_at,
                    updated_at=excluded.updated_at
                """,
                (
                    from_identity.cluster_id,
                    from_identity.namespace,
                    from_identity.service,
                    to_identity.cluster_id,
                    to_identity.namespace,
                    to_identity.service,
                    edge.edge_type,
                    edge.source,
                    confidence,
                    _json_dumps(list(warnings)),
                    edge.observed_at,
                    now_ts,
                ),
            )

    def ingest_kubernetes_inventory(self, inventory: KubernetesInventory) -> None:
        workload_by_app = self._workloads_by_app_label(inventory.workloads)
        config_maps = {(cm.namespace, cm.name): cm for cm in inventory.config_maps}

        for service in inventory.services:
            identity = normalize_service_identity(inventory.cluster_id, service.namespace, service.name)
            app_label = self._service_app_label(service)
            matched_workload = workload_by_app.get((service.namespace, app_label or ""))
            warnings: list[str] = list(identity.warnings)

            if app_label and not matched_workload:
                warnings.append("service_app_label_without_matching_workload")
            if matched_workload and app_label and self._workload_app_label(matched_workload) != app_label:
                warnings.append("service_workload_app_label_mismatch")

            self.upsert_service(
                ServiceRecord(
                    cluster_id=inventory.cluster_id,
                    namespace=service.namespace,
                    service=service.name,
                    service_category=service.service_type.lower(),
                    workload_kind=matched_workload.kind if matched_workload else None,
                    workload_name=matched_workload.name if matched_workload else None,
                    app_label=app_label,
                    identity_warnings=tuple(warnings),
                    source="kubernetes",
                )
            )

        service_names = {(svc.namespace, svc.name) for svc in inventory.services}
        for workload in inventory.workloads:
            from_service = self._service_name_for_workload(workload, inventory.services)
            if not from_service:
                continue
            for target in self._targets_from_workload_config(workload, config_maps, service_names):
                self.add_edge(
                    ServiceEdge(
                        from_cluster_id=inventory.cluster_id,
                        from_namespace=workload.namespace,
                        from_service=from_service,
                        to_cluster_id=inventory.cluster_id,
                        to_namespace=target[0],
                        to_service=target[1],
                        edge_type="depends_on",
                        source="k8s_config",
                        confidence=0.35,
                        warnings=("weak_evidence_config_reference",),
                    )
                )

    def get_service_topology(self, cluster_id: str, namespace: str, service: str) -> dict[str, Any]:
        identity = normalize_service_identity(cluster_id, namespace, service)
        service_row = self._conn.execute(
            """
            SELECT * FROM services
            WHERE cluster_id = ? AND namespace = ? AND service = ?
            """,
            (identity.cluster_id, identity.namespace, identity.service),
        ).fetchone()
        outgoing = self._conn.execute(
            """
            SELECT * FROM service_edges
            WHERE from_cluster_id = ? AND from_namespace = ? AND from_service = ?
            ORDER BY to_namespace, to_service, edge_type, source
            """,
            (identity.cluster_id, identity.namespace, identity.service),
        ).fetchall()
        incoming = self._conn.execute(
            """
            SELECT * FROM service_edges
            WHERE to_cluster_id = ? AND to_namespace = ? AND to_service = ?
            ORDER BY from_namespace, from_service, edge_type, source
            """,
            (identity.cluster_id, identity.namespace, identity.service),
        ).fetchall()

        warnings = list(identity.warnings)
        if service_row is None:
            warnings.append("service_not_found")

        return {
            "ok": True,
            "service": self._serialize_service(service_row, identity),
            "edges": {
                "outgoing": [self._serialize_edge(row, direction="outgoing") for row in outgoing],
                "incoming": [self._serialize_edge(row, direction="incoming") for row in incoming],
            },
            "freshness": self._freshness(service_row, [*outgoing, *incoming]),
            "warnings": tuple(dict.fromkeys(warnings)),
        }

    def _serialize_service(self, row: sqlite3.Row | None, identity: ServiceIdentity) -> dict[str, Any]:
        if row is None:
            return {
                "cluster_id": identity.cluster_id,
                "namespace": identity.namespace,
                "service": identity.service,
                "service_id": identity.service_id,
                "found": False,
            }

        return {
            "cluster_id": row["cluster_id"],
            "namespace": row["namespace"],
            "service": row["service"],
            "service_id": f"{row['cluster_id']}/{row['namespace']}/{row['service']}",
            "found": True,
            "service_category": row["service_category"],
            "workload_kind": row["workload_kind"],
            "workload_name": row["workload_name"],
            "app_label": row["app_label"],
            "source": row["source"],
            "identity_warnings": _json_loads(row["identity_warnings_json"], []),
        }

    def _serialize_edge(self, row: sqlite3.Row, *, direction: str) -> dict[str, Any]:
        observed_at = float(row["observed_at"])
        stale = (_now() - observed_at) > self.stale_after_seconds
        return {
            "direction": direction,
            "from": {
                "cluster_id": row["from_cluster_id"],
                "namespace": row["from_namespace"],
                "service": row["from_service"],
            },
            "to": {
                "cluster_id": row["to_cluster_id"],
                "namespace": row["to_namespace"],
                "service": row["to_service"],
            },
            "edge_type": row["edge_type"],
            "source": row["source"],
            "confidence": row["confidence"],
            "freshness": {
                "observed_at": observed_at,
                "stale": stale,
                "stale_after_seconds": self.stale_after_seconds,
            },
            "warnings": _json_loads(row["warnings_json"], []),
        }

    def _freshness_for_timestamp(self, observed_at: float | None) -> dict[str, Any]:
        stale = observed_at is None or (_now() - observed_at) > self.stale_after_seconds
        return {
            "observed_at": observed_at,
            "stale": stale,
            "stale_after_seconds": self.stale_after_seconds,
        }

    def _freshness(self, service_row: sqlite3.Row | None, edges: list[sqlite3.Row]) -> dict[str, Any]:
        service_observed_at = float(service_row["observed_at"]) if service_row is not None else None
        service_freshness = self._freshness_for_timestamp(service_observed_at)
        edge_timestamps = [float(edge["observed_at"]) for edge in edges]
        edge_freshness = [
            self._freshness_for_timestamp(observed_at)
            for observed_at in edge_timestamps
        ]
        return {
            "observed_at": service_freshness["observed_at"],
            "stale": service_freshness["stale"],
            "stale_after_seconds": self.stale_after_seconds,
            "service": service_freshness,
            "edges": {
                "count": len(edges),
                "latest_observed_at": max(edge_timestamps) if edge_timestamps else None,
                "stale": any(item["stale"] for item in edge_freshness) if edge_freshness else None,
            },
        }

    @staticmethod
    def _app_label(labels: dict[str, str]) -> str | None:
        return labels.get("app") or labels.get("app.kubernetes.io/name")

    @classmethod
    def _workload_app_label(cls, workload: KubernetesWorkload) -> str | None:
        return cls._app_label(workload.labels)

    @classmethod
    def _service_app_label(cls, service: KubernetesService) -> str | None:
        return cls._app_label(service.selector) or cls._app_label(service.labels)

    @staticmethod
    def _workloads_by_app_label(workloads: Iterable[KubernetesWorkload]) -> dict[tuple[str, str], KubernetesWorkload]:
        mapping: dict[tuple[str, str], KubernetesWorkload] = {}
        for workload in workloads:
            app_label = TopologyStore._workload_app_label(workload)
            if app_label:
                mapping[(workload.namespace, app_label)] = workload
        return mapping

    @staticmethod
    def _service_name_for_workload(
        workload: KubernetesWorkload,
        services: Iterable[KubernetesService],
    ) -> str | None:
        workload_app = TopologyStore._workload_app_label(workload)
        for service in services:
            service_app = TopologyStore._service_app_label(service)
            if service.namespace == workload.namespace and workload_app and service_app == workload_app:
                return service.name
        return None

    @staticmethod
    def _targets_from_workload_config(
        workload: KubernetesWorkload,
        config_maps: dict[tuple[str, str], KubernetesConfigMap],
        service_names: set[tuple[str, str]],
    ) -> set[tuple[str, str]]:
        candidates: set[str] = set()
        candidates.update(workload.env.values())
        for cm_ref in workload.config_map_refs:
            config_map = config_maps.get((workload.namespace, cm_ref))
            if config_map:
                candidates.update(config_map.data.values())

        targets: set[tuple[str, str]] = set()
        for value in candidates:
            explicit_targets = TopologyStore._explicit_k8s_dns_targets(value, service_names)
            targets.update(explicit_targets)
            targets.update(
                TopologyStore._same_namespace_short_name_targets(
                    value,
                    workload.namespace,
                    service_names,
                    explicit_targets,
                )
            )
        return targets

    @staticmethod
    def _explicit_k8s_dns_targets(value: str, service_names: set[tuple[str, str]]) -> set[tuple[str, str]]:
        targets: set[tuple[str, str]] = set()
        for match in K8S_DNS_RE.finditer(value):
            target = (match.group("namespace"), match.group("service"))
            if target in service_names:
                targets.add(target)
        return targets

    @staticmethod
    def _same_namespace_short_name_targets(
        value: str,
        workload_namespace: str,
        service_names: set[tuple[str, str]],
        explicit_targets: set[tuple[str, str]],
    ) -> set[tuple[str, str]]:
        targets: set[tuple[str, str]] = set()
        explicit_services = {service for _, service in explicit_targets}
        for namespace, service in service_names:
            if namespace != workload_namespace or service in explicit_services:
                continue
            if re.search(rf"\b{re.escape(service)}\b", value):
                targets.add((namespace, service))
        return targets


def get_service_topology(
    cluster_id: str,
    namespace: str,
    service: str,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    with TopologyStore(db_path) as store:
        return store.get_service_topology(cluster_id, namespace, service)


def check_topology_store_requirements() -> bool:
    return True


TOPOLOGY_SCHEMA = {
    "name": "get_service_topology",
    "description": "读取 V1 service catalog 与弱证据 topology store。",
    "parameters": {
        "type": "object",
        "properties": {
            "cluster_id": {"type": "string"},
            "namespace": {"type": "string"},
            "service": {"type": "string"},
        },
        "required": ["cluster_id", "namespace", "service"],
    },
}


def _handler(args: dict[str, Any], **_: Any) -> str:
    result = get_service_topology(
        args.get("cluster_id", ""),
        args.get("namespace", ""),
        args.get("service", ""),
    )
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="get_service_topology",
    toolset="topology",
    schema=TOPOLOGY_SCHEMA,
    handler=_handler,
    check_fn=check_topology_store_requirements,
    is_async=False,
    emoji="🕸️",
    max_result_size_chars=100_000,
)
