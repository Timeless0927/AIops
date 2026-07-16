"""First Run Alert-to-Report acceptance gates."""

from __future__ import annotations

import math

from .evidence import Artifact, GateExecution
from .governed_change import GovernedChangeGateRunner
from .integration_support import fail_gate
from .run_one_decisions import (
    is_verification_incident,
    select_v03_action,
    signal_fingerprints,
    valid_run_id,
    v02_ready,
    v02_public_fact,
)
from .verification_trigger import V01Inputs


class RunOneGateRunner(GovernedChangeGateRunner):
    def run_v02(
        self,
        run_id: str,
        *,
        trigger_started_at: float,
        attempts: int = 90,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V02")
        artifacts: list[Artifact] = []
        try:
            if (
                not valid_run_id(run_id)
                or self.user is None
                or self.telemetry is None
                or not math.isfinite(trigger_started_at)
                or trigger_started_at < 0
                or attempts < 1
            ):
                raise ValueError("V02 requires run identity, User session and telemetry probe")
            intent = {
                "run_id": run_id,
                "trigger_started_at": trigger_started_at,
                "telemetry_deadline_at": trigger_started_at + 120,
                "public_deadline_at": trigger_started_at + 180,
            }
            artifacts.append(
                self.evidence.write_json("V02", "deadline-intent.json", intent)
            )
            return self._complete_v02(
                run_id=run_id,
                intent=intent,
                artifacts=artifacts,
                started_at=started_at,
                attempts=attempts,
                resume=False,
            )
        except Exception as exc:
            fail_gate(self.evidence, "V02", artifacts, exc, (), started_at)

    def resume_v02(self, *, attempts: int = 90) -> dict[str, str]:
        execution = self.evidence.resume_gate("V02")
        artifacts = list(execution.artifacts)
        try:
            intent = self._artifact_json(artifacts, "deadline-intent.json")
            run_id = str(intent.get("run_id") or "")
            if not run_id or attempts < 1:
                raise ValueError("V02 durable deadline intent is invalid")
            return self._complete_v02(
                run_id=run_id,
                intent=intent,
                artifacts=artifacts,
                started_at=execution.started_at,
                attempts=attempts,
                resume=True,
                execution=execution,
            )
        except Exception as exc:
            self._reconcile_unresolved(
                "V02",
                execution,
                outcome="unprovable",
                public_fact={"terminal": False, "reason": "original_deadline_unprovable"},
            )
            fail_gate(
                self.evidence, "V02", artifacts, exc, (), execution.started_at
            )

    def _complete_v02(
        self,
        *,
        run_id: str,
        intent: dict[str, object],
        artifacts: list[Artifact],
        started_at: str,
        attempts: int,
        resume: bool,
        execution: GateExecution | None = None,
    ) -> dict[str, str]:
        if self.user is None or (not resume and self.telemetry is None):
            raise ValueError("V02 completion requires its public fact Adapters")
        if not valid_run_id(run_id):
            raise ValueError("V02 durable run identity is invalid")
        trigger_started_at = float(intent["trigger_started_at"])
        telemetry_deadline_at = float(intent["telemetry_deadline_at"])
        public_deadline_at = float(intent["public_deadline_at"])
        if (
            not all(math.isfinite(item) for item in (
                trigger_started_at, telemetry_deadline_at, public_deadline_at,
            ))
            or trigger_started_at < 0
            or telemetry_deadline_at != trigger_started_at + 120
            or public_deadline_at != trigger_started_at + 180
        ):
            raise ValueError("V02 durable deadline intent is invalid")
        monotonic_started_at = self.monotonic()
        wall_now = self.now()
        if not math.isfinite(monotonic_started_at) or not math.isfinite(wall_now):
            raise ValueError("V02 runner clocks are invalid")
        telemetry_deadline = monotonic_started_at + max(
            0.0, telemetry_deadline_at - wall_now
        )
        public_deadline = monotonic_started_at + max(
            0.0, public_deadline_at - wall_now
        )
        if resume:
            observed = self._artifact_json(artifacts, "signal-paths.json")
        else:
            observed: dict[str, object] | None = None
            for attempt in range(attempts):
                if self.monotonic() > telemetry_deadline:
                    break
                assert self.telemetry is not None
                candidate = self.telemetry.probe_v02(run_id)
                if (
                    self.monotonic() <= telemetry_deadline
                    and v02_ready(
                        candidate,
                        run_id=run_id,
                        trigger_started_at=trigger_started_at,
                        telemetry_deadline_at=telemetry_deadline_at,
                    )
                ):
                    observed = candidate
                    artifacts.append(
                        self.evidence.write_json("V02", "signal-paths.json", observed)
                    )
                    break
                if attempt + 1 < attempts and self.monotonic() < telemetry_deadline:
                    self.sleep(min(2.0, telemetry_deadline - self.monotonic()))
            if observed is None:
                raise TimeoutError("V02 telemetry did not converge within 120s")
        if not v02_ready(
            observed,
            run_id=run_id,
            trigger_started_at=trigger_started_at,
            telemetry_deadline_at=telemetry_deadline_at,
        ):
            raise ValueError("V02 persisted telemetry does not prove the original deadline")
        fingerprints = signal_fingerprints(observed)
        public: dict[str, object] | None = None
        for attempt in range(attempts):
            public = self._v02_public(
                fingerprints=fingerprints,
                trigger_started_at=trigger_started_at,
                public_deadline_at=public_deadline_at,
            )
            if public is not None and (resume or self.monotonic() <= public_deadline):
                break
            public = None
            if attempt + 1 >= attempts or self.monotonic() >= public_deadline:
                break
            self.sleep(min(2.0, public_deadline - self.monotonic()))
        if public is None:
            raise TimeoutError("V02 public Alert/Incident did not converge within 180s")
        artifacts.append(
            self.evidence.write_json("V02", "public-incident.json", public)
        )
        signal = public["alert_signal"]
        incident = public["incident"]
        investigation = public["investigation"]
        assert isinstance(signal, dict) and isinstance(incident, dict)
        assert isinstance(investigation, dict)
        if execution is None:
            execution = self.evidence.resume_gate("V02")
        if any(
            operation.get("kind") != "gate_execution"
            for operation in execution.operations
        ):
            raise ValueError("V02 operation journal contains an unexpected effect")
        self._reconcile_unresolved(
            "V02",
            execution,
            outcome="succeeded",
            public_fact={
                "run_id": run_id,
                "fingerprint": signal["fingerprint"],
                "webhook_request_id": signal["firing_webhook_request_id"],
                "incident_id": incident["id"],
                "investigation_id": investigation["id"],
                "terminal": True,
            },
        )
        self.evidence.record_gate("V02", "passed", artifacts, started_at=started_at)
        return {
            "run_id": run_id,
            "incident_id": str(incident["id"]),
            "investigation_id": str(investigation["id"]),
            "alert_fingerprint": str(signal["fingerprint"]),
            "webhook_request_id": str(signal["firing_webhook_request_id"]),
        }

    def _v02_public(
        self,
        *,
        fingerprints: set[str],
        trigger_started_at: float,
        public_deadline_at: float,
    ) -> dict[str, object] | None:
        assert self.user is not None
        incidents = self.user.request("GET", "/api/v1/incidents")
        if incidents.status != 200:
            raise RuntimeError(f"Incident list returned HTTP {incidents.status}")
        values = incidents.body.get("incidents")
        if not isinstance(values, list):
            raise ValueError("Incident list omitted its public projection")
        matches: list[dict[str, object]] = []
        for incident in values:
            if not isinstance(incident, dict) or not is_verification_incident(incident):
                continue
            incident_id = incident.get("id")
            if not isinstance(incident_id, str) or not incident_id:
                continue
            workbench = self.user.request(
                "GET", f"/api/v1/incidents/{incident_id}/workbench"
            )
            if workbench.status != 200:
                continue
            fact = v02_public_fact(
                workbench.body,
                incident_id=incident_id,
                fingerprints=fingerprints,
                trigger_started_at=trigger_started_at,
                public_deadline_at=public_deadline_at,
            )
            if fact is not None:
                matches.append(fact)
        if len(matches) > 1:
            raise ValueError("V02 correlation resolved more than one public Incident")
        return matches[0] if matches else None

    def run_v03(
        self,
        *,
        run_id: str,
        incident_id: str,
        investigation_id: str,
        alert_fingerprint: str,
        attempts: int = 300,
    ) -> dict[str, str]:
        started_at = self.evidence.start_gate("V03")
        artifacts: list[Artifact] = []
        try:
            if self.user is None or not all(
                (run_id, incident_id, investigation_id, alert_fingerprint)
            ):
                raise ValueError("V03 requires the exact public Incident and Investigation")
            accepted: dict[str, object] | None = None
            model: dict[str, object] | None = None
            action: dict[str, object] | None = None
            for attempt in range(attempts):
                platform = self.user.request("GET", "/api/v1/platform/status")
                workbench = self.user.request(
                    "GET", f"/api/v1/incidents/{incident_id}/workbench"
                )
                if platform.status != 200 or workbench.status != 200:
                    raise RuntimeError("V03 public status/workbench read failed")
                model = platform.body.get("capabilities", {}).get("model")
                decision = select_v03_action(
                    workbench.body,
                    incident_id=incident_id,
                    investigation_id=investigation_id,
                    alert_fingerprint=alert_fingerprint,
                    model=model,
                    now=self.now(),
                )
                investigation = workbench.body.get("investigation")
                if decision is not None:
                    action = decision.action
                    accepted = {
                        "run_id": run_id,
                        "incident": workbench.body.get("incident"),
                        "investigation": workbench.body.get("investigation"),
                        "alert_signal": decision.signal,
                        "evidence_steps": workbench.body.get("evidence_steps"),
                        "judgment": workbench.body.get("judgment"),
                        "recommended_action": action,
                        "model": model,
                    }
                    break
                investigation = workbench.body.get("investigation")
                if isinstance(investigation, dict) and investigation.get("status") not in {
                    "queued", "running",
                }:
                    raise ValueError(
                        "Investigation became terminal without a complete Evidence Gate"
                    )
                if attempt + 1 < attempts:
                    self.sleep(2)
            if accepted is None or model is None or action is None:
                raise TimeoutError("V03 Diagnosis did not complete within 10m")
            artifacts.append(
                self.evidence.write_json("V03", "diagnosis.json", accepted)
            )
            self.evidence.record_gate("V03", "passed", artifacts, started_at=started_at)
            return {
                "run_id": run_id,
                "incident_id": incident_id,
                "investigation_id": investigation_id,
                "model_revision": str(model["configuration_revision"]),
                "recommended_action_id": str(action["id"]),
                "recommended_action_hash": str(action["hash"]),
                "recommended_action_summary": str(action["summary"]),
            }
        except Exception as exc:
            fail_gate(self.evidence, "V03", artifacts, exc, (), started_at)
