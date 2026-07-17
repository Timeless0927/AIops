"""Fixed Kubernetes cleanup and public Gateway reads for C01-C02."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .cleanup import CleanupHistoryScope, CleanupScope
from .command import CommandExecutor, CommandResult


_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9/._:-]{0,299}")


class KubectlCleanupAdapter:
    def __init__(self, commands: CommandExecutor, *, kube_context: str) -> None:
        if not kube_context:
            raise ValueError("C01 Kubernetes Adapter requires the frozen context")
        self.commands = commands
        self.kube_context = kube_context

    def snapshot(self, scope: CleanupScope) -> dict[str, object]:
        self._require_scope(scope)
        fixture = self._get("namespace", "aiops-verification")
        return {
            "system_namespace": self._get("namespace", "aiops-system"),
            "system_resources": self._system_resources(),
            "fixture_namespace": fixture,
            "run_job": self._get(
                "job", "verification-trigger", namespace="aiops-verification",
            ) if fixture is not None else None,
        }

    def delete_run(
        self, scope: CleanupScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object]:
        self._require_request(scope, before, operation_id)
        self._delete(scope.release_root / "verification/run")
        return {"operation_id": operation_id, **self.snapshot(scope)}

    def reconcile_delete_run(
        self, scope: CleanupScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None:
        self._require_request(scope, before, operation_id)
        value = {"operation_id": operation_id, **self.snapshot(scope)}
        return value if value["run_job"] is None and value["fixture_namespace"] else None

    def delete_base(
        self, scope: CleanupScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object]:
        self._require_request(scope, before, operation_id)
        self._delete(scope.release_root / "verification/base")
        return {"operation_id": operation_id, **self.snapshot(scope)}

    def reconcile_delete_base(
        self, scope: CleanupScope, before: dict[str, object], *, operation_id: str,
    ) -> dict[str, object] | None:
        self._require_request(scope, before, operation_id)
        value = {"operation_id": operation_id, **self.snapshot(scope)}
        return value if value["fixture_namespace"] is None and value["run_job"] is None else None

    def _delete(self, path: Any) -> None:
        result = self._run([
            "delete", "-k", str(path), "--ignore-not-found", "--wait=true",
        ], timeout=180)
        if result.exit_code != 0:
            raise RuntimeError(f"C01 fixed fixture delete failed: {path.name}")

    def _get(
        self, kind: str, name: str, *, namespace: str | None = None,
    ) -> dict[str, object] | None:
        args = ["get", kind, name]
        if namespace is not None:
            args.extend(["-n", namespace])
        args.extend(["--ignore-not-found", "-o", "json"])
        result = self._run(args, timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(f"C01 Kubernetes read failed: {kind}/{name}")
        if not result.stdout.strip():
            return None
        try:
            value = json.loads(result.stdout)
            metadata = value["metadata"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"C01 Kubernetes identity is invalid: {kind}/{name}") from exc
        result_value = {"name": metadata.get("name"), "uid": metadata.get("uid")}
        if namespace is not None:
            result_value["namespace"] = metadata.get("namespace")
        if any(not isinstance(item, str) or not item for item in result_value.values()):
            raise ValueError(f"C01 Kubernetes identity is incomplete: {kind}/{name}")
        return result_value

    def _system_resources(self) -> list[dict[str, str]]:
        result = self._run([
            "get", "deployment,daemonset,statefulset,service,pvc,configmap",
            "-n", "aiops-system", "-o", "json",
        ], timeout=30)
        if result.exit_code != 0:
            raise RuntimeError("C01 aiops-system resource inventory read failed")
        try:
            items = json.loads(result.stdout).get("items")
        except (AttributeError, json.JSONDecodeError) as exc:
            raise ValueError("C01 aiops-system resource inventory is invalid") from exc
        if not isinstance(items, list):
            raise ValueError("C01 aiops-system resource inventory is invalid")
        projected = [{
            "kind": str(item.get("kind") or ""),
            "name": str(item.get("metadata", {}).get("name") or ""),
            "uid": str(item.get("metadata", {}).get("uid") or ""),
        } for item in items if isinstance(item, dict)]
        if len(projected) != len(items) or any(not all(item.values()) for item in projected):
            raise ValueError("C01 aiops-system resource identity is incomplete")
        return sorted(projected, key=lambda item: (item["kind"], item["name"]))

    def _run(self, args: list[str], *, timeout: int) -> CommandResult:
        return self.commands.run(
            ["kubectl", "--context", self.kube_context, *args], timeout=timeout,
        )

    @staticmethod
    def _require_scope(scope: CleanupScope) -> None:
        paths = (
            scope.release_root / "verification/run/kustomization.yaml",
            scope.release_root / "verification/base/kustomization.yaml",
        )
        if (
            not scope.release_root.is_absolute()
            or not scope.expected_run_id
            or any(path.is_symlink() or not path.is_file() for path in paths)
        ):
            raise ValueError("C01 fixed cleanup scope is invalid")

    @classmethod
    def _require_request(
        cls, scope: CleanupScope, before: dict[str, object], operation_id: str,
    ) -> None:
        cls._require_scope(scope)
        if (
            _OPERATION_ID.fullmatch(operation_id) is None
            or before.get("system_namespace") is None
            or before.get("fixture_namespace") is None
        ):
            raise ValueError("C01 durable cleanup request is invalid")


class GatewayCleanupHistoryAdapter:
    """Reads two-round history only through actor-scoped public HTTP projections."""

    def __init__(
        self, *, user: Any, notification_admin: Any,
        sleep: Callable[[float], None] = time.sleep, attempts: int = 150,
    ) -> None:
        if attempts < 1:
            raise ValueError("C02 history Adapter requires positive attempts")
        self.user = user
        self.notification_admin = notification_admin
        self.sleep = sleep
        self.attempts = attempts

    def read(self, scope: CleanupHistoryScope) -> dict[str, object]:
        workbench = self._request(
            self.user, f"/api/v1/incidents/{scope.incident_id}/workbench",
        )
        report = self._request(
            self.user, f"/api/v1/incidents/{scope.incident_id}/report",
        )
        publications = report.get("publications")
        reports = self._select(
            publications, scope.identity_inventory["report_ids"], "C02 Incident Report",
        )
        report_v2 = reports[1]
        change_ids = scope.identity_inventory["change_request_ids"]
        executions = [
            self._request(
                self.user, f"/api/v1/change-requests/{change_id}/phase-execution",
            ).get("phase_execution")
            for change_id in change_ids
        ]
        if any(not isinstance(item, dict) for item in executions):
            raise ValueError("C02 public Phase Execution history is incomplete")
        delivery_body = self._request(
            self.notification_admin, "/api/v1/admin/notification-deliveries",
        )
        deliveries = self._select(
            delivery_body.get("deliveries"),
            scope.identity_inventory["delivery_ids"], "C02 Notification Delivery",
        )
        resource = self._resource(scope)
        incident = workbench.get("incident")
        investigation = workbench.get("investigation")
        if (
            not isinstance(incident, dict) or incident.get("id") != scope.incident_id
            or not isinstance(investigation, dict)
            or investigation.get("id") != scope.identity_inventory["investigation_ids"][1]
        ):
            raise ValueError("C02 current Incident projection is not the second round")
        return {
            "incident_id": scope.incident_id,
            "identity_inventory": self._inventory(
                report_v2, executions, reports, deliveries,
                expected=scope.identity_inventory,
            ),
            "phase_executions": executions,
            "reports": reports,
            "notification_deliveries": deliveries,
            "resource": resource,
        }

    def _resource(self, scope: CleanupHistoryScope) -> dict[str, object]:
        for attempt in range(self.attempts):
            body = self._request(self.user, "/api/v1/resources")
            matches = [
                item for item in body.get("resources", [])
                if isinstance(item, dict) and item.get("id") == scope.deployment_target_id
            ]
            if len(matches) > 1:
                raise ValueError("C02 Deployment Target projection is ambiguous")
            if len(matches) == 1 and matches[0].get("availability") == "unavailable":
                return matches[0]
            if attempt + 1 < self.attempts:
                self.sleep(2)
        raise TimeoutError("C02 deleted Deployment Target did not become unavailable")

    @staticmethod
    def _inventory(
        report_v2: dict[str, object], executions: list[dict[str, object]],
        reports: list[dict[str, object]], deliveries: list[dict[str, object]],
        *, expected: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        facts = report_v2.get("facts")
        history = report_v2.get("decision_action_history")
        if not isinstance(facts, dict) or not isinstance(history, dict):
            raise ValueError("C02 Report v2 omitted frozen public history")
        changes = history.get("change_requests")
        phases = [
            phase for change in changes if isinstance(change, dict)
            for phase in change.get("phases", []) if isinstance(phase, dict)
        ] if isinstance(changes, list) else []
        observed = {
            "investigation_ids": _ids(facts.get("investigations")),
            "evidence_step_ids": _ids(facts.get("evidence_steps")),
            "recommended_action_ids": _ids(history.get("recommended_actions")),
            "change_request_ids": _ids(changes),
            "phase_ids": _ids(phases),
            "revision_ids": _ids([
                revision for phase in phases
                for revision in phase.get("revisions", []) if isinstance(revision, dict)
            ]),
            "approval_ids": _ids([
                phase.get("approval") for phase in phases
                if isinstance(phase.get("approval"), dict)
            ]),
            "grant_ids": _ids([
                item.get("grant") for item in executions if isinstance(item.get("grant"), dict)
            ]),
            "command_ids": [
                str(step["command_id"])
                for item in executions
                for step in item.get("steps", [])
                if isinstance(step, dict) and step.get("command_id")
            ],
            "execution_ids": _ids(executions),
            "recovery_observation_ids": _ids(facts.get("recovery_observations")),
            "report_ids": _ids(reports),
            "delivery_ids": _ids(deliveries),
        }
        if any(
            len(values) != len(set(values))
            or any(item not in values for item in expected[key])
            for key, values in observed.items()
        ):
            raise ValueError("C02 public history omitted or duplicated a frozen identity")
        return {key: list(values) for key, values in expected.items()}

    @staticmethod
    def _select(
        values: object, expected_ids: list[str], label: str,
    ) -> list[dict[str, object]]:
        if not isinstance(values, list):
            raise ValueError(f"{label} projection is not a list")
        selected = []
        for expected_id in expected_ids:
            matches = [
                item for item in values
                if isinstance(item, dict) and item.get("id") == expected_id
            ]
            if len(matches) != 1:
                raise ValueError(f"{label} identity is missing or duplicated")
            selected.append(matches[0])
        return selected

    @staticmethod
    def _request(session: Any, path: str) -> dict[str, object]:
        response = session.request("GET", path)
        if response.status != 200 or not isinstance(response.body, dict):
            raise RuntimeError(f"C02 public read failed: {path}")
        return response.body


def _ids(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [
        str(item["id"]) for item in values
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]
