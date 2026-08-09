"""A01 real Model Provider invalid-to-verified gate."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .ledger import AcceptanceLedger
from .evidence_types import Artifact, GateResult
from .http import GatewaySession
from .integration_support import expect, fail_gate, reauthenticate


@dataclass(frozen=True)
class ModelInputs:
    endpoint: str
    endpoint_scope: str
    model: str
    timeout_seconds: int
    api_key: str


class ModelGateRunner:
    def __init__(
        self,
        *,
        evidence: AcceptanceLedger,
        admin: GatewaySession,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.evidence = evidence
        self.admin = admin
        self.sleep = sleep
        self.now = now

    def run_s03(self, inputs: ModelInputs, *, admin_password: str) -> None:
        started_at = self.evidence.start_gate("S03")
        execution_id = self.evidence.resume_gate("S03").execution_id
        secrets = (admin_password, inputs.api_key, "acceptance-invalid-model-credential")
        artifacts: list[Artifact] = []
        try:
            reauthenticate(self.admin, admin_password, "s03")
            detail = expect(
                self.admin.request("GET", "/api/v1/admin/model-provider"), {200}
            ).body["model_provider"]
            invalid_save_id = f"{execution_id}:s03-invalid-save"
            self.evidence.bind_operation(
                "S03", kind="model_mutation", operation_id=invalid_save_id,
            )
            invalid_save = expect(
                self.admin.request(
                    "PUT",
                    "/api/v1/admin/model-provider",
                    body={
                        "endpoint": inputs.endpoint,
                        "endpoint_scope": inputs.endpoint_scope,
                        "model": inputs.model,
                        "timeout_seconds": inputs.timeout_seconds,
                        "api_key": "acceptance-invalid-model-credential",
                        "expected_revision": detail.get("configuration_revision"),
                        "reason": "A01 prove invalid Model credential is rejected",
                    },
                    request_id=invalid_save_id,
                ),
                {200},
            )
            invalid_revision = invalid_save.body["model_provider"]["configuration_revision"]
            invalid_test_id = f"{execution_id}:s03-invalid-test"
            self.evidence.bind_operation(
                "S03", kind="model_mutation", operation_id=invalid_test_id,
            )
            invalid_test = expect(
                self.admin.request(
                    "POST",
                    "/api/v1/admin/model-provider/test",
                    body={
                        "expected_revision": invalid_revision,
                        "reason": "A01 bounded invalid Model probe",
                    },
                    request_id=invalid_test_id,
                ),
                {202},
            )
            invalid_status = self._poll(invalid_revision, "failed")
            reason = invalid_status["verification"].get("reason_code")
            if reason != "authentication_failed" or invalid_status["readiness"] != "not_ready":
                raise ValueError("invalid Model credential did not fail as authentication_failed")
            reauthenticate(self.admin, admin_password, "s03-real")
            real_save_id = f"{execution_id}:s03-real-save"
            self.evidence.bind_operation(
                "S03", kind="model_mutation", operation_id=real_save_id,
            )
            real_save = expect(
                self.admin.request(
                    "PUT",
                    "/api/v1/admin/model-provider",
                    body={
                        "endpoint": inputs.endpoint,
                        "endpoint_scope": inputs.endpoint_scope,
                        "model": inputs.model,
                        "timeout_seconds": inputs.timeout_seconds,
                        "api_key": inputs.api_key,
                        "expected_revision": invalid_revision,
                        "reason": "A01 configure real Model provider revision",
                    },
                    request_id=real_save_id,
                ),
                {200},
            )
            real_revision = real_save.body["model_provider"]["configuration_revision"]
            real_test_id = f"{execution_id}:s03-real-test"
            self.evidence.bind_operation(
                "S03", kind="model_mutation", operation_id=real_test_id,
            )
            real_test = expect(
                self.admin.request(
                    "POST",
                    "/api/v1/admin/model-provider/test",
                    body={
                        "expected_revision": real_revision,
                        "reason": "A01 two-turn tool-use nonce probe",
                    },
                    request_id=real_test_id,
                ),
                {202},
            )
            real_status = self._poll(real_revision, "verified")
            checked_at = real_status["verification"].get("checked_at")
            if (
                real_status["readiness"] != "ready"
                or real_status["configuration_revision"] != real_revision
                or not isinstance(checked_at, (int, float))
                or checked_at < self.now() - 900
            ):
                raise ValueError("real Model revision is not freshly ready")
            platform = expect(
                self.admin.request("GET", "/api/v1/platform/status"), {200}
            ).body["capabilities"]["model"]
            if (
                platform.get("readiness") != "ready"
                or platform.get("configuration_revision") != real_revision
                or platform.get("verification", {}).get("revision") != real_revision
                or platform.get("verification", {}).get("checked_at") != checked_at
            ):
                raise ValueError("Platform Status does not project the freshly verified Model revision")
            request_ids = {
                invalid_save.body["request_id"],
                invalid_test.body["request_id"],
                real_save.body["request_id"],
                real_test.body["request_id"],
            }
            audit_response = expect(
                self.admin.request("GET", "/api/v1/admin/audit"), {200}
            ).body
            audit_rows = [
                row
                for row in audit_response.get("audit", [])
                if row.get("request_id") in request_ids
            ]
            if {row.get("request_id") for row in audit_rows} != request_ids:
                raise ValueError("Model setup operations are missing admin audit correlation")
            artifacts.extend(
                [
                    self.evidence.write_json(
                        "S03",
                        "invalid-verification.json",
                        {
                            "revision": invalid_revision,
                            "operation_id": invalid_test.body["verification"]["operation_id"],
                            "state": "failed",
                            "reason_code": reason,
                        },
                        known_secrets=secrets,
                    ),
                    self.evidence.write_json(
                        "S03",
                        "real-verification.json",
                        {
                            "revision": real_revision,
                            "operation_id": real_test.body["verification"]["operation_id"],
                            "state": "verified",
                            "checked_at": checked_at,
                            "fresh_until": checked_at + 900,
                            "readiness": real_status["readiness"],
                            "platform_status": platform,
                        },
                        known_secrets=secrets,
                    ),
                    self.evidence.write_json(
                        "S03", "admin-audit.json", audit_rows, known_secrets=secrets
                    ),
                ]
            )
            self.evidence.record_gate("S03", GateResult("passed", tuple(artifacts)), started_at=started_at)
        except Exception as exc:
            fail_gate(self.evidence, "S03", artifacts, exc, secrets, started_at)

    def _poll(self, revision: str, expected_state: str) -> dict[str, Any]:
        for _ in range(300):
            model = expect(
                self.admin.request("GET", "/api/v1/model-provider/status"), {200}
            ).body["model"]
            verification = model.get("verification", {})
            if verification.get("revision") == revision:
                state = verification.get("state")
                if state == expected_state:
                    return model
                if state in {"failed", "verified", "stale"}:
                    raise ValueError(
                        f"Model verification reached unexpected terminal state {state}"
                    )
            self.sleep(3)
        raise TimeoutError(f"Model verification did not reach {expected_state} within 15m")
