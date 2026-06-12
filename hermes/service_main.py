"""Smokeable Hermes service boundary for split-image packaging."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse
from urllib import error, request

from apps.service_http import JsonHandler, connectivity_payload, serve
from aiops.contracts import EvidenceRef, ToolEnvelope
from toolsets import incident_store
from toolsets.incident_diagnosis import run_diagnosis_session


_DIAGNOSIS_SESSIONS: dict[str, dict[str, Any]] = {}


class HermesServiceHandler(JsonHandler):
    """Minimal Hermes HTTP surface used by image and compose smoke tests."""

    def do_GET(self) -> None:  # noqa: N802
        session_route = _parse_session_route(self.path)
        if session_route is not None:
            session_id, artifact = session_route
            session = get_session_export(session_id, artifact=artifact)
            if session is None:
                self.write_not_found()
                return
            self.write_json(HTTPStatus.OK, {"service": "hermes", "status": "ok", "session": session})
            return

        if self.path == "/diagnosis/sessions":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": "hermes",
                    "status": "ok",
                    "sessions": list(_DIAGNOSIS_SESSIONS.values()),
                },
            )
            return

        if self.path == "/healthz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": "hermes",
                    "status": "ok",
                    "gateway_url": os.getenv("AIOPS_GATEWAY_URL", ""),
                },
            )
            return

        if self.path in {"/readyz", "/connectivity/gateway"}:
            gateway_url = os.getenv("AIOPS_GATEWAY_URL", "")
            if not gateway_url:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "service": "hermes",
                        "status": "unavailable",
                        "peer": "gateway",
                        "error": "AIOPS_GATEWAY_URL is not set",
                    },
                )
                return
            status, payload = connectivity_payload(
                service="hermes",
                peer_name="gateway",
                peer_url=gateway_url,
            )
            self.write_json(status, payload)
            return

        self.write_not_found()

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/diagnosis/sessions":
            self.write_not_found()
            return

        try:
            payload = self.read_json_body()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.write_json(HTTPStatus.BAD_REQUEST, {"service": "hermes", "status": "invalid", "error": str(exc)})
            return

        status, result = enqueue_diagnosis_session(payload)
        self.write_json(status, result)


def validate_diagnosis_payload(payload: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]] | None:
    """Validate the Gateway handoff payload before starting diagnosis."""
    try:
        incident_id = str(payload["incident_id"]).strip()
        session_id = str(payload["session_id"]).strip()
    except (KeyError, TypeError) as exc:
        return HTTPStatus.BAD_REQUEST, {"service": "hermes", "status": "invalid", "error": str(exc)}
    if not incident_id:
        return HTTPStatus.BAD_REQUEST, {"service": "hermes", "status": "invalid", "error": "incident_id is required"}
    if not session_id:
        return HTTPStatus.BAD_REQUEST, {"service": "hermes", "status": "invalid", "error": "session_id is required"}
    alert = payload.get("alert")
    if alert is not None and not isinstance(alert, dict):
        return HTTPStatus.BAD_REQUEST, {"service": "hermes", "status": "invalid", "error": "alert must be an object"}
    return None


async def start_diagnosis_session(payload: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
    """Run diagnosis for a Gateway handoff and expose the resulting artifacts."""
    invalid = validate_diagnosis_payload(payload)
    if invalid is not None:
        return invalid

    incident = _incident_from_handoff(payload)
    session_id = str(incident["session_id"])
    incident_id = str(incident["incident_id"])
    await _record_timeline_event(
        incident_id,
        "investigate_start",
        "Hermes diagnosis session started",
        {"session_id": session_id, "source": incident.get("source"), "dedup_key": payload.get("dedup_key")},
    )
    session = await run_diagnosis_session(
        incident,
        metrics_adapter=_metrics_adapter,
        logs_adapter=_logs_adapter,
        k8s_read_adapter=_k8s_read_adapter,
        topology_adapter=_topology_adapter,
        incident_store=incident_store,
    )
    _DIAGNOSIS_SESSIONS[session_id] = session
    await _record_timeline_event(
        incident_id,
        "investigate_end",
        f"Hermes diagnosis session completed with status {session['status']}",
        {
            "session_id": session_id,
            "status": session["status"],
            "evidence_refs": _diagnosis_evidence_refs(session),
            "missing_evidence": session.get("missing_evidence", []),
            "diagnosis_summary": session["diagnosis"]["summary"],
        },
    )
    await _record_proposal_event(incident_id, session)
    return HTTPStatus.OK, {"service": "hermes", "status": session["status"], "session": session}


def enqueue_diagnosis_session(payload: dict[str, Any]) -> tuple[HTTPStatus, dict[str, Any]]:
    """Queue diagnosis quickly so Gateway handoff does not wait for tool calls."""
    invalid = validate_diagnosis_payload(payload)
    if invalid is not None:
        return invalid

    incident = _incident_from_handoff(payload)
    record = {
        "incident_id": incident["incident_id"],
        "session_id": incident["session_id"],
        "source": incident["source"],
        "status": "queued",
        "state_transitions": ["queued"],
    }
    _DIAGNOSIS_SESSIONS[str(incident["session_id"])] = record
    thread = threading.Thread(target=_run_background_diagnosis, args=(dict(payload),), daemon=True)
    thread.start()
    return HTTPStatus.ACCEPTED, {"service": "hermes", "status": "queued", "session": record}


def _run_background_diagnosis(payload: dict[str, Any]) -> None:
    session_id = str(payload.get("session_id") or "")
    try:
        status, result = asyncio.run(start_diagnosis_session(payload))
        session = result.get("session") if isinstance(result, dict) else None
        if isinstance(session, dict):
            _DIAGNOSIS_SESSIONS[session_id] = session
        elif status != HTTPStatus.OK:
            _DIAGNOSIS_SESSIONS[session_id] = {
                "incident_id": payload.get("incident_id"),
                "session_id": session_id,
                "status": "failed",
                "state_transitions": ["queued", "failed"],
                "error": result.get("error") if isinstance(result, dict) else "diagnosis failed",
            }
    except Exception as exc:  # pragma: no cover - background guard
        _DIAGNOSIS_SESSIONS[session_id] = {
            "incident_id": payload.get("incident_id"),
            "session_id": session_id,
            "status": "failed",
            "state_transitions": ["queued", "failed"],
            "error": f"{type(exc).__name__}: {exc}",
        }


def get_session_export(session_id: str, *, artifact: str | None = None) -> dict[str, Any] | None:
    """Return a full session or a single export artifact."""
    session = _DIAGNOSIS_SESSIONS.get(session_id)
    if session is None:
        return None
    if artifact in (None, ""):
        return session
    if artifact == "diagnosis":
        if "diagnosis" not in session:
            return None
        return dict(session["diagnosis"])
    if artifact == "markdown":
        if "diagnosis" not in session:
            return None
        return {
            "session_id": session["session_id"],
            "incident_id": session["incident_id"],
            "markdown": session["diagnosis"]["markdown"],
        }
    if artifact == "timeline":
        return {
            "session_id": session["session_id"],
            "incident_id": session["incident_id"],
            "state_transitions": session["state_transitions"],
            "steps": session["steps"],
            "missing_evidence": session["missing_evidence"],
        }
    return None


def _parse_session_route(path: str) -> tuple[str, str | None] | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) not in {3, 4} or parts[:2] != ["diagnosis", "sessions"]:
        return None
    artifact = parts[3] if len(parts) == 4 else None
    if artifact not in {None, "diagnosis", "markdown", "timeline"}:
        return None
    return parts[2], artifact


def _incident_from_handoff(payload: dict[str, Any]) -> dict[str, Any]:
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else {}
    description = str(alert.get("description") or alert.get("summary") or "")
    service = str(
        alert.get("service")
        or alert.get("workload_name")
        or alert.get("deployment")
        or alert.get("app")
        or ""
    )
    return {
        "incident_id": str(payload["incident_id"]),
        "session_id": str(payload["session_id"]),
        "source": str(payload.get("source") or "gateway"),
        "alert_name": str(alert.get("alertname") or payload.get("dedup_key") or "alertmanager alert"),
        "summary": description or str(alert.get("alertname") or "Alertmanager firing"),
        "namespace": str(alert.get("namespace") or "default"),
        "cluster": str(alert.get("cluster") or "default"),
        "service": service,
        "app": service,
        "severity": str(alert.get("severity") or "info"),
        "dedup_key": payload.get("dedup_key"),
        "dedup_key_version": payload.get("dedup_key_version"),
    }


async def _record_timeline_event(
    incident_id: str,
    event_type: str,
    output_summary: str,
    metadata: dict[str, Any],
) -> None:
    try:
        await incident_store.add_event(
            incident_id,
            event_type,
            "aiops_hermes",
            "diagnosis runtime",
            output_summary,
            metadata,
        )
    except ValueError:
        return


async def _record_proposal_event(incident_id: str, session: dict[str, Any]) -> None:
    proposals = [action for action in session.get("action_proposals", []) if action.get("approval_required")]
    if not proposals:
        return
    await _record_timeline_event(
        incident_id,
        "remediate_proposed",
        "Hermes produced approval_required action proposal without executing mutation",
        {
            "session_id": session["session_id"],
            "approval_required": True,
            "execute_automatically": False,
            "action_proposals": proposals,
        },
    )


def _diagnosis_evidence_refs(session: dict[str, Any]) -> list[str]:
    diagnosis = session.get("diagnosis") or {}
    return [str(item["source_ref"]) for item in diagnosis.get("evidence_chain", []) if item.get("source_ref")]


async def _metrics_adapter(args: dict[str, Any]) -> ToolEnvelope:
    args = _with_iso8601_metrics_window(args)
    mcp_url = os.getenv("AIOPS_PROMETHEUS_MCP_URL", "").strip()
    if mcp_url:
        return await _http_tool_adapter(
            args,
            url=f"{mcp_url.rstrip('/')}/query_metrics",
            tool_name="query_metrics",
            fallback_source="prometheus",
        )
    return await _synthetic_metrics_adapter(args)


async def _logs_adapter(args: dict[str, Any]) -> ToolEnvelope:
    mcp_url = os.getenv("AIOPS_LOKI_MCP_URL", "").strip()
    if mcp_url:
        return await _http_tool_adapter(
            args,
            url=f"{mcp_url.rstrip('/')}/query_logs",
            tool_name="query_logs",
            fallback_source="loki",
        )
    return await _synthetic_logs_adapter(args)


async def _k8s_read_adapter(args: dict[str, Any]) -> ToolEnvelope:
    gateway_url = os.getenv("AIOPS_GATEWAY_URL", "").strip()
    if gateway_url:
        payload = _gateway_read_payload(args)
        return await _http_tool_adapter(
            payload,
            url=f"{gateway_url.rstrip('/')}/k8s/read",
            tool_name="run_k8s_read",
            fallback_source="k8s_read",
        )
    return await _synthetic_k8s_read_adapter(args)


async def _topology_adapter(args: dict[str, Any]) -> ToolEnvelope:
    mcp_url = os.getenv("AIOPS_TOPOLOGY_MCP_URL", "").strip()
    if mcp_url:
        return await _http_tool_adapter(
            args,
            url=f"{mcp_url.rstrip('/')}/get_service_topology",
            tool_name="get_service_topology",
            fallback_source="topology",
        )
    return ToolEnvelope(
        request_id=str(args.get("request_id") or "get_service_topology"),
        tool_name="get_service_topology",
        status="partial",
        summary="Topology MCP URL is not configured",
        correlation_id=_correlation_id(args),
        data={},
        audit={
            "status": "partial",
            "tool_name": "get_service_topology",
            "missing_reason": "AIOPS_TOPOLOGY_MCP_URL is not set",
            "source": "topology",
        },
    )


async def _http_tool_adapter(
    args: dict[str, Any],
    *,
    url: str,
    tool_name: str,
    fallback_source: str,
) -> ToolEnvelope:
    try:
        data = await asyncio.to_thread(_post_json, url, args, _adapter_timeout())
    except (OSError, TimeoutError, error.URLError, json.JSONDecodeError, ValueError) as exc:
        return _failed_tool_envelope(args, tool_name=tool_name, source=fallback_source, message=str(exc))
    if not isinstance(data, dict):
        return _failed_tool_envelope(
            args,
            tool_name=tool_name,
            source=fallback_source,
            message="tool HTTP response was not a JSON object",
        )
    return _tool_envelope_from_mapping(data, args=args, tool_name=tool_name, source=fallback_source)


def _post_json(target: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    req = request.Request(
        target,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("response body must be a JSON object")
        return data


def _adapter_timeout() -> float:
    try:
        return max(0.1, float(os.getenv("AIOPS_HERMES_TOOL_TIMEOUT_SECONDS", "3")))
    except ValueError:
        return 3.0


def _gateway_read_payload(args: dict[str, Any]) -> dict[str, Any]:
    argv = args.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
        argv = _default_k8s_read_argv(args)
    return {
        "cluster_id": args.get("cluster_id") or "",
        "namespace": args.get("namespace") or "",
        "argv": argv,
        "reason": args.get("reason"),
        "task_id": str(args.get("request_id") or "diagnosis-k8s-read").replace(":", "-"),
        "command_id": f"cmd-{_stable_digest(args)[:12]}",
    }


def _default_k8s_read_argv(args: dict[str, Any]) -> list[str]:
    argv = ["kubectl", "get", "pods"]
    namespace = str(args.get("namespace") or "").strip()
    service = str(args.get("service") or "").strip()
    if namespace:
        argv.extend(["-n", namespace])
    if service:
        argv.extend(["-l", f"app={service}"])
    return argv


def _with_iso8601_metrics_window(args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    if not _looks_iso8601(str(normalized.get("start") or "")) or not _looks_iso8601(str(normalized.get("end") or "")):
        end = datetime.now(UTC).replace(microsecond=0)
        start = end - timedelta(minutes=30)
        normalized["start"] = _format_iso8601_z(start)
        normalized["end"] = _format_iso8601_z(end)
    return normalized


def _looks_iso8601(value: str) -> bool:
    if not value:
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return True


def _format_iso8601_z(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _tool_envelope_from_mapping(
    data: dict[str, Any],
    *,
    args: dict[str, Any],
    tool_name: str,
    source: str,
) -> ToolEnvelope:
    refs = tuple(_evidence_ref_from_mapping(item, source=source, args=args) for item in data.get("evidence_refs") or ())
    ref = data.get("result_ref") or data.get("ref")
    if not refs and ref:
        refs = (_evidence_ref_from_mapping({"ref_id": ref}, source=source, args=args),)
    return ToolEnvelope(
        request_id=str(data.get("request_id") or args.get("request_id") or args.get("task_id") or ""),
        tool_name=str(data.get("tool_name") or tool_name),
        status=str(data.get("status") or "failed"),
        summary=str(data.get("summary") or data.get("error_message") or f"{tool_name} returned {data.get('status')}"),
        correlation_id=_correlation_id(args),
        data=data.get("data") if isinstance(data.get("data"), dict) else {k: v for k, v in data.items() if k != "evidence_refs"},
        evidence_refs=refs,
        audit=data.get("audit") if isinstance(data.get("audit"), dict) else {"status": data.get("status"), "tool_name": tool_name},
        truncated=bool(data.get("truncated", False)),
        next_cursor=data.get("next_cursor"),
    )


def _evidence_ref_from_mapping(item: Any, *, source: str, args: dict[str, Any]) -> EvidenceRef:
    data = item if isinstance(item, dict) else {}
    ref_id = str(data.get("ref_id") or data.get("ref") or f"ev_{source}_{_stable_digest(args)[:16]}")
    return EvidenceRef(
        ref_id=ref_id,
        source=str(data.get("source") or source),
        cluster_id=str(data.get("cluster_id") or args.get("cluster_id") or ""),
        namespace=data.get("namespace") or args.get("namespace"),
        service=data.get("service") or args.get("service"),
        time_range=data.get("time_range"),
        query_digest=data.get("query_digest"),
        cursor=data.get("cursor"),
    )


def _failed_tool_envelope(args: dict[str, Any], *, tool_name: str, source: str, message: str) -> ToolEnvelope:
    return ToolEnvelope(
        request_id=str(args.get("request_id") or args.get("task_id") or f"{tool_name}_failed"),
        tool_name=tool_name,
        status="failed",
        summary=f"{tool_name} unavailable: {message}",
        correlation_id=_correlation_id(args),
        data={},
        audit={"status": "failed", "tool_name": tool_name, "error_code": "backend_unavailable", "source": source},
    )


async def _synthetic_metrics_adapter(args: dict[str, Any]) -> ToolEnvelope:
    service = str(args.get("service") or "service")
    ref = _evidence_ref("prometheus", args)
    return ToolEnvelope(
        request_id=str(args.get("request_id") or ref.ref_id),
        tool_name="query_metrics",
        status="succeeded",
        summary=f"Prometheus evidence indicates elevated 5xx/error-rate signal for {service}",
        correlation_id=_correlation_id(args),
        data={"ref": ref.ref_id, "query_digest": ref.query_digest, "synthetic": True},
        evidence_refs=(ref,),
        audit={"status": "succeeded", "tool_name": "query_metrics", "synthetic": True},
    )


async def _synthetic_logs_adapter(args: dict[str, Any]) -> ToolEnvelope:
    service = str(args.get("service") or "service")
    ref = _evidence_ref("loki", args)
    return ToolEnvelope(
        request_id=str(args.get("request_id") or ref.ref_id),
        tool_name="query_logs",
        status="succeeded",
        summary=f"Loki evidence contains timeout/error samples for {service}",
        correlation_id=_correlation_id(args),
        data={"ref": ref.ref_id, "query_digest": ref.query_digest, "synthetic": True},
        evidence_refs=(ref,),
        audit={"status": "succeeded", "tool_name": "query_logs", "synthetic": True},
    )


async def _synthetic_k8s_read_adapter(args: dict[str, Any]) -> ToolEnvelope:
    ref = _evidence_ref("k8s_read", args)
    namespace = str(args.get("namespace") or "default")
    cluster = str(args.get("cluster_id") or "default")
    return ToolEnvelope(
        request_id=str(args.get("request_id") or ref.ref_id),
        tool_name="run_k8s_read",
        status="succeeded",
        summary=f"K8s read evidence captured workload state in {namespace}/{cluster}",
        correlation_id=_correlation_id(args),
        data={"ref": ref.ref_id, "command": args.get("command"), "synthetic": True},
        evidence_refs=(ref,),
        audit={"status": "succeeded", "tool_name": "run_k8s_read", "synthetic": True},
    )


def _evidence_ref(source: str, args: dict[str, Any]) -> EvidenceRef:
    digest = _stable_digest({"source": source, "args": args})
    return EvidenceRef(
        ref_id=f"ev_{source}_{digest[:16]}",
        source=source,
        query_digest=digest,
        cluster_id=str(args.get("cluster_id") or ""),
        namespace=str(args.get("namespace") or ""),
    )


def _correlation_id(args: dict[str, Any]) -> str | None:
    value = args.get("correlation_id")
    return str(value) if value is not None else None


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps Hermes service smoke boundary")
    parser.add_argument("--host", default=os.getenv("AIOPS_HERMES_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_HERMES_PORT", "8082")))
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    serve(HermesServiceHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
