"""Tests for tests/export_incident.py (ADR-0003 replay campaign #4).

Strategy: initialize a real IncidentStore schema on a tmp_path DB, seed the
tables directly to match copied live SQLite snapshots, then assert the exporter
produces a harness-consumable fixture with correct field mapping.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# tests/ is on sys.path via conftest; export_incident lives beside replay_incident
from toolsets import incident_store as store_mod


# --- helpers -----------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "incidents.db"
    store_mod.IncidentStore(db_path=db_path).close()
    return db_path


def _seed_live_incident(tmp_path: Path) -> tuple[str, str, Path]:
    """Populate a tmp DB with one incident: 2 trace steps + 2 evidence rows +
    case_profile + diagnosis_json. Returns (incident_id, session_id, db_path)."""
    db_path = _make_db(tmp_path)
    iid = "inc-export-test"
    sid = "diagnosis-test-session-001"
    now = time.time()
    diagnosis = {
        "root_cause_candidates": [{
            "cause": "memory leak drove RSS above limit",
            "category": "resource_pressure_memory",
            "confidence": {"score": 0.82, "level": "high"},
        }],
        "confidence": {"score": 0.82, "level": "high"},
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO incidents (
                id, alert_name, namespace, cluster, service, status, created_at,
                summary, diagnosis_json, diagnosed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iid, "PodCrashLooping", "demo-apps", "dev-external", "demo-probe",
                "new", now, "demo-probe restarts climbing",
                json.dumps(diagnosis, ensure_ascii=False, sort_keys=True), now,
            ),
        )
        for step, tool, args, ref, in_tok, out_tok in (
            (0, "query_logs", {"query": '{namespace="demo-apps"}'}, "ev_loki_query_logs_0", 100, 20),
            (1, "run_k8s_read", {"resource": "pods"}, "ev_k8s_run_k8s_read_1", 200, 30),
        ):
            conn.execute(
                """
                INSERT INTO diagnosis_trace (
                    session_id, step_index, tool_name, tool_args_json, observation_ref,
                    input_tokens, output_tokens, trace_collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, step, tool, json.dumps(args, ensure_ascii=False, sort_keys=True), ref, in_tok, out_tok, now + step),
            )
        for source, ref, summary, payload in (
            ("logs", "ev_loki_query_logs_0", "Loki matched 50 lines", {"lines": ["OOMKilled"]}),
            ("k8s_gateway", "ev_k8s_run_k8s_read_1", "pod restarted 3x", {"restarts": 3}),
        ):
            conn.execute(
                """
                INSERT INTO incident_evidence (
                    incident_id, source_type, source_ref, summary, payload_json,
                    collected_at, collector_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iid, source, ref, summary,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True), now,
                    "incident_diagnosis/llm-tooluse-v1",
                ),
            )
        conn.execute(
            """
            INSERT INTO incident_case_profiles (
                incident_id, incident_signature, final_root_cause, root_cause_category,
                key_evidence_refs_json, effective_actions_json, invalid_actions_json,
                metric_delta_summary_json, similar_incident_ids_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iid, "PodCrashLooping|demo-apps",
                "demo-probe OOMKilled under memory pressure",
                "resource_pressure_memory",
                json.dumps(["ev_loki_query_logs_0", "ev_k8s_run_k8s_read_1"], ensure_ascii=False),
                json.dumps(["raise memory limit", "rollout restart"], ensure_ascii=False),
                "[]", "{}", "[]", now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return iid, sid, db_path


def _load_seed(tmp_path: Path) -> tuple[str, str, dict]:
    iid, sid, db_path = _seed_live_incident(tmp_path)
    from tests.export_incident import load_live_incident
    live = asyncio.run(load_live_incident(iid, sid, db_path))
    return iid, sid, live


# --- pure build_fixture ------------------------------------------------------

def test_build_fixture_maps_all_fields(tmp_path):
    iid, sid, live = _load_seed(tmp_path)

    from tests.export_incident import build_fixture
    fx = build_fixture(live["incident"], live["evidence"], live["trace"], live["case_profile"], sid)

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


def test_build_fixture_trace_only_missing_evidence() -> None:
    """trace has more steps than evidence → missing rows flagged, not crash."""
    sid = "sess-traceonly"
    incident = {"id": "inc-traceonly", "alert_name": "X", "namespace": "ns", "cluster": "c", "summary": "s"}
    trace = [{"session_id": sid, "step_index": 0, "tool_name": "query_metrics", "tool_args": {"q": "up"}, "observation_ref": None}]
    from tests.export_incident import build_fixture
    fx = build_fixture(incident, [], trace, None, sid)
    assert len(fx["evidence"]) == 1
    assert fx["evidence"][0]["_trace_only_missing_evidence"] is True
    assert fx["evidence"][0]["payload"] == {}
    # truth has empty root_cause (no case_profile) and no recorded_prediction
    assert fx["truth"]["root_cause_category"] == ""
    assert "recorded_prediction" not in fx["truth"]


def test_build_fixture_skew_from_failed_middle_step() -> None:
    """A failed middle tool call writes a trace row but no evidence row.

    Positional pairing would then attach step-2's evidence payload to step-1's
    tool and flag step-2 as missing. Keyed linking (observation_ref ↔ source_ref)
    must attach each evidence payload to its own tool and flag only the failed step.
    """
    sid = "sess-skew"
    incident = {"id": "inc-skew", "alert_name": "X", "namespace": "ns", "cluster": "c", "summary": "s"}
    trace = [
        {"session_id": sid, "step_index": 0, "tool_name": "query_metrics", "tool_args": {"q": "up"}, "observation_ref": "ev_prom_0"},
        {"session_id": sid, "step_index": 1, "tool_name": "run_k8s_read", "tool_args": {"resource": "pods"}, "observation_ref": None},
        {"session_id": sid, "step_index": 2, "tool_name": "query_logs", "tool_args": {"q": '{ns="x"}'}, "observation_ref": "ev_loki_2"},
    ]
    evidence = [
        {"source_type": "prometheus", "source_ref": "ev_prom_0", "summary": "metric up", "payload": {"value": 1}},
        {"source_type": "loki", "source_ref": "ev_loki_2", "summary": "log line", "payload": {"lines": ["boom"]}},
    ]
    from tests.export_incident import build_fixture
    fx = build_fixture(incident, evidence, trace, None, sid)
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


def test_recorded_prediction_handles_float_confidence() -> None:
    """LLM-tooluse schema stores candidate.confidence as a float and the guarded
    {score, level} on the top-level diagnosis.confidence. recorded_prediction must
    pull the float score + top-level level, not return None for a missing dict.
    """
    incident = {
        "id": "inc-float",
        "alert_name": "X",
        "namespace": "ns",
        "cluster": "c",
        "summary": "s",
        "diagnosis_json": json.dumps(
            {
            "root_cause_candidates": [{
                "cause": "memory leak", "category": "resource_pressure_memory",
                "confidence": 0.77, "evidence_refs": [],
            }],
            "confidence": {"score": 0.77, "level": "high"},
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    }
    from tests.export_incident import build_fixture
    fx = build_fixture(incident, [], [], None, "sid")
    rp = fx["truth"]["recorded_prediction"]
    assert rp["confidence"] == 0.77
    assert rp["level"] == "high"
    assert rp["category"] == "resource_pressure_memory"


def test_write_fixture_round_trip_loads_in_harness(tmp_path: Path) -> None:
    """The exported fixture must load via the replay harness loader."""
    iid, sid, live = _load_seed(tmp_path)

    from tests.export_incident import build_fixture, write_fixture
    fx = build_fixture(live["incident"], live["evidence"], live["trace"], live["case_profile"], sid)
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


def test_write_fixture_refuses_existing_without_force(tmp_path: Path) -> None:
    _, sid, live = _load_seed(tmp_path)
    from tests.export_incident import build_fixture, write_fixture
    fx = build_fixture(live["incident"], [], [], None, sid)
    write_fixture(tmp_path / "fx", fx, force=False)
    with pytest.raises(SystemExit):
        write_fixture(tmp_path / "fx", fx, force=False)
    # force overwrites
    write_fixture(tmp_path / "fx", fx, force=True)


def test_load_live_incident_round_trip(tmp_path: Path) -> None:
    """load_live_incident reads a real store (async store.close is sync) end-to-end."""
    iid, sid, db_path = _seed_live_incident(tmp_path)
    from tests.export_incident import load_live_incident
    live = asyncio.run(load_live_incident(iid, sid, db_path))
    assert live["incident"]["id"] == iid
    assert len(live["trace"]) == 2
    assert len(live["evidence"]) == 2
    assert live["case_profile"]["root_cause_category"] == "resource_pressure_memory"


def test_load_live_incident_missing_incident_exits(tmp_path: Path) -> None:
    """A missing incident id surfaces as a CLI SystemExit, not a ValueError stack."""
    db_path = _make_db(tmp_path)
    from tests.export_incident import load_live_incident
    with pytest.raises(SystemExit, match="incident not found"):
        asyncio.run(load_live_incident("no-such-id", "sid", db_path))
