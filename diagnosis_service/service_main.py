"""Smokeable diagnosis service boundary for split-image packaging."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib import error, request

from apps.service_http import JsonHandler, connectivity_payload, serve
from aiops.contracts import EvidenceRef, ToolEnvelope
from apps.internal_auth import enforce_internal_auth, internal_auth_headers
from diagnosis_service import change_planner_http, model_provider_http
from diagnosis_service.jobs import DiagnosisJobError, DiagnosisJobs, start_workers
from diagnosis_service.k8s_read_adapter import gateway_read_payload
from diagnosis_service.model_provider import (
    ModelProviderConfiguration,
    ModelProviderError,
)
from diagnosis_service.model_provider_crypto import CredentialCipher, read_encryption_key
from diagnosis_service.model_provider_repository import ModelProviderRepository
from diagnosis_service.runtime import DiagnosisRuntime

logger = logging.getLogger("diagnosis_service.service_main")
SERVICE_NAME = "diagnosis"

_JOBS: DiagnosisJobs | None = None
_MODEL_PROVIDER: ModelProviderConfiguration | None = None


class DiagnosisServiceHandler(JsonHandler):
    """Minimal diagnosis HTTP surface used by image and compose smoke tests."""

    service_name = SERVICE_NAME

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if self.is_metrics_request():
            self.write_metrics(SERVICE_NAME, _diagnosis_jobs().metrics().encode())
            return
        if path in {"/model-provider/status", "/admin/model-provider"} and _dispatch_model_provider(self, path):
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
        path = urlparse(self.path).path
        if path in {"/admin/model-provider", "/admin/model-provider/test"} and _dispatch_model_provider(self, path):
            return
        if self.path == "/change-plans":
            try:
                provider = _diagnosis_runtime().resolve_provider()
            except ModelProviderError:
                provider = None
            change_planner_http.handle(self, provider, SERVICE_NAME)
            return
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
            result = _diagnosis_runtime().accept(payload)
        except ModelProviderError as exc:
            self.write_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                _service_payload(status="blocked", error={"code": exc.code, "message": "Model Provider is not ready"}),
            )
            return
        except DiagnosisJobError as exc:
            status = HTTPStatus.CONFLICT if exc.code == "request_conflict" else HTTPStatus.BAD_REQUEST
            self.write_json(status, _service_payload(status="rejected", error={"code": exc.code, "message": exc.message}))
            return
        self.write_json(HTTPStatus.ACCEPTED, _service_payload(**result))

    def do_PUT(self) -> None:  # noqa: N802
        if not _dispatch_model_provider(self, urlparse(self.path).path):
            self.write_not_found()

    def do_DELETE(self) -> None:  # noqa: N802
        if not _dispatch_model_provider(self, urlparse(self.path).path):
            self.write_not_found()

def _parse_session_route(path: str) -> tuple[str, str | None] | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) not in {3, 4} or parts[:2] != ["diagnosis", "sessions"]:
        return None
    artifact = parts[3] if len(parts) == 4 else None
    if artifact not in {None, "diagnosis", "markdown", "timeline"}:
        return None
    return parts[2], artifact


def _authorize_gateway(handler) -> str | None:
    return enforce_internal_auth(
        handler,
        service_name=SERVICE_NAME,
        allowed_service_account="aiops-gateway",
    )


def _dispatch_model_provider(handler, path: str) -> bool:
    try:
        owner = _model_provider()
    except ModelProviderError:
        handler.write_json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"status": "unavailable", "error": {"code": "owner_unavailable", "message": "Model Provider owner is unavailable"}},
        )
        return True
    return model_provider_http.dispatch(handler, path, owner, _authorize_gateway)


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
        payload = gateway_read_payload(args)
        return await _http_tool_adapter(
            payload,
            url=f"{gateway_url.rstrip('/')}/api/v1/internal/diagnosis/k8s-read",
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


def _model_provider() -> ModelProviderConfiguration:
    global _MODEL_PROVIDER
    path = DiagnosisJobs.default_path()
    key_path = os.getenv("AIOPS_MODEL_ENCRYPTION_KEY_PATH", "/var/run/secrets/aiops-model/key")
    if _MODEL_PROVIDER is None or _MODEL_PROVIDER.db_path != path:
        try:
            cipher = CredentialCipher(read_encryption_key(Path(key_path)), nonce_source=os.urandom)
        except OSError as exc:
            raise ModelProviderError(
                "encryption_key_unavailable",
                "Model Provider encryption key is unavailable",
            ) from exc
        except ValueError as exc:
            raise ModelProviderError("invalid_encryption_key", str(exc)) from exc
        _MODEL_PROVIDER = ModelProviderConfiguration(
            ModelProviderRepository(path),
            cipher,
            clock=time.time,
            revision_id=lambda: f"model-provider:{uuid.uuid4().hex}",
            verification_nonce=lambda: uuid.uuid4().hex + uuid.uuid4().hex,
        )
    return _MODEL_PROVIDER


def _diagnosis_runtime() -> DiagnosisRuntime:
    return DiagnosisRuntime(
        _diagnosis_jobs(),
        _model_provider,
        metrics_adapter=_metrics_adapter,
        logs_adapter=_logs_adapter,
        k8s_read_adapter=_k8s_read_adapter,
        topology_adapter=_topology_adapter,
        max_turns=_diagnosis_max_turns(),
        clock=time.monotonic,
    )


def _diagnosis_max_turns() -> int:
    raw = os.getenv("AIOPS_LLM_TOOLUSE_MAX_TURNS") or os.getenv("AIOPS_AGENT_MAX_TURNS")
    try:
        value = int(raw) if raw else 6
    except ValueError:
        return 6
    return value if value > 0 else 6


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


def _start_verification_worker(
    owner: ModelProviderConfiguration,
    runtime: DiagnosisRuntime,
    *,
    interval_seconds: float = 1.0,
) -> threading.Thread:
    stop = threading.Event()

    def work() -> None:
        while not stop.is_set():
            if not owner.run_verification_once(runtime.verify_provider):
                stop.wait(interval_seconds)

    worker = threading.Thread(target=work, name="model-provider-verification", daemon=True)
    worker.start()
    return worker


def main() -> None:
    args = _build_parser().parse_args()
    runtime = _diagnosis_runtime()
    start_workers(_diagnosis_jobs(), runner=runtime.execute_job, sender=_send_writeback)
    try:
        _start_verification_worker(_model_provider(), runtime)
    except ModelProviderError as exc:
        logger.warning("Model Provider owner unavailable at startup: %s", exc.code)
    serve(DiagnosisServiceHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
