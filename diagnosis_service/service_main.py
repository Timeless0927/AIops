"""Smokeable diagnosis service boundary for split-image packaging."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse
from urllib import error, request

from apps.service_http import JsonHandler, connectivity_payload, serve
from aiops.contracts import EvidenceRef, ToolEnvelope
from apps.internal_auth import enforce_internal_auth, internal_auth_headers
from toolsets.incident_diagnosis import run_diagnosis_session

import diagnosis_service.diagnosis_provider as diagnosis_provider
from diagnosis_service.jobs import DiagnosisJobError, DiagnosisJobs, start_workers
from diagnosis_service.handoff import incident_from_handoff as _incident_from_handoff

logger = logging.getLogger("diagnosis_service.service_main")
SERVICE_NAME = "diagnosis"

_JOBS: DiagnosisJobs | None = None

# sentinel checked before re-resolving so a failed load isn't retried every call.
_PROVIDER_RESOLVED_SENTINEL: Any = object()

# LLM provider config resolved once at process load; None → keyword fallback (ADR-0003).
# ponytail: 进程级单例,load_from_env 出境日志只在启动打一次。
_DIAGNOSIS_PROVIDER: Any | None = None


def _resolve_diagnosis_provider() -> Any | None:
    global _DIAGNOSIS_PROVIDER
    if _DIAGNOSIS_PROVIDER is _PROVIDER_RESOLVED_SENTINEL:
        return None
    if _DIAGNOSIS_PROVIDER is None:
        try:
            _DIAGNOSIS_PROVIDER = diagnosis_provider.load_from_env()
        except diagnosis_provider.ProviderUnavailable as exc:
            logger.warning("diagnosis provider disabled (%s); diagnoses use keyword fallback", exc.code)
            _DIAGNOSIS_PROVIDER = _PROVIDER_RESOLVED_SENTINEL
    return None if _DIAGNOSIS_PROVIDER is _PROVIDER_RESOLVED_SENTINEL else _DIAGNOSIS_PROVIDER


class DiagnosisServiceHandler(JsonHandler):
    """Minimal diagnosis HTTP surface used by image and compose smoke tests."""

    service_name = SERVICE_NAME

    def do_GET(self) -> None:  # noqa: N802
        if self.is_metrics_request():
            self.write_metrics(SERVICE_NAME, _diagnosis_jobs().metrics().encode())
            return
        session_route = _parse_session_route(self.path)
        if session_route is not None:
            if enforce_internal_auth(
                self,
                service_name=SERVICE_NAME,
                allowed_service_account="aiops-gateway",
            ) is None:
                return
            session_id, artifact = session_route
            session = _diagnosis_jobs().export(session_id, artifact=artifact)
            if session is None:
                self.write_not_found()
                return
            self.write_json(HTTPStatus.OK, _service_payload(status="ok", session=session))
            return

        if self.path == "/diagnosis/sessions":
            if enforce_internal_auth(
                self,
                service_name=SERVICE_NAME,
                allowed_service_account="aiops-gateway",
            ) is None:
                return
            self.write_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "sessions": _diagnosis_jobs().list(),
                }
                | _service_payload(),
            )
            return

        if self.path == "/healthz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "gateway_url": os.getenv("AIOPS_GATEWAY_URL", ""),
                }
                | _service_payload(),
            )
            return

        if self.path in {"/readyz", "/connectivity/gateway"}:
            gateway_url = os.getenv("AIOPS_GATEWAY_URL", "")
            if not gateway_url:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "status": "unavailable",
                        "peer": "gateway",
                        "error": "AIOPS_GATEWAY_URL is not set",
                    }
                    | _service_payload(),
                )
                return
            status, payload = connectivity_payload(
                service=SERVICE_NAME,
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
        if enforce_internal_auth(
            self,
            service_name=SERVICE_NAME,
            allowed_service_account="aiops-gateway",
        ) is None:
            return

        try:
            payload = self.read_json_body()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.write_json(HTTPStatus.BAD_REQUEST, _service_payload(status="invalid", error=str(exc)))
            return

        try:
            result = _diagnosis_jobs().accept(payload)
        except DiagnosisJobError as exc:
            status = HTTPStatus.CONFLICT if exc.code == "request_conflict" else HTTPStatus.BAD_REQUEST
            self.write_json(status, _service_payload(status="rejected", error={"code": exc.code, "message": exc.message}))
            return
        self.write_json(HTTPStatus.ACCEPTED, _service_payload(**result))


async def run_diagnosis_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one already-persisted Diagnosis Job."""
    incident = _incident_from_handoff(payload)
    session = await run_diagnosis_session(
        incident,
        metrics_adapter=_metrics_adapter,
        logs_adapter=_logs_adapter,
        k8s_read_adapter=_k8s_read_adapter,
        topology_adapter=_topology_adapter,
        provider=_resolve_diagnosis_provider(),
        incident_store=False,
    )
    diagnosis = session.get("diagnosis")
    if isinstance(diagnosis, dict) and incident["human_input_event_ids"]:
        diagnosis["human_input_event_ids"] = incident["human_input_event_ids"]
    return session


def _parse_session_route(path: str) -> tuple[str, str | None] | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) not in {3, 4} or parts[:2] != ["diagnosis", "sessions"]:
        return None
    artifact = parts[3] if len(parts) == 4 else None
    if artifact not in {None, "diagnosis", "markdown", "timeline"}:
        return None
    return parts[2], artifact


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
    return _unconfigured_partial(args, "query_metrics", "prometheus", "AIOPS_PROMETHEUS_MCP_URL is not set")


async def _logs_adapter(args: dict[str, Any]) -> ToolEnvelope:
    mcp_url = os.getenv("AIOPS_LOKI_MCP_URL", "").strip()
    if mcp_url:
        return await _http_tool_adapter(
            args,
            url=f"{mcp_url.rstrip('/')}/query_logs",
            tool_name="query_logs",
            fallback_source="loki",
        )
    return _unconfigured_partial(args, "query_logs", "loki", "AIOPS_LOKI_MCP_URL is not set")


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
    return _unconfigured_partial(args, "run_k8s_read", "k8s_read", "AIOPS_GATEWAY_URL is not set")


def _unconfigured_partial(args: dict[str, Any], tool_name: str, source: str, reason: str) -> ToolEnvelope:
    """MCP URL 未配置:返回 partial 缺口 envelope,不造假证据(tool-use 路径下缺口即缺口)。"""
    return ToolEnvelope(
        request_id=str(args.get("request_id") or tool_name),
        tool_name=tool_name,
        status="partial",
        summary=reason,
        correlation_id=_correlation_id(args),
        data={},
        audit={
            "status": "partial",
            "tool_name": tool_name,
            "missing_reason": reason,
            "source": source,
        },
    )


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
    headers: dict[str, str] | None = None,
) -> ToolEnvelope:
    try:
        if headers:
            data = await asyncio.to_thread(_post_json, url, args, _adapter_timeout(), headers=headers)
        else:
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


def _post_json(
    target: str,
    payload: dict[str, Any],
    timeout: float,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "Accept": "application/json", **internal_auth_headers()}
    request_headers.update(_request_context_headers(payload))
    request_headers.update(headers or {})
    req = request.Request(
        target,
        data=body,
        headers=request_headers,
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("response body must be a JSON object")
        return data


def _adapter_timeout() -> float:
    return _float_env("AIOPS_DIAGNOSIS_TOOL_TIMEOUT_SECONDS", 3.0)


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _gateway_read_payload(args: dict[str, Any]) -> dict[str, Any]:
    argv = args.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
        argv = _default_k8s_read_argv(args)
    payload = {
        "cluster_id": args.get("cluster_id") or "",
        "namespace": args.get("namespace") or "",
        "argv": argv,
        "reason": args.get("reason"),
        "task_id": str(args.get("request_id") or "diagnosis-k8s-read").replace(":", "-"),
        "command_id": f"cmd-{_stable_digest(args)[:12]}",
    }
    selector = str(args.get("selector") or "").strip()
    if selector:
        payload["selector"] = selector
    return payload


def _default_k8s_read_argv(args: dict[str, Any]) -> list[str]:
    argv = ["kubectl", "get", "pods"]
    namespace = str(args.get("namespace") or "").strip()
    service = str(args.get("service") or "").strip()
    selector = str(args.get("selector") or "").strip()
    if namespace:
        argv.extend(["-n", namespace])
    if not selector and service:
        selector = f"app.kubernetes.io/name={service}"
    if selector:
        argv.extend(["-l", selector])
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


def _correlation_id(args: dict[str, Any]) -> str | None:
    value = args.get("correlation_id")
    return str(value) if value is not None else None


def _request_context_headers(payload: dict[str, object]) -> dict[str, str]:
    request_id = str(payload.get("request_id") or payload.get("session_id") or f"req-{uuid.uuid4().hex}")
    correlation_id = str(payload.get("correlation_id") or payload.get("incident_id") or request_id)
    return {"X-Request-ID": request_id, "X-Correlation-ID": correlation_id}


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _service_payload(**payload: Any) -> dict[str, Any]:
    return {"service": SERVICE_NAME, **payload}


def _diagnosis_jobs() -> DiagnosisJobs:
    global _JOBS
    path = DiagnosisJobs.default_path()
    if _JOBS is None or _JOBS.db_path != path:
        _JOBS = DiagnosisJobs(path)
    return _JOBS


def _execute_job(payload: dict[str, object]) -> dict[str, object]:
    return asyncio.run(run_diagnosis_job(payload))


def _send_writeback(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    gateway_url = os.getenv("AIOPS_GATEWAY_URL", "").strip()
    if not gateway_url:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"status": "gateway_unconfigured"}
    target = f"{gateway_url.rstrip('/')}/diagnosis/writeback"
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json", **internal_auth_headers()}
    headers.update(_request_context_headers(payload))
    req = request.Request(target, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=_float_env("AIOPS_DIAGNOSIS_WRITEBACK_TIMEOUT_SECONDS", 2.0)) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
            return response.status, data if isinstance(data, dict) else {"status": "invalid_response"}
    except error.HTTPError as exc:
        data = json.loads(exc.read().decode("utf-8") or "{}")
        return exc.code, data if isinstance(data, dict) else {"status": "invalid_response"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps diagnosis service smoke boundary")
    parser.add_argument("--host", default=os.getenv("AIOPS_DIAGNOSIS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_DIAGNOSIS_PORT", "8082")))
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    start_workers(_diagnosis_jobs(), runner=_execute_job, sender=_send_writeback)
    serve(DiagnosisServiceHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
