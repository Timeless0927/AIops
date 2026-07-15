"""Real Cluster telemetry probe for A01 S06."""

from __future__ import annotations

import hashlib
import json
import shlex
import time
import urllib.parse
from typing import Any

from .command import CommandExecutor


class KubernetesTelemetryProbe:
    def __init__(self, commands: CommandExecutor, *, now=time.time) -> None:
        self.commands = commands
        self.now = now

    def probe(self) -> dict[str, Any]:
        self._commands: list[dict[str, Any]] = []
        targets_payload = self._get_json(
            "http://aiops-prometheus:9090/api/v1/targets",
            deployment="aiops-mcp-prometheus",
        )
        health_by_job: dict[str, list[str]] = {}
        for item in targets_payload.get("data", {}).get("activeTargets", []):
            job = str(item.get("labels", {}).get("job"))
            health_by_job.setdefault(job, []).append(str(item.get("health")))
        targets = {
            job: "up" if health and all(item == "up" for item in health) else "down"
            for job, health in health_by_job.items()
        }
        rules_payload = self._get_json(
            "http://aiops-prometheus:9090/api/v1/rules",
            deployment="aiops-mcp-prometheus",
        )
        rules = [
            rule
            for group in rules_payload.get("data", {}).get("groups", [])
            for rule in group.get("rules", [])
        ]
        query = urllib.parse.urlencode(
            {"query": '{__name__=~"aiops_.+",namespace="aiops-system"}'}
        )
        series_payload = self._get_json(
            f"http://aiops-prometheus:9090/api/v1/query?{query}",
            deployment="aiops-mcp-prometheus",
        )
        series = series_payload.get("data", {}).get("result", [])
        end = int(self.now() * 1_000_000_000)
        loki_query = urllib.parse.urlencode(
            {
                "query": '{namespace="aiops-system"}',
                "start": end - 15 * 60 * 1_000_000_000,
                "end": end,
                "limit": 20,
            }
        )
        loki_payload = self._get_json(
            f"http://aiops-loki:3100/loki/api/v1/query_range?{loki_query}",
            deployment="aiops-mcp-loki",
        )
        streams = loki_payload.get("data", {}).get("result", [])
        alertmanager = self._exec_json(
            "amtool --alertmanager.url=http://127.0.0.1:9093 --output=json config show",
            deployment="aiops-alertmanager",
        )
        original_config = str(alertmanager.get("config", {}).get("original", ""))
        mcp_prometheus = self._mcp(
            "aiops-mcp-prometheus:8083",
            "/query_metrics",
            {
                "request_id": "acceptance-s06-prometheus",
                "cluster_id": "pilot-cluster",
                "namespace": "aiops-system",
                "service": "aiops-gateway",
                "reason": "A01 real guarded Prometheus query",
                "query": 'aiops_gateway_sse_connections{namespace="aiops-system"}',
                "max_series": 5,
                "step": "15s",
            },
        )
        mcp_loki = self._mcp(
            "aiops-mcp-loki:8084",
            "/query_logs",
            {
                "request_id": "acceptance-s06-loki",
                "cluster_id": "pilot-cluster",
                "namespace": "aiops-system",
                "service": "aiops-gateway",
                "reason": "A01 real guarded Loki query",
                "query": '{namespace="aiops-system", app="aiops-gateway"}',
                "time_range": {"type": "relative", "value": "15m"},
                "max_lines": 20,
                "sample_size": 5,
            },
        )
        return {
            "targets": dict(sorted(targets.items())),
            "rules_loaded": bool(rules) and all(rule.get("health") == "ok" for rule in rules),
            "rule_names_sha256": hashlib.sha256(
                "\n".join(sorted(str(rule.get("name")) for rule in rules)).encode()
            ).hexdigest(),
            "workload_series": len(series),
            "series_label_hashes": [
                hashlib.sha256(json.dumps(item.get("metric", {}), sort_keys=True).encode()).hexdigest()
                for item in series
            ],
            "loki_streams": len(streams),
            "loki_stream_label_hashes": [
                hashlib.sha256(json.dumps(item.get("stream", {}), sort_keys=True).encode()).hexdigest()
                for item in streams
            ],
            "alertmanager_route": (
                "receiver: gateway" in original_config
                and 'aiops_route="gateway"' in original_config
            ),
            "mcp_prometheus": self._safe_mcp(mcp_prometheus, "returned_series"),
            "mcp_loki": self._safe_mcp(mcp_loki, "returned_lines"),
            "commands": self._commands,
        }

    def _get_json(self, url: str, *, deployment: str) -> dict[str, Any]:
        script = f"curl --noproxy '*' -fsS {shlex.quote(url)}"
        return self._exec_json(script, deployment=deployment)

    def _mcp(self, service: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"))
        script = (
            "token=$(cat /var/run/secrets/aiops-internal/token); "
            "curl --noproxy '*' -fsS -H \"Authorization: Bearer $token\" "
            "-H 'Content-Type: application/json' "
            f"-d {shlex.quote(body)} http://{service}{path}"
        )
        return self._exec_json(script)

    def _exec_json(self, script: str, *, deployment: str = "aiops-diagnosis") -> dict[str, Any]:
        result = self.commands.run(
            [
                "kubectl", "-n", "aiops-system", "exec", f"deployment/{deployment}",
                "--", "sh", "-ec", script,
            ],
            timeout=60,
        )
        self._commands.append(
            {
                "command": list(result.command[:-1]) + [
                    "script-sha256:" + hashlib.sha256(script.encode()).hexdigest()
                ],
                "exit_code": result.exit_code,
                "started_at": result.started_at,
                "completed_at": result.completed_at,
                "duration_seconds": result.duration_seconds,
            }
        )
        if result.exit_code != 0:
            raise RuntimeError("in-Cluster telemetry query failed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("in-Cluster telemetry query returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("in-Cluster telemetry query returned a non-object")
        return payload

    @staticmethod
    def _safe_mcp(payload: dict[str, Any], count_key: str) -> dict[str, Any]:
        data = payload.get("data", {})
        return {
            "status": payload.get("status"),
            count_key: data.get(count_key, 0),
            "evidence_ref_hashes": [
                hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
                for item in payload.get("evidence_refs", [])
            ],
        }
