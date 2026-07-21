from __future__ import annotations

from copy import deepcopy

import pytest

from aiops.acceptance.run_one_decisions import (
    select_v03_action,
    signal_fingerprints,
    v02_public_fact,
    v02_ready,
)


def _telemetry() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "observed_at": 1100.0,
        "fault_metric_series": 1,
        "deployment_unavailable_series": 1,
        "activation_log_lines": 1,
        "prometheus_alerts": [{"labels_sha256": "1" * 64, "state": "firing"}],
        "alertmanager_alerts": [{
            "fingerprint": "fingerprint-1",
            "labels_sha256": "1" * 64,
            "status": "active",
        }],
    }


def _public() -> dict[str, object]:
    return {
        "incident": {"id": "incident-1", "created_at": 1110.0},
        "investigation": {
            "id": "investigation-1",
            "status": "running",
            "created_at": 1110.0,
        },
        "alert_signals": [{
            "fingerprint": "fingerprint-1",
            "alertname": "AIOpsVerificationWorkloadUnavailable",
            "status": "firing",
            "workload_kind": "Deployment",
            "workload_name": "verification-api",
            "started_at": 1105.0,
            "created_at": 1110.0,
            "firing_webhook_request_id": "webhook-1",
        }],
    }


def _scope() -> dict[str, object]:
    return {
        "cluster_id": "pilot-cluster",
        "namespace": "aiops-verification",
        "workload_kind": "Deployment",
        "workload_name": "verification-api",
    }


def _workbench() -> dict[str, object]:
    steps = [
        {
            "id": "step-prometheus",
            "source": "prometheus",
            "state": "succeeded",
            "scope": _scope(),
            "observed_at": 990.0,
            "expires_at": 1300.0,
        },
        {
            "id": "step-loki",
            "source": "loki",
            "state": "succeeded",
            "scope": _scope(),
            "observed_at": 991.0,
            "expires_at": 1300.0,
        },
        {
            "id": "step-k8s",
            "source": "k8s",
            "state": "succeeded",
            "scope": _scope(),
            "observed_at": 992.0,
            "expires_at": 1300.0,
        },
    ]
    return {
        "incident": {"id": "incident-1"},
        "investigation": {
            "id": "investigation-1",
            "status": "completed",
            "model_revision": "model-provider:revision-1",
        },
        "alert_signals": [{
            "fingerprint": "fingerprint-1",
            "alertname": "AIOpsVerificationWorkloadUnavailable",
            "status": "firing",
        }],
        "evidence_steps": steps,
        "judgment": {"evidence_gate_status": "complete"},
        "recommended_actions": [{
            "id": "action-1",
            "hash": "a" * 64,
            "summary": "Controlled restart",
            "change_intent": "controlled_restart",
            "target": _scope(),
            "evidence_step_ids": [item["id"] for item in steps],
            "gate": {"status": "complete"},
            "stale": False,
        }],
    }


def _model(revision: str = "model-provider:revision-1") -> dict[str, object]:
    return {
        "readiness": "ready",
        "configuration_revision": revision,
        "verification": {"state": "verified", "revision": revision},
        "availability": {"state": "available"},
    }


def test_v02_requires_exact_run_and_independent_label_identity() -> None:
    observed = _telemetry()
    assert signal_fingerprints(observed) == {"fingerprint-1"}
    assert v02_ready(
        observed,
        run_id="run-1",
        trigger_started_at=1000.0,
        telemetry_deadline_at=1120.0,
    )

    wrong_run = deepcopy(observed)
    wrong_run["run_id"] = "run-2"
    assert not v02_ready(
        wrong_run,
        run_id="run-1",
        trigger_started_at=1000.0,
        telemetry_deadline_at=1120.0,
    )
    observed["alertmanager_alerts"][0]["labels_sha256"] = "2" * 64  # type: ignore[index]
    assert signal_fingerprints(observed) == set()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fingerprint", "wrong-fingerprint"),
        ("firing_webhook_request_id", ""),
        ("started_at", 1181.0),
    ],
)
def test_v02_public_fact_fails_closed_on_wrong_correlation(
    field: str, value: object
) -> None:
    workbench = _public()
    workbench["alert_signals"][0][field] = value  # type: ignore[index]
    assert v02_public_fact(
        workbench,
        incident_id="incident-1",
        fingerprints={"fingerprint-1"},
        trigger_started_at=1000.0,
        public_deadline_at=1180.0,
    ) is None


def test_v03_accepts_one_exact_frozen_fresh_evidence_chain() -> None:
    decision = select_v03_action(
        _workbench(),
        incident_id="incident-1",
        investigation_id="investigation-1",
        alert_fingerprint="fingerprint-1",
        model=_model(),
        now=1000.0,
    )
    assert decision is not None
    assert decision.model_revision == "model-provider:revision-1"
    assert decision.action["id"] == "action-1"


def _incident_mismatch(workbench: dict[str, object]) -> None:
    workbench["incident"]["id"] = "incident-2"  # type: ignore[index]


def _alertname_mismatch(workbench: dict[str, object]) -> None:
    workbench["alert_signals"][0]["alertname"] = "OtherAlert"  # type: ignore[index]


def _duplicate_step_id(workbench: dict[str, object]) -> None:
    workbench["evidence_steps"][1]["id"] = "step-prometheus"  # type: ignore[index]


def _missing_step(workbench: dict[str, object]) -> None:
    workbench["recommended_actions"][0]["evidence_step_ids"].append("missing")  # type: ignore[index]


def _stale_step(workbench: dict[str, object]) -> None:
    workbench["evidence_steps"][0]["observed_at"] = 879.0  # type: ignore[index]


def _missing_source(workbench: dict[str, object]) -> None:
    workbench["recommended_actions"][0]["evidence_step_ids"].remove("step-k8s")  # type: ignore[index]


def _extra_source(workbench: dict[str, object]) -> None:
    workbench["evidence_steps"].append({  # type: ignore[union-attr]
        "id": "step-human",
        "source": "human",
        "state": "succeeded",
        "scope": _scope(),
        "observed_at": 999.0,
        "expires_at": 1300.0,
    })
    workbench["recommended_actions"][0]["evidence_step_ids"].append("step-human")  # type: ignore[index]


@pytest.mark.parametrize(
    "mutate",
    [
        _incident_mismatch,
        _alertname_mismatch,
        _duplicate_step_id,
        _missing_step,
        _stale_step,
        _missing_source,
        _extra_source,
    ],
)
def test_v03_rejects_identity_and_evidence_drift(mutate) -> None:
    workbench = _workbench()
    mutate(workbench)
    assert select_v03_action(
        workbench,
        incident_id="incident-1",
        investigation_id="investigation-1",
        alert_fingerprint="fingerprint-1",
        model=_model(),
        now=1000.0,
    ) is None


def test_v03_rejects_current_model_drift_from_frozen_revision() -> None:
    assert select_v03_action(
        _workbench(),
        incident_id="incident-1",
        investigation_id="investigation-1",
        alert_fingerprint="fingerprint-1",
        model=_model("model-provider:revision-2"),
        now=1000.0,
    ) is None
