"""A01 real Notification dead-letter-to-sent and route-selection gate."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .evidence import AcceptanceEvidence, Artifact
from .http import GatewaySession
from .integration_support import expect, fail_gate, reauthenticate, string_values


SUPPORTED_NOTIFICATION_PROVIDERS = ("feishu", "dingtalk", "smtp")
INVALID_NOTIFICATION_CONFIGS = {
    "feishu": {"webhook_url": "https://127.0.0.1:1/acceptance-invalid"},
    "dingtalk": {
        "webhook_url": "https://127.0.0.1:1/acceptance-invalid",
        "signing_secret": "acceptance-invalid-signing-secret",
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
        confirm_receipt: Callable[[str], None],
    ) -> None:
        started_at = self.evidence.start_gate("S04")
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
                    request_id="acceptance-s04-invalid-create",
                ),
                {201},
            ).body["destination"]
            destination_id = created["id"]
            invalid_revision = created["configuration_revision"]
            invalid_test = self._test(destination_id, invalid_revision, "invalid")
            dead = self._poll_delivery(invalid_test["delivery_id"], "dead_letter")
            reauthenticate(self.admin, admin_password, "s04-real")
            repaired = expect(
                self.admin.request(
                    "PATCH",
                    f"/api/v1/admin/notification-destinations/{destination_id}",
                    body={
                        "enabled": True,
                        "config": inputs.config,
                        "expected_revision": invalid_revision,
                        "reason": "A01 repair Notification Destination with real provider",
                    },
                    request_id="acceptance-s04-real-save",
                ),
                {200},
            ).body["destination"]
            real_revision = repaired["configuration_revision"]
            real_test = self._test(destination_id, real_revision, "real")
            sent = self._poll_delivery(real_test["delivery_id"], "sent")
            confirm_receipt(real_test["delivery_id"])
            self.evidence.require_verified_attestation(
                "S04", role="platform_administrator"
            )
            reauthenticate(self.admin, admin_password, "s04-select")
            selected = expect(
                self.admin.request(
                    "POST",
                    f"/api/v1/admin/notification-destinations/{destination_id}/select-pilot-route",
                    body={
                        "expected_revision": real_revision,
                        "reason": "A01 select exact verified Pilot catch-all route",
                    },
                    request_id="acceptance-s04-select-route",
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
            artifacts.extend(
                [
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
                    ),
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
                            "pilot_route_selected": True,
                            "route_id": pilot_route["id"],
                            "route_revision": pilot_route["selected_destination_revision"],
                            "platform_readiness": "ready",
                        },
                        known_secrets=secrets,
                    ),
                ]
            )
            self.evidence.record_gate("S04", "passed", artifacts, started_at=started_at)
        except Exception as exc:
            fail_gate(self.evidence, "S04", artifacts, exc, secrets, started_at)

    def _test(self, destination_id: str, revision: str, suffix: str) -> dict[str, Any]:
        return expect(
            self.admin.request(
                "POST",
                f"/api/v1/admin/notification-destinations/{destination_id}/test",
                body={
                    "expected_revision": revision,
                    "reason": f"A01 {suffix} Notification delivery probe",
                },
                request_id=f"acceptance-s04-{suffix}-test",
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
