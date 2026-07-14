"""A01 real Prometheus/Loki/Alertmanager/MCP setup gate."""

from __future__ import annotations

from typing import Any, Protocol

from .evidence import AcceptanceEvidence, Artifact
from .integration_support import fail_gate


class TelemetryProbe(Protocol):
    def probe(self) -> dict[str, Any]: ...


class ObservabilityGateRunner:
    def __init__(self, *, evidence: AcceptanceEvidence, telemetry: TelemetryProbe) -> None:
        self.evidence = evidence
        self.telemetry = telemetry

    def run_s06(self) -> None:
        started_at = self.evidence.start_gate("S06")
        artifacts: list[Artifact] = []
        try:
            summary = self.telemetry.probe()
            summary["command_context"] = {
                "release_sha256": self.evidence.candidate_sha256,
                "kube_context": self.evidence.kube_context,
            }
            required_targets = {
                "prometheus",
                "alertmanager",
                "kube-state-metrics",
                "aiops-gateway",
                "aiops-connector",
                "aiops-diagnosis",
                "aiops-notification",
                "aiops-mcp-prometheus",
                "aiops-mcp-loki",
                "aiops-mcp-topology",
            }
            targets = summary.get("targets", {})
            if not required_targets <= {
                name for name, health in targets.items() if health == "up"
            }:
                raise ValueError("real Prometheus AIOps/kube-state-metrics targets are not up")
            if (
                summary.get("rules_loaded") is not True
                or int(summary.get("workload_series", 0)) < 1
                or int(summary.get("loki_streams", 0)) < 1
                or summary.get("alertmanager_route") is not True
            ):
                raise ValueError("real observability owner path is incomplete")
            prometheus = summary.get("mcp_prometheus", {})
            loki = summary.get("mcp_loki", {})
            if (
                prometheus.get("status") != "succeeded"
                or int(prometheus.get("returned_series", 0)) < 1
                or loki.get("status") != "succeeded"
                or int(loki.get("returned_lines", 0)) < 1
            ):
                raise ValueError("guarded MCP Prometheus/Loki query failed")
            artifacts.append(self.evidence.write_json("S06", "telemetry-summary.json", summary))
            self.evidence.record_gate("S06", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            fail_gate(self.evidence, "S06", artifacts, exc, (), started_at)
