"""Fixed-window retention telemetry Adapter for Stateful Recovery."""

from __future__ import annotations

import hashlib
import json
import math
import urllib.parse

from .command import CommandExecutor
from .recovery import NAMESPACE, recovery_metric_ref_sha256
from .run_one_decisions import valid_run_id


class KubectlRetentionTelemetry:
    """Queries fixed Prometheus/Loki windows through argv-only kubectl exec."""

    def __init__(self, commands: CommandExecutor, *, kube_context: str) -> None:
        if not kube_context:
            raise ValueError("retention telemetry requires an exact kube context")
        self.commands = commands
        self.kube_context = kube_context

    def probe_retention(
        self,
        run_id: str,
        *,
        metric_at: float,
        log_at: float,
    ) -> dict[str, object]:
        if (
            not valid_run_id(run_id)
            or not all(math.isfinite(item) and item >= 0 for item in (metric_at, log_at))
        ):
            raise ValueError("retention probe requires exact V06 identities and timestamps")
        self._commands: list[dict[str, object]] = []
        selectors = {
            "namespace": "aiops-verification",
            "deployment": "verification-api",
            "service": "verification-api",
            "run_id": run_id,
        }
        metric_query = urllib.parse.urlencode({
            "query": (
                'aiops_verification_fault_active{namespace="aiops-verification",'
                f'service="verification-api",run_id="{run_id}"}}'
            ),
            "start": metric_at - 1,
            "end": metric_at + 1,
            "step": 1,
        })
        metric_payload = self._get_json(
            f"http://aiops-prometheus:9090/api/v1/query_range?{metric_query}",
            deployment="aiops-mcp-prometheus",
        )
        metric_refs = [
            recovery_metric_ref_sha256(run_id, float(sample[0]))
            for item in metric_payload.get("data", {}).get("result", [])
            if isinstance(item, dict)
            and all(item.get("metric", {}).get(key) == value for key, value in selectors.items())
            for sample in item.get("values", [])
            if isinstance(sample, list)
            and len(sample) == 2
            and abs(float(sample[0]) - metric_at) <= 1
            and float(sample[1]) == 0
        ]
        log_query = urllib.parse.urlencode({
            "query": (
                '{namespace="aiops-verification",container="verification-api"} '
                f'|= "{run_id}" |= "verification_fault_recovered"'
            ),
            "start": int((log_at - 1) * 1_000_000_000),
            "end": int((log_at + 1) * 1_000_000_000),
            "limit": 20,
        })
        log_payload = self._get_json(
            f"http://aiops-loki:3100/loki/api/v1/query_range?{log_query}",
            deployment="aiops-mcp-loki",
        )
        recovery_line = json.dumps(
            {"event": "verification_fault_recovered", "run_id": run_id},
            separators=(",", ":"), sort_keys=True,
        )
        log_refs = [
            hashlib.sha256(f"{timestamp}\n{line}".encode()).hexdigest()
            for stream in log_payload.get("data", {}).get("result", [])
            if stream.get("stream", {}).get("namespace") == "aiops-verification"
            and stream.get("stream", {}).get("container") == "verification-api"
            for timestamp, line in stream.get("values", [])
            if abs(int(timestamp) / 1_000_000_000 - log_at) <= 1
            and line == recovery_line
        ]
        if not metric_refs or not log_refs:
            raise RuntimeError("fixed V06 metric/log samples were not retained")
        return {
            "run_id": run_id,
            "recovery_metric_observed_at": metric_at,
            "recovery_log_observed_at": log_at,
            "recovery_metric_ref_hashes": sorted(set(metric_refs)),
            "recovery_log_ref_hashes": sorted(set(log_refs)),
            "commands": self._commands,
        }

    def _get_json(self, url: str, *, deployment: str) -> dict[str, object]:
        result = self.commands.run([
            "kubectl", "--context", self.kube_context, "-n", NAMESPACE,
            "exec", f"deployment/{deployment}", "--",
            "curl", "--noproxy", "*", "-fsS", url,
        ], timeout=60)
        self._commands.append({
            "command": list(result.command[:-1]) + [
                "url-sha256:" + hashlib.sha256(url.encode()).hexdigest(),
            ],
            "exit_code": result.exit_code,
            "duration_seconds": result.duration_seconds,
        })
        if result.exit_code != 0:
            raise RuntimeError("fixed-window telemetry query failed")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("fixed-window telemetry query returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("fixed-window telemetry query returned a non-object")
        return value
