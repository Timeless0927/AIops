"""ISSUE-A: 证据采集落库行为(succeeded/partial/skipped 落,failed 不落)。"""

from __future__ import annotations

from typing import Any

import pytest

from toolsets.incident_diagnosis import run_diagnosis_session


class RecordingStore:
    """记录 add_evidence / record_incident_diagnosis 调用的最小桩。"""

    def __init__(self) -> None:
        self.evidence: list[dict[str, Any]] = []

    async def add_evidence(
        self,
        incident_id: str,
        source_type: str,
        source_ref: str | None,
        summary: str,
        **kwargs: Any,
    ) -> int:
        self.evidence.append(
            {
                "incident_id": incident_id,
                "source_type": source_type,
                "source_ref": source_ref,
                "summary": summary,
                **kwargs,
            }
        )
        return len(self.evidence)

    async def record_incident_diagnosis(self, incident_id: str, diagnosis: dict[str, Any]) -> None:
        return None


def _succeeded_adapter(payload: dict[str, Any]):
    async def adapter(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "summary": "evidence collected",
            "data": payload,
            "evidence_refs": [{"ref_id": "ev-ref-1"}],
        }

    return adapter


def _zero_match_k8s_adapter():
    """succeeded 但 0 匹配 → observation 降级为 partial。"""

    async def adapter(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "summary": "0 matches",
            "data": {"items": [], "command": "kubectl get pods"},
            "evidence_refs": [{"ref_id": "k8s-ref"}],
        }

    return adapter


async def _raising_adapter(args: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("backend exploded")


@pytest.mark.asyncio
async def test_succeeded_and_skipped_observations_persist_failed_does_not() -> None:
    store = RecordingStore()
    incident = {
        "incident_id": "inc-1",
        "alert_name": "payment 5xx error rate",
        "service": "payment",
        "namespace": "prod",
        "start": "2026-06-23T00:00:00Z",
        "end": "2026-06-23T00:30:00Z",
    }
    # plan: metrics, logs, k8s_read, topology
    session = await run_diagnosis_session(
        incident,
        metrics_adapter=_succeeded_adapter({"series": [1, 2, 3]}),
        logs_adapter=_raising_adapter,  # failed → 不落
        k8s_read_adapter=_succeeded_adapter({"items": [{"name": "p"}], "command": "kubectl get pods"}),
        topology_adapter=None,  # adapter 缺失 → skipped → 落空 payload
        incident_store=store,
    )

    by_source = {e["source_type"]: e for e in store.evidence}
    # failed(logs adapter 抛错)不进 evidence 表
    assert "logs" not in by_source
    # succeeded / skipped 都落
    assert set(by_source) == {"metrics", "k8s_read", "topology"}

    # succeeded 存全量 payload + 时间窗 + collector_version
    metrics_ev = by_source["metrics"]
    assert metrics_ev["payload"] == {"series": [1, 2, 3]}
    assert metrics_ev["collector_version"]
    assert metrics_ev["window_start_ts"] is not None
    assert metrics_ev["window_end_ts"] is not None
    assert metrics_ev["confidence"] == pytest.approx(0.8)

    # skipped 存空 payload,summary 记 reason
    topo_ev = by_source["topology"]
    assert topo_ev["payload"] == {}
    assert "unavailable" in topo_ev["summary"].lower()

    assert session["status"] in {"partial", "diagnosed", "needs_human"}


@pytest.mark.asyncio
async def test_partial_observation_persists_with_low_confidence() -> None:
    store = RecordingStore()
    incident = {
        "incident_id": "inc-2",
        "alert_name": "pod CrashLoopBackOff",
        "service": "worker",
        "namespace": "prod",
    }
    # crashloop plan: run_k8s_read, query_logs
    await run_diagnosis_session(
        incident,
        k8s_read_adapter=_zero_match_k8s_adapter(),
        logs_adapter=_succeeded_adapter({"lines": ["boom"]}),
        incident_store=store,
    )

    k8s_ev = next(e for e in store.evidence if e["source_type"] == "k8s_read")
    # 0 匹配的 k8s 读是低 confidence 证据
    assert k8s_ev["confidence"] == pytest.approx(0.25)
    assert k8s_ev["payload"]  # partial 仍存部分 payload


@pytest.mark.asyncio
async def test_k8s_payload_is_redacted() -> None:
    store = RecordingStore()
    incident = {
        "incident_id": "inc-3",
        "alert_name": "pod CrashLoopBackOff",
        "service": "worker",
        "namespace": "prod",
    }
    secret_payload = {
        "command": "kubectl get pods",
        "items": [{"env": "API_TOKEN=supersecretvalue"}],
    }
    await run_diagnosis_session(
        incident,
        k8s_read_adapter=_succeeded_adapter(secret_payload),
        logs_adapter=None,
        incident_store=store,
    )

    k8s_ev = next(e for e in store.evidence if e["source_type"] == "k8s_read")
    assert "supersecretvalue" not in str(k8s_ev["payload"])
