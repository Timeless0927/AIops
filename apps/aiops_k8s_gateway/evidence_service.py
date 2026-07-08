"""Gateway-owned evidence query helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

from aiops.domain.identity import Actor, ROLE_ADMIN, ROLE_AUDITOR


JSON = dict[str, Any]

MAX_LIMIT = 100
MAX_RANGE_SECONDS = 6 * 60 * 60
MAX_TIMEOUT_SECONDS = 5.0
OPENOBSERVE_URL_ENV = "AIOPS_OPENOBSERVE_URL"
OPENOBSERVE_TOKEN_ENV = "AIOPS_OPENOBSERVE_TOKEN"
OPENOBSERVE_ORG_ENV = "AIOPS_OPENOBSERVE_ORG"

TEMPLATES = {
    "service_overview": ("metrics", "service:{service} namespace:{namespace}"),
    "error_logs": ("logs", "errors service:{service} namespace:{namespace}"),
    "trace_latency": ("traces", "latency service:{service} namespace:{namespace}"),
    "k8s_state": ("kubernetes", "k8s service:{service} namespace:{namespace}"),
    "topology_dependencies": ("topology", "topology service:{service} namespace:{namespace}"),
    "changes_recent": ("changes", "changes service:{service} namespace:{namespace}"),
    "tool_output": ("tool_output", "tool output service:{service} namespace:{namespace}"),
}
AGENT_TEMPLATES = frozenset({"service_overview", "error_logs", "trace_latency", "k8s_state", "topology_dependencies"})
OPENOBSERVE_KINDS = frozenset({"metrics", "logs", "traces"})
ALL_KINDS = ("metrics", "logs", "traces", "kubernetes", "topology", "changes", "tool_output")
SCOPE_ALIASES = {
    "cluster": ("k8s_cluster_name", "cluster"),
    "namespace": ("k8s_namespace_name", "namespace"),
    "service": ("service_name", "service"),
    "team": ("team",),
}
SECRET_KEY_RE = re.compile(r"(secret|token|password|authorization|api[_-]?key)", re.IGNORECASE)
SECRET_VALUE_RE = re.compile(
    r"(?i)(authorization:\s*bearer\s+)[^\s,;]+|((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+"
)


class EvidenceServiceError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def query_evidence(payload: JSON, *, actor: Actor, request_id: str, agent: bool = False) -> JSON:
    scope = _scope(payload)
    _require_scope(scope, actor)
    limit = _clamp_int(payload.get("limit"), default=20, minimum=1, maximum=MAX_LIMIT)
    timeout = _clamp_float(payload.get("timeout_seconds"), default=2.0, minimum=0.1, maximum=MAX_TIMEOUT_SECONDS)
    start, end = _time_range(payload.get("time_range") if isinstance(payload.get("time_range"), dict) else {})
    template = str(payload.get("template") or "service_overview").strip() or "service_overview"
    advanced_query = str(payload.get("advanced_query") or "").strip()

    if agent and template not in AGENT_TEMPLATES:
        raise EvidenceServiceError("template_forbidden", "agent evidence template is not allowlisted", status=HTTPStatus.FORBIDDEN)
    if advanced_query and not (actor.has_role(ROLE_ADMIN) or actor.has_role(ROLE_AUDITOR)):
        raise EvidenceServiceError("advanced_query_forbidden", "advanced evidence queries require admin or auditor", status=HTTPStatus.FORBIDDEN)
    if not advanced_query and template not in TEMPLATES:
        raise EvidenceServiceError("template_unknown", "unknown evidence template", status=HTTPStatus.BAD_REQUEST)

    query_type = str(payload.get("query_type") or (TEMPLATES.get(template) or ("all", ""))[0]).strip().lower() or "all"
    query_text = advanced_query or _template_query(template, scope)
    kinds = _selected_kinds(query_type)
    oo_sources, oo_status = _openobserve_sources(kinds, query_text, scope, limit, timeout, request_id, actor=actor, start=start, end=end)
    sources = oo_sources + [_compat_source(kind, scope=scope, start=start, end=end) for kind in kinds if kind not in OPENOBSERVE_KINDS]
    status = "ok" if sources and all(source["status"] == "ok" for source in sources) else "partial"
    if oo_status == "unconfigured" and all(source["status"] == "empty" for source in sources):
        status = "partial"

    return {
        "status": status,
        "query": {
            "query_type": query_type,
            "template": template,
            "advanced": bool(advanced_query),
            "agent": agent,
        },
        "scope": scope,
        "limits": {
            "limit": limit,
            "time_range_seconds": int(end - start),
            "timeout_seconds": timeout,
        },
        "sources": sources,
        "nodes": nodes_from_sources(sources, scope=scope, start=start, end=end),
        "redaction": {"applied": True},
        "request_id": request_id,
    }


def _scope(payload: JSON) -> JSON:
    raw = payload.get("scope") if isinstance(payload.get("scope"), dict) else payload
    return {
        "cluster": str(raw.get("cluster") or raw.get("cluster_id") or "").strip(),
        "namespace": str(raw.get("namespace") or "").strip(),
        "service": str(raw.get("service") or raw.get("service_id") or "").strip(),
        "team": str(raw.get("team") or raw.get("team_id") or "").strip(),
        "environment": str(raw.get("environment") or "prod").strip() or "prod",
    }


def _require_scope(scope: JSON, actor: Actor) -> None:
    if actor.has_role(ROLE_ADMIN):
        return
    missing = [key for key in ("cluster", "namespace", "service", "team") if not scope.get(key)]
    if missing:
        raise EvidenceServiceError("scope_required", f"missing evidence scope: {', '.join(missing)}", status=HTTPStatus.BAD_REQUEST)


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _clamp_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _time_range(value: JSON) -> tuple[float, float]:
    now = time.time()
    end_raw = value.get("end_ts", value.get("to"))
    start_raw = value.get("start_ts", value.get("from"))
    end = _clamp_float(_timestamp(end_raw), default=now, minimum=0, maximum=now + 60)
    start = _clamp_float(_timestamp(start_raw), default=end - 3600, minimum=0, maximum=end)
    if end - start > MAX_RANGE_SECONDS:
        start = end - MAX_RANGE_SECONDS
    return start, end


def _timestamp(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _template_query(template: str, scope: JSON) -> str:
    _, template_text = TEMPLATES[template]
    return template_text.format(**{key: scope.get(key) or "*" for key in ("cluster", "namespace", "service", "team", "environment")})


def _selected_kinds(query_type: str) -> tuple[str, ...]:
    if query_type in {"all", "overview"}:
        return ALL_KINDS
    return (query_type,) if query_type in ALL_KINDS else ("metrics", "logs", "traces")


def _openobserve_sources(
    kinds: tuple[str, ...],
    query: str,
    scope: JSON,
    limit: int,
    timeout: float,
    request_id: str,
    *,
    actor: Actor,
    start: float,
    end: float,
) -> tuple[list[JSON], str]:
    oo_kinds = [kind for kind in kinds if kind in OPENOBSERVE_KINDS]
    if not oo_kinds:
        return [], "skipped"
    url = os.getenv(OPENOBSERVE_URL_ENV, "").rstrip("/")
    token = os.getenv(OPENOBSERVE_TOKEN_ENV, "").strip()
    if not url or not token:
        return ([_source(kind, "degraded", "OpenObserve is not configured", [], backend="openobserve", scope=scope, start=start, end=end) for kind in oo_kinds], "unconfigured")
    try:
        rows = _openobserve_request(url, token, query, scope, limit, timeout, request_id, actor=actor, start=start, end=end)
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        return ([_source(kind, "failed", f"OpenObserve unavailable: {type(exc).__name__}", [], backend="openobserve", scope=scope, start=start, end=end) for kind in oo_kinds], "failed")
    return ([_source(kind, "ok", f"OpenObserve returned {len(rows)} scoped row(s)", rows, backend="openobserve", scope=scope, start=start, end=end) for kind in oo_kinds], "ok")


def _openobserve_request(
    url: str,
    token: str,
    query: str,
    scope: JSON,
    limit: int,
    timeout: float,
    request_id: str,
    *,
    actor: Actor,
    start: float,
    end: float,
) -> list[JSON]:
    org = os.getenv(OPENOBSERVE_ORG_ENV, "default").strip() or "default"
    body = json.dumps(
        {
            "query": {
                "sql": query,
                "from": int(start),
                "to": int(end),
                "size": limit,
                "scope": scope,
            },
            "request_id": request_id,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/api/{org}/_search",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    rows = payload.get("hits") or payload.get("rows") or payload.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("hits") or rows.get("rows") or []
    if not isinstance(rows, list):
        rows = []
    return [redact(row) for row in rows if isinstance(row, dict) and _row_allowed(row, scope, actor)][:limit]


def _row_allowed(row: JSON, scope: JSON, actor: Actor) -> bool:
    if actor.has_role(ROLE_ADMIN):
        return True
    row_scope = _row_scope(row)
    for key in ("cluster", "namespace", "service", "team"):
        if not row_scope.get(key) or row_scope[key] != scope.get(key):
            return False
    return True


def _row_scope(row: JSON) -> JSON:
    scope: JSON = {}
    for key, aliases in SCOPE_ALIASES.items():
        scope[key] = next((str(row.get(alias) or "").strip() for alias in aliases if str(row.get(alias) or "").strip()), "")
    return scope


def _compat_source(kind: str, *, scope: JSON, start: float, end: float) -> JSON:
    return _source(kind, "empty", f"{kind} compatibility evidence is not available for this query", [], backend="compatibility", scope=scope, start=start, end=end)


def _source(kind: str, status: str, summary: str, rows: list[JSON], *, backend: str, scope: JSON, start: float, end: float) -> JSON:
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {
        "kind": kind,
        "status": status,
        "summary": redact(summary),
        "backend": backend,
        "refs": [{"ref_id": f"ev_{backend}_{kind}_{digest}", "source": backend}] if rows else [],
        "samples": rows[:5],
        "scope": scope,
        "time_range": {"start_ts": int(start), "end_ts": int(end)},
        "redacted": True,
    }


def nodes_from_sources(sources: list[JSON], *, scope: JSON, start: float, end: float) -> list[JSON]:
    return [_node_from_source(index, source, scope=scope, start=start, end=end) for index, source in enumerate(sources, start=1)]


def nodes_from_records(records: list[JSON], *, scope: JSON, actor: Actor) -> list[JSON]:
    nodes: list[JSON] = []
    for index, record in enumerate(records, start=1):
        if not _record_allowed(record, scope, actor):
            continue
        start = record.get("window_start") or record.get("collected_at") or time.time()
        end = record.get("window_end") or record.get("collected_at") or start
        rows = [redact(record.get("payload") or {})]
        source = _source(
            str(record.get("source_type") or "evidence"),
            "ok",
            str(record.get("summary") or record.get("source_ref") or "Evidence collected"),
            rows,
            backend=str(record.get("source_ref") or "gateway"),
            scope=scope,
            start=float(start),
            end=float(end),
        )
        source["refs"] = [{"ref_id": str(record.get("source_ref") or record.get("id") or f"evidence-{index}"), "source": source["backend"]}]
        nodes.append(_node_from_source(index, source, scope=scope, start=float(start), end=float(end)))
    return nodes


def _record_allowed(record: JSON, scope: JSON, actor: Actor) -> bool:
    if actor.has_role(ROLE_ADMIN):
        return True
    record_scope = _record_scope(record)
    for key in ("cluster", "namespace", "service", "team"):
        if not record_scope.get(key) or record_scope[key] != scope.get(key):
            return False
    return True


def _record_scope(record: JSON) -> JSON:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    embedded = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    scoped = _scope(embedded) if embedded else _row_scope(payload)
    if any(scoped.values()):
        return scoped
    return _row_scope(record)


def _node_from_source(index: int, source: JSON, *, scope: JSON, start: float, end: float) -> JSON:
    samples = source.get("samples") if isinstance(source.get("samples"), list) else []
    refs = source.get("refs") if isinstance(source.get("refs"), list) else []
    kind = str(source.get("kind") or "evidence")
    return {
        "node_id": (refs[0].get("ref_id") if refs and isinstance(refs[0], dict) else f"evidence-{index}"),
        "kind": kind,
        "title": kind.replace("_", " ").title(),
        "status": source.get("status") or "unknown",
        "summary": source.get("summary") or "-",
        "detail": _structured_detail(source),
        "snippet": _snippet(samples, str(source.get("summary") or "")),
        "refs": refs,
        "scope": source.get("scope") if isinstance(source.get("scope"), dict) else scope,
        "time_range": source.get("time_range") if isinstance(source.get("time_range"), dict) else {"start_ts": int(start), "end_ts": int(end)},
        "why_it_mattered": _why_it_mattered(kind, source),
        "presentation": "process_node",
    }


def _structured_detail(source: JSON) -> JSON:
    samples = source.get("samples") if isinstance(source.get("samples"), list) else []
    return {
        "backend": source.get("backend") or "gateway",
        "status": source.get("status") or "unknown",
        "row_count": len(samples),
        "chart": _chart_points(samples),
    }


def _chart_points(samples: list[Any]) -> list[JSON]:
    points: list[JSON] = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            continue
        value = next((item for item in sample.values() if isinstance(item, int | float)), None)
        if value is not None:
            points.append({"x": index, "y": value})
    return points[:20]


def _snippet(samples: list[Any], fallback: str) -> str:
    if samples:
        first = samples[0]
        if isinstance(first, dict):
            text = str(first.get("message") or first.get("body") or first.get("summary") or fallback)
        else:
            text = str(first)
    else:
        text = fallback
    return str(redact(text))[:500]


def _why_it_mattered(kind: str, source: JSON) -> str:
    status = str(source.get("status") or "unknown")
    if status in {"failed", "degraded"}:
        return f"{kind} evidence shows a degraded source, so the investigation should not rely on it alone."
    if status == "empty":
        return f"{kind} evidence had no scoped rows, which narrows the investigation path."
    return f"{kind} evidence was collected inside the requested scope and time range."


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        if str(value.get("kind") or "").lower() == "secret":
            return {
                key: ("[redacted]" if key in {"data", "stringData"} else redact(item))
                for key, item in value.items()
            }
        return {key: ("[redacted]" if SECRET_KEY_RE.search(str(key)) else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE_RE.sub(lambda match: f"{match.group(1) or match.group(2)}[redacted]", value)
    return value
