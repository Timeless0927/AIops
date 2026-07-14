"""A01 exact Connector Enrollment and live read-verification gate."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import yaml

from .command import CommandExecutor
from .evidence import AcceptanceEvidence, Artifact
from .http import GatewaySession
from .integration_support import expect, fail_gate, reauthenticate


class ConnectorGateRunner:
    def __init__(
        self,
        *,
        evidence: AcceptanceEvidence,
        admin: GatewaySession,
        commands: CommandExecutor,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.evidence = evidence
        self.admin = admin
        self.commands = commands
        self.sleep = sleep

    def run_s05(
        self,
        *,
        admin_password: str,
        connector_id: str,
        cluster_id: str,
        confirm_one_time: Callable[[], None],
    ) -> None:
        started_at = self.evidence.start_gate("S05")
        artifacts: list[Artifact] = []
        credential = ""
        try:
            if not connector_id or not cluster_id:
                raise ValueError("release Connector/Cluster identity must be non-empty")
            reauthenticate(self.admin, admin_password, "s05")
            initial = expect(
                self.admin.request("GET", "/api/v1/admin/connector-enrollments"), {200}
            ).body
            if initial.get("connector_enrollments") or initial.get("clusters"):
                raise ValueError("S05 requires a clean Connector Enrollment baseline")
            enrolled = expect(
                self.admin.request(
                    "POST",
                    "/api/v1/admin/connector-enrollments",
                    body={
                        "connector_id": connector_id,
                        "cluster_id": cluster_id,
                        "reason": "A01 enroll exact Pilot Connector and Cluster",
                    },
                    request_id="acceptance-s05-enroll",
                ),
                {201},
            ).body
            credential = enrolled.get("credential", "")
            if not isinstance(credential, str) or not credential:
                raise ValueError("Connector Enrollment did not return a one-time credential")
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "aiops-connector-secret", "namespace": "aiops-system"},
                "type": "Opaque",
                "stringData": {"AIOPS_CONNECTOR_CREDENTIAL": credential},
            }
            applied = self.commands.run(
                ["kubectl", "apply", "-f", "-"],
                stdin=yaml.safe_dump(secret, sort_keys=False),
                timeout=60,
            )
            rollout = self.commands.run(
                [
                    "kubectl", "rollout", "restart", "deployment/aiops-connector",
                    "-n", "aiops-system",
                ],
                timeout=60,
            )
            waited = self.commands.run(
                [
                    "kubectl", "rollout", "status", "deployment/aiops-connector",
                    "-n", "aiops-system", "--timeout=5m",
                ],
                timeout=330,
            )
            if any(result.exit_code != 0 for result in (applied, rollout, waited)):
                raise RuntimeError("Connector Secret apply or rollout failed")
            ready = self._poll(connector_id, cluster_id)
            if "credential" in json.dumps(ready).lower():
                raise ValueError("Connector credential was readable after enrollment")
            platform = expect(
                self.admin.request("GET", "/api/v1/platform/status"), {200}
            ).body["capabilities"]["connector"]
            if (
                platform.get("readiness") != "ready"
                or platform.get("configuration") != "present"
                or platform.get("verification", {}).get("state") != "verified"
                or platform.get("connection", {}).get("online", 0) < 1
            ):
                raise ValueError("Platform Status did not project Connector read verification as ready")
            confirm_one_time()
            self.evidence.require_verified_attestation(
                "S05", role="platform_operator"
            )
            verification = ready["cluster"]["read_verification"]
            artifacts.extend(
                [
                    self.evidence.write_text(
                        "S05",
                        "connector-rollout.txt",
                        "\n".join(
                            self.evidence.command_text(result, known_secrets=[credential])
                            for result in (applied, rollout, waited)
                        ),
                        known_secrets=[credential],
                    ),
                    self.evidence.write_json(
                        "S05",
                        "connector-read-verification.json",
                        {
                            "enrollment_id": enrolled["connector_enrollment"]["id"],
                            "connector_id": connector_id,
                            "cluster_id": cluster_id,
                            "state": ready["enrollment"]["state"],
                            "read_verification": verification,
                            "platform_status": platform,
                        },
                        known_secrets=[credential],
                    ),
                ]
            )
            self.evidence.record_gate("S05", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            fail_gate(
                self.evidence,
                "S05",
                artifacts,
                exc,
                (admin_password, credential),
                started_at,
            )

    def _poll(self, connector_id: str, cluster_id: str) -> dict[str, Any]:
        for _ in range(200):
            body = expect(
                self.admin.request("GET", "/api/v1/admin/connector-enrollments"), {200}
            ).body
            enrollment = next(
                (
                    item for item in body.get("connector_enrollments", [])
                    if item.get("connector_id") == connector_id
                    and item.get("cluster_id") == cluster_id
                ),
                None,
            )
            cluster = next(
                (
                    item for item in body.get("clusters", [])
                    if item.get("connector_id") == connector_id
                    and item.get("cluster_id") == cluster_id
                ),
                None,
            )
            verification = cluster.get("read_verification", {}) if cluster else {}
            if (
                enrollment
                and enrollment.get("state") == "online"
                and enrollment.get("read_verification") == "verified"
                and cluster
                and cluster.get("runtime_status") == "online"
                and verification.get("status") == "verified"
                and verification.get("cluster_identity")
                and verification.get("discovery")
                and verification.get("permission_summary")
            ):
                return {"enrollment": enrollment, "cluster": cluster}
            self.sleep(3)
        raise TimeoutError("Connector heartbeat/read verification did not converge within 10m")
