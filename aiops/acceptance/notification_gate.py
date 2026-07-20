"""A01 real Notification dead-letter-to-sent and route-selection gate."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .evidence import AcceptanceEvidence
from .evidence_types import Artifact
from .http import GatewaySession
from .integration_support import expect, fail_gate, reauthenticate, string_values


SUPPORTED_NOTIFICATION_PROVIDERS = ("feishu", "dingtalk", "smtp")
INVALID_NOTIFICATION_CONFIGS = {
    "feishu": {
        "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/acceptance-invalid-token"
    },
    "dingtalk": {
        "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=acceptanceinvalidtoken",
        "signing_secret": "acceptanceinvalidsigningsecret",
    },
    "smtp": {
        "host": "127.0.0.1",
        "port": 1,
        "username": "acceptance-invalid",
        "password": "acceptance-invalid-password",
        "from_address": "invalid@example.test",
        "to_addresses": ["invalid@example.test"],
        "tls_mode": "starttls",
    },
}


@dataclass(frozen=True)
class NotificationInputs:
    provider: str
    config: dict[str, Any]


class NotificationGateRunner:
    def __init__(
        self,
        *,
        evidence: AcceptanceEvidence,
        admin: GatewaySession,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.evidence = evidence
        self.admin = admin
        self.sleep = sleep

    def run_s04(
        self,
        inputs: NotificationInputs,
        *,
        admin_password: str,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("S04")
        execution_id = self.evidence.resume_gate("S04").execution_id

        def operation_id(suffix: str) -> str:
            value = f"{execution_id}:s04-{suffix}"
            self.evidence.bind_operation(
                "S04", kind="notification_mutation", operation_id=value,
            )
            return value

        secrets = (admin_password, *string_values(inputs.config))
        artifacts: list[Artifact] = []
        try:
            if inputs.provider not in SUPPORTED_NOTIFICATION_PROVIDERS:
                raise ValueError("unsupported Notification provider")
            reauthenticate(self.admin, admin_password, "s04")
            listed = expect(
                self.admin.request("GET", "/api/v1/admin/notification-destinations"), {200}
            ).body
            name = f"AIOps Pilot {self.evidence.root.name}"
            if any(item.get("name") == name for item in listed.get("destinations", [])):
                raise ValueError("acceptance Notification Destination already exists; use a clean run")
            created = expect(
                self.admin.request(
                    "POST",
                    "/api/v1/admin/notification-destinations",
                    body={
                        "name": name,
                        "provider": inputs.provider,
                        "config": copy.deepcopy(INVALID_NOTIFICATION_CONFIGS[inputs.provider]),
                        "reason": "A01 prove invalid Notification delivery dead-letters",
                    },
                    request_id=operation_id("invalid-create"),
                ),
                {201},
            ).body["destination"]
            destination_id = created["id"]
            invalid_revision = created["configuration_revision"]
            invalid_test = self._test(
                destination_id, invalid_revision, "invalid",
                request_id=operation_id("invalid-test"),
            )
            dead = self._poll_delivery(invalid_test["delivery_id"], "dead_letter")
            reauthenticate(self.admin, admin_password, "s04-real")
            repaired = expect(
                self.admin.request(
                    "PATCH",
                    f"/api/v1/admin/notification-destinations/{destination_id}",
                    body={
                        "enabled": False,
                        "config": inputs.config,
                        "expected_revision": invalid_revision,
                        "reason": "A01 repair Notification Destination with real provider",
                    },
                    request_id=operation_id("real-save"),
                ),
                {200},
            ).body["destination"]
            real_revision = repaired["configuration_revision"]
            real_test = self._test(
                destination_id, real_revision, "real",
                request_id=operation_id("real-test"),
            )
            sent = self._poll_delivery(real_test["delivery_id"], "sent")
            provider_identity = sent.get("provider_identity")
            if not isinstance(provider_identity, str) or not provider_identity:
                raise ValueError("sent Notification Delivery omitted provider identity")
            artifacts.append(
                self.evidence.write_json(
                    "S04",
                    "dead-letter.json",
                    {
                        "destination_id": destination_id,
                        "revision": invalid_revision,
                        "delivery_id": dead["id"],
                        "status": dead["status"],
                        "attempt_count": dead.get("attempt_count"),
                        "attempt_ids": self._attempt_ids(dead),
                        "reason_code": dead.get("last_reason_code"),
                    },
                    known_secrets=secrets,
                )
            )
            receipt_review = self.evidence.write_json(
                "S04",
                "receipt-review.json",
                {
                    "destination_id": destination_id,
                    "revision": real_revision,
                    "delivery_id": sent["id"],
                    "status": sent["status"],
                    "attempt_count": sent.get("attempt_count"),
                    "attempt_ids": self._attempt_ids(sent),
                    "provider_identity": provider_identity,
                },
                known_secrets=secrets,
            )
            artifacts.append(receipt_review)
            return {
                "gate_id": "S04",
                "status": "open",
                "required_attestation": "platform_administrator",
            }
        except Exception as exc:
            fail_gate(self.evidence, "S04", artifacts, exc, secrets, started_at)

    def resume_s04(self, *, admin_password: str) -> None:
        execution = self.evidence.resume_gate("S04")
        artifacts = list(execution.artifacts)
        receipts = [
            item for item in artifacts if item.path.name == "receipt-review.json"
        ]
        if len(receipts) != 1:
            raise ValueError("S04 requires one receipt review before resume")
        receipt_review = receipts[0]
        receipt_attestations = self.evidence.require_verified_attestation(
            "S04", role="platform_administrator"
        )
        if not any(
            item.get("statement", {}).get("note")
            == f"notification_receipt_sha256={receipt_review.sha256}"
            for item in receipt_attestations
        ):
            raise ValueError("S04 attestation does not bind the exact receipt review")

        secrets = (admin_password,)
        try:
            receipt = json.loads(receipt_review.path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("S04 receipt review is not an object")
            destination_id = str(receipt.get("destination_id") or "")
            real_revision = str(receipt.get("revision") or "")
            delivery_id = str(receipt.get("delivery_id") or "")
            provider_identity = str(receipt.get("provider_identity") or "")
            if not all((destination_id, real_revision, delivery_id, provider_identity)):
                raise ValueError("S04 receipt review omitted durable delivery identity")

            expected_operations = [
                f"{execution.execution_id}:s04-{suffix}"
                for suffix in (
                    "invalid-create", "invalid-test", "real-save", "real-test",
                )
            ]
            actual_operations = [
                item["operation_id"] for item in execution.operations
                if item.get("kind") == "notification_mutation"
            ]
            if actual_operations != expected_operations:
                raise ValueError("S04 is not at the resumable receipt boundary")

            sent = self._poll_delivery(delivery_id, "sent")
            if (
                sent.get("provider_identity") != provider_identity
                or self._attempt_ids(sent) != receipt.get("attempt_ids")
            ):
                raise ValueError("S04 live Delivery drifted from the reviewed receipt")

            listed = expect(
                self.admin.request("GET", "/api/v1/admin/notification-destinations"), {200}
            ).body
            destination = next(
                (
                    item for item in listed.get("destinations", [])
                    if item.get("id") == destination_id
                ),
                None,
            )
            if (
                not destination
                or destination.get("configuration_revision") != real_revision
                or destination.get("enabled") is not False
            ):
                raise ValueError("S04 Destination drifted before receipt resume")

            def operation_id(suffix: str) -> str:
                value = f"{execution.execution_id}:s04-{suffix}"
                self.evidence.bind_operation(
                    "S04", kind="notification_mutation", operation_id=value,
                )
                return value

            reauthenticate(self.admin, admin_password, "s04-select")
            activated = expect(
                self.admin.request(
                    "PATCH",
                    f"/api/v1/admin/notification-destinations/{destination_id}",
                    body={
                        "enabled": True,
                        "expected_revision": real_revision,
                        "reason": "A01 activate exact verified Notification revision",
                    },
                    request_id=operation_id("activate"),
                ),
                {200},
            ).body["destination"]
            if (
                not activated.get("enabled")
                or activated.get("configuration_revision") != real_revision
            ):
                raise ValueError("verified Notification revision was not activated")
            selected = expect(
                self.admin.request(
                    "POST",
                    f"/api/v1/admin/notification-destinations/{destination_id}/select-pilot-route",
                    body={
                        "expected_revision": real_revision,
                        "reason": "A01 select exact verified Pilot catch-all route",
                    },
                    request_id=operation_id("select-route"),
                ),
                {200},
            ).body["destination"]
            routes = expect(
                self.admin.request("GET", "/api/v1/admin/notification-routes"), {200}
            ).body.get("routes", [])
            pilot_route = next(
                (item for item in routes if item.get("id") == "route:pilot-catch-all"),
                None,
            )
            platform = expect(
                self.admin.request("GET", "/api/v1/platform/status"), {200}
            ).body["capabilities"]["notification"]
            if (
                not selected.get("pilot_route_selected")
                or not pilot_route
                or pilot_route.get("selected_destination_revision") != real_revision
                or platform.get("readiness") != "ready"
                or platform.get("configuration_revision") != real_revision
            ):
                raise ValueError("repaired Notification revision is not selected and ready")
            artifacts.append(
                self.evidence.write_json(
                    "S04",
                    "sent-and-selected.json",
                    {
                        "destination_id": destination_id,
                        "revision": real_revision,
                        "delivery_id": sent["id"],
                        "status": sent["status"],
                        "attempt_count": sent.get("attempt_count"),
                        "attempt_ids": self._attempt_ids(sent),
                        "provider_identity": provider_identity,
                        "pilot_route_selected": True,
                        "route_id": pilot_route["id"],
                        "route_revision": pilot_route["selected_destination_revision"],
                        "platform_readiness": "ready",
                    },
                    known_secrets=secrets,
                )
            )
            self.evidence.record_gate(
                "S04", "passed", artifacts, started_at=execution.started_at,
            )
        except Exception as exc:
            fail_gate(
                self.evidence, "S04", artifacts, exc, secrets, execution.started_at,
            )

    def _test(
        self, destination_id: str, revision: str, suffix: str, *, request_id: str,
    ) -> dict[str, Any]:
        return expect(
            self.admin.request(
                "POST",
                f"/api/v1/admin/notification-destinations/{destination_id}/test",
                body={
                    "expected_revision": revision,
                    "reason": f"A01 {suffix} Notification delivery probe",
                },
                request_id=request_id,
            ),
            {202},
        ).body["verification"]

    def _poll_delivery(self, delivery_id: str, expected_status: str) -> dict[str, Any]:
        for _ in range(300):
            body = expect(
                self.admin.request("GET", "/api/v1/admin/notification-deliveries"), {200}
            ).body
            delivery = next(
                (item for item in body.get("deliveries", []) if item.get("id") == delivery_id),
                None,
            )
            if delivery and delivery.get("status") == expected_status:
                return delivery
            self.sleep(3)
        raise TimeoutError(f"Notification Delivery did not reach {expected_status} within 15m")

    @staticmethod
    def _attempt_ids(delivery: dict[str, Any]) -> list[str]:
        attempt_ids = [str(item.get("id", "")) for item in delivery.get("attempts", [])]
        if not attempt_ids or any(not item for item in attempt_ids):
            raise ValueError("Notification Delivery did not expose durable attempt IDs")
        return attempt_ids
