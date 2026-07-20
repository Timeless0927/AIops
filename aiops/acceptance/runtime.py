"""Concrete P01-C03 command composition for the single-gate Conductor."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .adapters import HttpsProfileProbe, PlaywrightBrowser, PlaywrightV01Console
from .cleanup import CleanupGateRunner
from .cleanup_adapters import GatewayCleanupHistoryAdapter, KubectlCleanupAdapter
from .cluster_install import ClusterInstallRunner
from .command import SubprocessCommands
from .connector_gate import ConnectorGateRunner
from .credentials import (
    KubernetesBootstrapCredentialSource,
    RunCredentialStore,
    assert_public_payload,
)
from .dependency_degradation import DependencyDegradationGateRunner
from .dependency_loki import KubectlLokiDependencyProbe
from .dependency_probes import ConnectorPollGatewayProbe, GatewayDependencyProbe
from .evidence import AcceptanceEvidence
from .governed_change import GovernedChangeGateRunner
from .http import GatewaySession
from .model_gate import ModelGateRunner, ModelInputs
from .notification_gate import NotificationGateRunner, NotificationInputs
from .observability_gate import ObservabilityGateRunner
from .package_install import PackageInstallRunner, release_connector_identity
from .platform_status_gates import PlatformStatusGateRunner
from .recovery import RecoveryGateRunner
from .recovery_adapters import GatewayRecoveryProbe, KubernetesRecoveryAdapter
from .recovery_telemetry import KubectlRetentionTelemetry
from .rerun import RerunGateRunner, RerunInputs
from .rerun_adapters import GatewayRerunChainAdapter, KubectlRerunAdapter
from .run_one import RunOneGateRunner
from .stale_change import StaleChangeGateRunner
from .stale_change_adapters import KubectlStaleChangeAdapter
from .telemetry import KubernetesTelemetryProbe
from .verification_trigger import V01Inputs
from .web_gates import WebGateRunner


_CONFIG_FIELDS = {
    "format_version", "archive", "checksums", "work_dir", "acceptance_tool",
    "base_url", "https_profile", "usernames", "model", "notification_provider",
    "report_v1_narrative", "report_v2_narrative",
}
_USER_FIELDS = {"admin", "ordinary", "sre"}
_MODEL_FIELDS = {"endpoint", "endpoint_scope", "model", "timeout_seconds"}
_HTTPS_FIELDS = {"base_url", "ingress", "http_url", "require_redirect"}
_NARRATIVE_FIELDS = {
    "impact_summary", "root_cause_explanation", "resolution_summary", "follow_up_narrative",
}
RESUMABLE_GATES = frozenset({
    "I05", "V01", "V02", "V04", "V05", "V06", "V07",
    "R01", "R02", "R03", "R04", "R06", "V08", "C01", "C02", "C03",
})


class AcceptanceRuntime:
    """Build only the real command selected by the ledger-owned frontier."""

    def __init__(
        self,
        *,
        evidence: AcceptanceEvidence,
        config: dict[str, Any],
        source_root: Path,
        credential_store: Path | None,
        admission_verifier: Callable[[dict[str, Any]], None],
        attest: Callable[[str, str], None],
    ) -> None:
        self.evidence = evidence
        self.config = _validate_config(config)
        self.source_root = source_root.resolve()
        self.credential_store = credential_store
        self.admission_verifier = admission_verifier
        self.attest = attest
        self.commands = SubprocessCommands()

    @classmethod
    def from_file(
        cls,
        *,
        evidence: AcceptanceEvidence,
        config_path: Path,
        source_root: Path,
        credential_store: Path | None,
        admission_verifier: Callable[[dict[str, Any]], None],
        attest: Callable[[str, str], None],
    ) -> "AcceptanceRuntime":
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("acceptance runtime config is unreadable") from exc
        if not isinstance(config, dict):
            raise ValueError("acceptance runtime config must be an object")
        return cls(
            evidence=evidence, config=config, source_root=source_root,
            credential_store=credential_store, admission_verifier=admission_verifier,
            attest=attest,
        )

    def advance(self, gate_id: str) -> Any:
        if gate_id == "P01":
            return self._package().run_p01(
                self._path("archive"), self._path("checksums"), work_dir=self._path("work_dir"),
            )
        if gate_id == "P02":
            return self._package().run_p02(
                self._path("archive"), self._path("acceptance_tool"),
                admission_verifier=self.admission_verifier,
            )
        if gate_id in {"I01", "I02"}:
            return getattr(self._cluster(), f"run_{gate_id.lower()}")(self._release())
        if gate_id in {"I03", "I04", "I05"}:
            return self._advance_web(gate_id)
        if gate_id in {"S01", "S02", "S03", "S04", "S05", "S06"}:
            return self._advance_setup(gate_id)
        if gate_id in {"V01", "V02", "V03", "V04", "R05", "V05", "V06", "V07"}:
            return self._advance_run_one(gate_id)
        if gate_id in {"R01", "R02"}:
            return getattr(self._recovery(), f"run_{gate_id.lower()}")()
        if gate_id in {"R03", "R04"}:
            return getattr(self._dependencies(), f"run_{gate_id.lower()}")()
        if gate_id == "R06":
            return self._stale().run_r06(
                sre_username=self._username("sre"), sre_password=self._secret("sre-password"),
            )
        if gate_id == "V08":
            return self._rerun().run_v08(self._rerun_inputs())
        if gate_id == "C01":
            return self._cleanup().run_c01(self._release())
        if gate_id == "C02":
            return self._cleanup().run_c02()
        if gate_id == "C03":
            return self._cleanup().run_c03(known_secrets=self._known_secrets())
        raise ValueError(f"unsupported gate command: {gate_id}")

    def resume(self, gate_id: str) -> Any:
        if gate_id == "I05":
            return self._web().resume_i05(
                admin_username=self._username("admin"),
                admin_password=self._admin_password(),
                user_username=self._username("ordinary"),
                user_password=self._secret("ordinary-user-password"),
            )
        if gate_id == "V01":
            return self._run_one(reader=False).resume_v01()
        if gate_id == "V02":
            return self._run_one().resume_v02()
        if gate_id == "V04":
            return self._run_one().resume_v04()
        if gate_id == "V05":
            return self._run_one().resume_v05()
        if gate_id == "V06":
            return self._run_one().resume_v06()
        if gate_id == "V07":
            return self._run_one().resume_v07(
                notification_admin=self._admin_session(),
                sre_username=self._username("sre"), sre_password=self._secret("sre-password"),
            )
        if gate_id in {"R01", "R02"}:
            return getattr(self._recovery(), f"resume_{gate_id.lower()}")()
        if gate_id in {"R03", "R04"}:
            return getattr(self._dependencies(), f"resume_{gate_id.lower()}")()
        if gate_id == "R06":
            return self._stale().resume_r06(
                sre_username=self._username("sre"), sre_password=self._secret("sre-password"),
            )
        if gate_id == "V08":
            return self._rerun().resume_v08(self._rerun_inputs())
        if gate_id == "C01":
            return self._cleanup().resume_c01(self._release())
        if gate_id == "C02":
            return self._cleanup().resume_c02()
        if gate_id == "C03":
            return self._cleanup().resume_c03(known_secrets=self._known_secrets())
        raise ValueError(f"{gate_id} has no reconciliation command")

    def _advance_web(self, gate_id: str) -> Any:
        runner = self._web()
        admin_username = self._username("admin")
        admin_password = self._admin_password()
        if gate_id == "I03":
            return runner.run_i03(
                self._base_url(), admin_username=admin_username,
                admin_password=admin_password,
            )
        if gate_id == "I04":
            profile = self.config["https_profile"]
            identity = None
            if self.evidence.access_profile == "https_ingress":
                identity = HttpsProfileProbe(self.commands).probe(
                    profile["base_url"], ingress=profile["ingress"],
                    http_url=profile["http_url"], require_redirect=profile["require_redirect"],
                )
            return runner.run_i04(profile["base_url"], identity)
        return runner.run_i05(
            admin_username=admin_username, admin_password=admin_password,
            user_username=self._username("ordinary"),
            user_password=self._secret("ordinary-user-password"),
        )

    def _advance_setup(self, gate_id: str) -> Any:
        admin_password = self._admin_password()
        admin = self._login(self._username("admin"), admin_password)
        if gate_id in {"S01", "S02"}:
            ordinary_password = self._secret("ordinary-user-password")
            runner = PlatformStatusGateRunner(
                evidence=self.evidence, admin=admin,
                relogin=lambda: self._login(self._username("admin"), admin_password),
                stale_admin=lambda: self._login(self._username("admin"), admin_password),
                user=self._login(self._username("ordinary"), ordinary_password),
                browser=self._browser(), base_url=self._base_url(),
            )
            return (
                runner.run_s01(
                    admin_username=self._username("admin"), admin_password=admin_password,
                )
                if gate_id == "S01" else runner.run_s02(admin_password=admin_password)
            )
        if gate_id == "S03":
            model = self.config["model"]
            return ModelGateRunner(evidence=self.evidence, admin=admin).run_s03(
                ModelInputs(
                    model["endpoint"], model["endpoint_scope"], model["model"],
                    model["timeout_seconds"], self._secret("model-api-key"),
                ),
                admin_password=admin_password,
            )
        if gate_id == "S04":
            return NotificationGateRunner(evidence=self.evidence, admin=admin).run_s04(
                self._notification_inputs(), admin_password=admin_password,
                confirm_receipt=lambda _delivery_id: self.attest("S04", "platform_administrator"),
            )
        if gate_id == "S05":
            connector_id, cluster_id = release_connector_identity(self._release())
            return ConnectorGateRunner(
                evidence=self.evidence, admin=admin, commands=self.commands,
            ).run_s05(
                admin_password=admin_password, connector_id=connector_id, cluster_id=cluster_id,
                confirm_one_time=lambda: self.attest("S05", "platform_operator"),
            )
        return ObservabilityGateRunner(
            evidence=self.evidence, telemetry=KubernetesTelemetryProbe(self.commands),
        ).run_s06()

    def _advance_run_one(self, gate_id: str) -> Any:
        runner = self._run_one(reader=gate_id != "V01")
        if gate_id == "V01":
            return runner.run_v01(V01Inputs(
                self._release(), self._username("admin"), self._admin_password(),
                self._username("sre"), self._secret("sre-password"),
            ))
        v01 = self._artifact("V01", "run.json")
        if gate_id == "V02":
            return runner.run_v02(
                str(v01["run_id"]), trigger_started_at=float(v01["trigger_started_at"]),
            )
        v02 = self._artifact("V02", "public-incident.json")
        incident = _object(v02, "incident")
        investigation = _object(v02, "investigation")
        signal = _object(v02, "alert_signal")
        if gate_id == "V03":
            return runner.run_v03(
                run_id=str(v01["run_id"]), incident_id=str(incident["id"]),
                investigation_id=str(investigation["id"]),
                alert_fingerprint=str(signal["fingerprint"]),
            )
        v03 = self._artifact("V03", "diagnosis.json")
        action = _object(v03, "recommended_action")
        if gate_id == "V04":
            return runner.run_v04(
                run_id=str(v01["run_id"]), incident_id=str(incident["id"]),
                recommended_action_id=str(action["id"]),
                recommended_action_hash=str(action["hash"]),
                recommended_action_summary=str(action["summary"]),
                sre_username=self._username("sre"), sre_password=self._secret("sre-password"),
            )
        change = self._change_scope()
        if gate_id == "R05":
            return runner.run_r05(
                **change,
                no_authority_username=self._username("ordinary"),
                no_authority_password=self._secret("ordinary-user-password"),
            )
        if gate_id == "V05":
            return runner.run_v05(
                **change, sre_username=self._username("sre"),
                sre_password=self._secret("sre-password"),
            )
        if gate_id == "V06":
            return runner.run_v06(
                run_id=str(v01["run_id"]), incident_id=str(incident["id"]),
                investigation_id=str(investigation["id"]),
                alert_fingerprint=str(signal["fingerprint"]),
            )
        destination = self._artifact("S04", "sent-and-selected.json")
        return runner.run_v07(
            run_id=str(v01["run_id"]), incident_id=str(incident["id"]),
            investigation_id=str(investigation["id"]),
            destination_revision=str(destination["revision"]),
            narrative=dict(self.config["report_v1_narrative"]),
        )

    def _package(self) -> PackageInstallRunner:
        return PackageInstallRunner(evidence=self.evidence, commands=self.commands)

    def _cluster(self) -> ClusterInstallRunner:
        return ClusterInstallRunner(evidence=self.evidence, commands=self.commands)

    def _web(self) -> WebGateRunner:
        return WebGateRunner(
            evidence=self.evidence, anonymous=GatewaySession(self._base_url()),
            session_factory=lambda: GatewaySession(self._base_url()), browser=self._browser(),
        )

    def _run_one(self, *, reader: bool = True) -> RunOneGateRunner:
        return RunOneGateRunner(
            evidence=self.evidence, commands=self.commands, console=self._console(),
            base_url=self._base_url(),
            user=self._sre_session() if reader else None,
            telemetry=KubernetesTelemetryProbe(self.commands),
        )

    def _recovery(self) -> RecoveryGateRunner:
        return RecoveryGateRunner(evidence=self.evidence, adapter=self._recovery_adapter())

    def _recovery_adapter(self) -> KubernetesRecoveryAdapter:
        public = GatewayRecoveryProbe(
            user=self._sre_session(), admin=self._admin_session(),
            telemetry=KubectlRetentionTelemetry(
                self.commands, kube_context=self.evidence.kube_context,
            ),
        )
        return KubernetesRecoveryAdapter(
            commands=self.commands, public_probe=public, release_root=self._release(),
            candidate_sha256=self.evidence.candidate_sha256,
            kube_context=self.evidence.kube_context,
            cluster_identity_sha256=self.evidence.cluster_identity_sha256,
        )

    def _dependencies(self) -> DependencyDegradationGateRunner:
        connector = KubernetesBootstrapCredentialSource(self.commands).read(
            kube_context=self.evidence.kube_context,
            secret_name="aiops-connector-secret", key="AIOPS_CONNECTOR_CREDENTIAL",
        ).value
        probe = GatewayDependencyProbe(
            base_url=self._base_url(), user=self._sre_session(), admin=self._admin_session(),
            browser=self._console(),
            connector_poll=ConnectorPollGatewayProbe(self._base_url(), connector),
            loki=KubectlLokiDependencyProbe(
                self.commands, GatewaySession(self._base_url()),
                kube_context=self.evidence.kube_context,
            ),
            sre_username=self._username("sre"), sre_password=self._secret("sre-password"),
            admin_username=self._username("admin"), admin_password=self._admin_password(),
        )
        return DependencyDegradationGateRunner(
            evidence=self.evidence, effects=self._recovery_adapter(), probe=probe,
        )

    def _stale(self) -> StaleChangeGateRunner:
        inventory = self.evidence.passed_artifact("P01", "artifact-inventory.json")
        effects = KubectlStaleChangeAdapter(
            self.commands, kube_context=self.evidence.kube_context,
            candidate_sha256=self.evidence.candidate_sha256,
            release_inventory_sha256=inventory.sha256,
            cluster_identity_sha256=self.evidence.cluster_identity_sha256,
        )
        return StaleChangeGateRunner(
            evidence=self.evidence, effects=effects, console=self._console(),
            user=self._sre_session(), base_url=self._base_url(),
        )

    def _rerun(self) -> RerunGateRunner:
        return RerunGateRunner(
            evidence=self.evidence,
            effects=KubectlRerunAdapter(
                self.commands, kube_context=self.evidence.kube_context,
            ),
            chain=GatewayRerunChainAdapter(
                console=self._console(), user=self._sre_session(),
                notification_admin=self._admin_session(),
                telemetry=KubernetesTelemetryProbe(self.commands), base_url=self._base_url(),
            ),
        )

    def _cleanup(self) -> CleanupGateRunner:
        return CleanupGateRunner(
            evidence=self.evidence,
            effects=KubectlCleanupAdapter(
                self.commands, kube_context=self.evidence.kube_context,
            ),
            history=GatewayCleanupHistoryAdapter(
                user=self._sre_session(), notification_admin=self._admin_session(),
            ),
        )

    def _change_scope(self) -> dict[str, str]:
        value = self._artifact("V04", "change-plan.json")
        review = _object(value, "phase_review")
        changes = review.get("changes")
        if not isinstance(changes, list) or len(changes) != 1 or not isinstance(changes[0], dict):
            raise ValueError("V04 exact Change scope is incomplete")
        change = changes[0]
        request = _object(value, "change_request")
        intent = self._artifact("V04", "intent.json")
        return {
            "run_id": str(value["run_id"]), "incident_id": str(intent["incident_id"]),
            "change_request_id": str(request["id"]), "phase_id": str(review["phase_id"]),
            "revision_id": str(review["revision_id"]),
            "dry_run_hash": str(change["dry_run_hash"]),
            "target_confirmation": str(change["target_confirmation"]),
        }

    def _rerun_inputs(self) -> RerunInputs:
        return RerunInputs(
            release_root=self._release(),
            sre_username=self._username("sre"), sre_password=self._secret("sre-password"),
            no_authority_username=self._username("ordinary"),
            no_authority_password=self._secret("ordinary-user-password"),
            platform_admin_username=self._username("admin"),
            platform_admin_password=self._admin_password(),
            narrative=dict(self.config["report_v2_narrative"]),
        )

    def _artifact(self, gate_id: str, name: str) -> dict[str, Any]:
        value = self.evidence.passed_artifact_json(gate_id, name)["value"]
        if not isinstance(value, dict):
            raise ValueError(f"{gate_id} {name} is not an object")
        return value

    def _release(self) -> Path:
        return self._package().prepare_release(
            self._path("archive"), self._path("checksums"), work_dir=self._path("work_dir"),
        )

    def _browser(self) -> PlaywrightBrowser:
        return PlaywrightBrowser(commands=self.commands, source_root=self.source_root)

    def _console(self) -> PlaywrightV01Console:
        return PlaywrightV01Console(
            commands=self.commands, source_root=self.source_root, evidence=self.evidence,
        )

    def _store(self) -> RunCredentialStore:
        if self.credential_store is None:
            raise ValueError("this gate requires the run-scoped credential store")
        return RunCredentialStore(
            self.credential_store, workspace=self.source_root, evidence_root=self.evidence.root,
        ).open()

    def _secret(self, name: str) -> str:
        return self._store().read(name).reveal()

    def _admin_password(self) -> str:
        return KubernetesBootstrapCredentialSource(self.commands).read(
            kube_context=self.evidence.kube_context,
        ).value.reveal()

    def _known_secrets(self) -> tuple[str, ...]:
        notification = self._secret("notification-config")
        values = [
            self._admin_password(), self._secret("ordinary-user-password"),
            self._secret("sre-password"), self._secret("model-api-key"), notification,
        ]
        values.extend(_secret_strings(json.loads(notification)))
        values.append(KubernetesBootstrapCredentialSource(self.commands).read(
            kube_context=self.evidence.kube_context,
            secret_name="aiops-connector-secret", key="AIOPS_CONNECTOR_CREDENTIAL",
        ).value.reveal())
        return tuple(dict.fromkeys(item for item in values if item))

    def _notification_inputs(self) -> NotificationInputs:
        try:
            value = json.loads(self._secret("notification-config"))
        except json.JSONDecodeError as exc:
            raise ValueError("notification credential input is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("notification credential input must be an object")
        return NotificationInputs(self.config["notification_provider"], value)

    def _login(self, username: str, password: str) -> GatewaySession:
        session = GatewaySession(self._base_url())
        response = session.request(
            "POST", "/auth/login",
            body={"username": username, "password": password, "session_mode": "cookie"},
            csrf=False,
        )
        if response.status != 200:
            raise RuntimeError(f"login for {username!r} returned HTTP {response.status}")
        return session

    def _admin_session(self) -> GatewaySession:
        return self._login(self._username("admin"), self._admin_password())

    def _sre_session(self) -> GatewaySession:
        return self._login(self._username("sre"), self._secret("sre-password"))

    def _path(self, name: str) -> Path:
        return Path(self.config[name])

    def _username(self, role: str) -> str:
        return str(self.config["usernames"][role])

    def _base_url(self) -> str:
        return str(self.config["base_url"])


def _validate_config(value: dict[str, Any]) -> dict[str, Any]:
    assert_public_payload(value)
    if set(value) != _CONFIG_FIELDS or value.get("format_version") != 1:
        raise ValueError("acceptance runtime config fields are invalid")
    for name in ("archive", "checksums", "work_dir", "acceptance_tool"):
        path = Path(str(value.get(name, "")))
        if not path.is_absolute():
            raise ValueError(f"acceptance runtime {name} must be absolute")
    base = urllib.parse.urlsplit(str(value.get("base_url", "")))
    if base.scheme not in {"http", "https"} or not base.netloc or base.query or base.fragment:
        raise ValueError("acceptance runtime base_url is invalid")
    users = value.get("usernames")
    model = value.get("model")
    https = value.get("https_profile")
    if (
        not isinstance(users, dict) or set(users) != _USER_FIELDS
        or any(not isinstance(item, str) or not item for item in users.values())
        or not isinstance(model, dict) or set(model) != _MODEL_FIELDS
        or not isinstance(model.get("timeout_seconds"), int)
        or not all(isinstance(model.get(name), str) and model[name] for name in _MODEL_FIELDS - {"timeout_seconds"})
        or not isinstance(https, dict) or set(https) != _HTTPS_FIELDS
        or not isinstance(https.get("require_redirect"), bool)
        or value.get("notification_provider") not in {"feishu", "dingtalk", "smtp"}
    ):
        raise ValueError("acceptance runtime actor or integration config is invalid")
    for name in ("report_v1_narrative", "report_v2_narrative"):
        narrative = value.get(name)
        if (
            not isinstance(narrative, dict) or set(narrative) != _NARRATIVE_FIELDS
            or any(not isinstance(item, str) or not item or len(item) > 4000 for item in narrative.values())
        ):
            raise ValueError("acceptance runtime Report narrative is invalid")
    return json.loads(json.dumps(value))


def _object(value: dict[str, Any], name: str) -> dict[str, Any]:
    item = value.get(name)
    if not isinstance(item, dict):
        raise ValueError(f"public artifact omitted {name}")
    return item


def _secret_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _secret_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _secret_strings(child)]
    return []
