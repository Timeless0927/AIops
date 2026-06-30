"""Tests for tests/export_incident.py (ADR-0003 replay campaign #4).

Strategy: build a real IncidentStore on a tmp_path DB, populate incident + trace +
evidence + case_profile + diagnosis via the store's own write methods, then assert
the exporter produces a harness-consumable fixture with correct field mapping.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# tests/ is on sys.path via conftest; export_incident lives beside replay_incident
from toolsets import incident_store as store_mod


# --- helpers -----------------------------------------------------------------

def _make_store(tmp_path: Path) -> store_mod.IncidentStore:
    return store_mod.IncidentStore(db_path=tmp_path / "incidents.db")


async def _seed_live_incident(tmp_path: Path) -> tuple[str, str, store_mod.IncidentStore]:
    """Populate a tmp DB with one incident: 2 trace steps + 2 evidence rows +
    case_profile + diagnosis_json. Returns (incident_id, session_id, store)."""
    s = _make_store(tmp_path)
    iid = await s.create_incident(
        alert_name="PodCrashLooping",
        namespace="demo-apps",
        cluster="dev-external",
        summary="demo-probe restarts climbing",
        service="demo-probe",
    )
    sid = "diagnosis-test-session-001"
    # two tool-use steps, same order as evidence
    await s.add_diagnosis_trace(
        session_id=sid, step_index=0, tool_name="query_logs",
        tool_args={"query": "{namespace=\"demo-apps\"}"}, observation_ref="ev_loki_query_logs_0",
        input_tokens=100, output_tokens=20,
    )
    await s.add_diagnosis_trace(
        session_id=sid, step_index=1, tool_name="run_k8s_read",
        tool_args={"resource": "pods"}, observation_ref="ev_k8s_run_k8s_read_1",
        input_tokens=200, output_tokens=30,
    )
    await s.add_evidence(
        iid, "logs", "ev_loki_query_logs_0", "Loki matched 50 lines",
        payload={"lines": ["OOMKilled"]}, collector_version="incident_diagnosis/llm-tooluse-v1",
    )
    await s.add_evidence(
        iid, "k8s_gateway", "ev_k8s_run_k8s_read_1", "pod restarted 3x",
        payload={"restarts": 3}, collector_version="incident_diagnosis/llm-tooluse-v1",
    )
    await s.upsert_case_profile(
        iid,
        incident_signature="PodCrashLooping|demo-apps",
        final_root_cause="demo-probe OOMKilled under memory pressure",
        root_cause_category="resource_pressure_memory",
        key_evidence_refs=["ev_loki_query_logs_0", "ev_k8s_run_k8s_read_1"],
        effective_actions=["raise memory limit", "rollout restart"],
    )
    # diagnosis_json = what the brain produced (recorded_prediction source)
    await s.record_incident_diagnosis(
        iid,
        {
            "root_cause_candidates": [{
                "cause": "memory leak drove RSS above limit",
                "category": "resource_pressure_memory",
                "confidence": {"score": 0.82, "level": "high"},
            }],
            "confidence": {"score": 0.82, "level": "high"},
        },
    )
    return iid, sid, s


# --- pure build_fixture ------------------------------------------------------

def test_build_fixture_maps_all_fields(tmp_path):
    iid, sid, s = asyncio.run(_seed_live_incident(tmp_path))
    incident = asyncio.run(s.get_incident(iid))
    evidence = asyncio.run(s.list_evidence(iid))
    trace = asyncio.run(s.list_diagnosis_trace(sid))
    cp = asyncio.run(s.get_case_profile(iid))

    from tests.export_incident import build_fixture
    fx = build_fixture(incident, evidence, trace, cp, sid)

    # incident.json
    inc = fx["incident"]
    assert inc["incident_id"] == iid
    assert inc["session_id"] == sid
    assert inc["synthetic"] is False
    assert inc["namespace"] == "demo-apps"
    assert "time_range" in inc

    # evidence: 2 rows, tool/tool_args from trace, payload/summary from evidence
    assert len(fx["evidence"]) == 2
    r0 = fx["evidence"][0]
    assert r0["tool"] == "query_logs"
    assert r0["tool_args"] == {"query": '{namespace="demo-apps"}'}
    assert r0["summary"] == "Loki matched 50 lines"
    assert r0["payload"] == {"lines": ["OOMKilled"]}
    assert r0["ref_id"] == "ev_loki_query_logs_0"
    assert r0["status"] == "succeeded"
    assert r0["namespace"] == "demo-apps"
    r1 = fx["evidence"][1]
    assert r1["tool"] == "run_k8s_read"

    # truth: root_cause from case_profile, recorded_prediction from diagnosis_json
    t = fx["truth"]
    assert t["synthetic"] is False
    assert t["root_cause_category"] == "resource_pressure_memory"
    assert t["final_root_cause"] == "demo-probe OOMKilled under memory pressure"
    assert t["key_evidence_refs"] == ["ev_loki_query_logs_0", "ev_k8s_run_k8s_read_1"]
    assert t["effective_actions"] == ["raise memory limit", "rollout restart"]
    rp = t["recorded_prediction"]
    assert rp["category"] == "resource_pressure_memory"
    assert rp["confidence"] == 0.82
    assert rp["level"] == "high"


async def test_build_fixture_trace_only_missing_evidence(tmp_path: Path, **_: object):
    """trace has more steps than evidence → missing rows flagged, not crash."""
    s = _make_store(tmp_path)
    iid = await s.create_incident(alert_name="X", namespace="ns", cluster="c", summary="s")
    sid = "sess-traceonly"
    await s.add_diagnosis_trace(session_id=sid, step_index=0, tool_name="query_metrics",
                                tool_args={"q": "up"}, observation_ref=None)
    # no evidence row added
    incident = await s.get_incident(iid)
    from tests.export_incident import build_fixture
    fx = build_fixture(incident, [], await s.list_diagnosis_trace(sid), None, sid)
    assert len(fx["evidence"]) == 1
    assert fx["evidence"][0]["_trace_only_missing_evidence"] is True
    assert fx["evidence"][0]["payload"] == {}
    # truth has empty root_cause (no case_profile) and no recorded_prediction
    assert fx["truth"]["root_cause_category"] == ""
    assert "recorded_prediction" not in fx["truth"]


async def test_build_fixture_skew_from_failed_middle_step(tmp_path: Path, **_: object):
    """A failed middle tool call writes a trace row but no evidence row.

    Positional pairing would then attach step-2's evidence payload to step-1's
    tool and flag step-2 as missing. Keyed linking (observation_ref ↔ source_ref)
    must attach each evidence payload to its own tool and flag only the failed step.
    """
    s = _make_store(tmp_path)
    iid = await s.create_incident(alert_name="X", namespace="ns", cluster="c", summary="s")
    sid = "sess-skew"
    # step 0: query_metrics, has evidence
    await s.add_diagnosis_trace(session_id=sid, step_index=0, tool_name="query_metrics",
                                tool_args={"q": "up"}, observation_ref="ev_prom_0")
    # step 1: run_k8s_read FAILED → trace row exists, no evidence row
    await s.add_diagnosis_trace(session_id=sid, step_index=1, tool_name="run_k8s_read",
                                tool_args={"resource": "pods"}, observation_ref=None)
    # step 2: query_logs, has evidence
    await s.add_diagnosis_trace(session_id=sid, step_index=2, tool_name="query_logs",
                                tool_args={"q": "{ns=\"x\"}"}, observation_ref="ev_loki_2")
    await s.add_evidence(iid, "prometheus", "ev_prom_0", "metric up",
                         payload={"value": 1})
    # NO evidence for the failed k8s step
    await s.add_evidence(iid, "loki", "ev_loki_2", "log line",
                         payload={"lines": ["boom"]})
    incident = await s.get_incident(iid)
    from tests.export_incident import build_fixture
    fx = build_fixture(incident, await s.list_evidence(iid),
                       await s.list_diagnosis_trace(sid), None, sid)
    rows = fx["evidence"]
    assert len(rows) == 3
    # step 0: metrics, payload intact
    assert rows[0]["tool"] == "query_metrics"
    assert rows[0]["payload"] == {"value": 1}
    assert rows[0]["ref_id"] == "ev_prom_0"
    assert "_trace_only_missing_evidence" not in rows[0]
    # step 1: failed k8s step, no evidence, flagged
    assert rows[1]["tool"] == "run_k8s_read"
    assert rows[1]["_trace_only_missing_evidence"] is True
    assert rows[1]["payload"] == {}
    # step 2: logs, payload intact (NOT mis-paired onto the k8s step)
    assert rows[2]["tool"] == "query_logs"
    assert rows[2]["payload"] == {"lines": ["boom"]}
    assert rows[2]["ref_id"] == "ev_loki_2"


async def test_recorded_prediction_handles_float_confidence(tmp_path: Path, **_: object):
    """LLM-tooluse schema stores candidate.confidence as a float and the guarded
    {score, level} on the top-level diagnosis.confidence. recorded_prediction must
    pull the float score + top-level level, not return None for a missing dict.
    """
    s = _make_store(tmp_path)
    iid = await s.create_incident(alert_name="X", namespace="ns", cluster="c", summary="s")
    # LLM-tooluse shape: candidate.confidence = float, top-level confidence = {score,level}
    await s.record_incident_diagnosis(
        iid,
        {
            "root_cause_candidates": [{
                "cause": "memory leak", "category": "resource_pressure_memory",
                "confidence": 0.77, "evidence_refs": [],
            }],
            "confidence": {"score": 0.77, "level": "high"},
        },
    )
    incident = await s.get_incident(iid)
    from tests.export_incident import build_fixture
    fx = build_fixture(incident, [], [], None, "sid")
    rp = fx["truth"]["recorded_prediction"]
    assert rp["confidence"] == 0.77
    assert rp["level"] == "high"
    assert rp["category"] == "resource_pressure_memory"


async def test_write_fixture_round_trip_loads_in_harness(tmp_path: Path, **_: object):
    """The exported fixture must load via the replay harness loader."""
    iid, sid, s = await _seed_live_incident(tmp_path)
    incident = await s.get_incident(iid)
    evidence = await s.list_evidence(iid)
    trace = await s.list_diagnosis_trace(sid)
    cp = await s.get_case_profile(iid)

    from tests.export_incident import build_fixture, write_fixture
    fx = build_fixture(incident, evidence, trace, cp, sid)
    out = write_fixture(tmp_path / "fixtures", fx, force=False)

    # load via the harness loader
    sys.path.insert(0, str(Path(__file__).parent))
    from replay_incident import load_fixture
    loaded = load_fixture(out)
    assert loaded["incident"]["incident_id"] == iid
    assert loaded["incident"]["synthetic"] is False
    assert len(loaded["evidence"]) == 2
    assert loaded["truth"]["root_cause_category"] == "resource_pressure_memory"
    assert loaded["truth"]["synthetic"] is False


async def test_write_fixture_refuses_existing_without_force(tmp_path: Path, **_: object):
    iid, sid, s = await _seed_live_incident(tmp_path)
    incident = await s.get_incident(iid)
    from tests.export_incident import build_fixture, write_fixture
    fx = build_fixture(incident, [], [], None, sid)
    write_fixture(tmp_path / "fx", fx, force=False)
    with pytest.raises(SystemExit):
        write_fixture(tmp_path / "fx", fx, force=False)
    # force overwrites
    write_fixture(tmp_path / "fx", fx, force=True)


async def test_load_live_incident_round_trip(tmp_path: Path, **_: object):
    """load_live_incident reads a real store (async store.close is sync) end-to-end."""
    iid, sid, s = await _seed_live_incident(tmp_path)
    s.close()
    from tests.export_incident import load_live_incident
    live = await load_live_incident(iid, sid, tmp_path / "incidents.db")
    assert live["incident"]["id"] == iid
    assert len(live["trace"]) == 2
    assert len(live["evidence"]) == 2
    assert live["case_profile"]["root_cause_category"] == "resource_pressure_memory"


async def test_load_live_incident_missing_incident_exits(tmp_path: Path, **_: object):
    """A missing incident id surfaces as a CLI SystemExit, not a ValueError stack."""
    s = _make_store(tmp_path)
    s.close()
    from tests.export_incident import load_live_incident
    with pytest.raises(SystemExit, match="incident not found"):
        await load_live_incident("no-such-id", "sid", tmp_path / "incidents.db")
