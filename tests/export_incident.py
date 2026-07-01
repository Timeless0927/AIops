"""ADR-0003 replay campaign #4 — fixture exporter.

Freezes a live incident (real backend采集 + real LLM tool-use run + human-backfilled
真根因) into a replay-harness-consumable fixture under tests/fixtures/incidents/<id>/.

Read-only over ``incident_store`` (no production-code mutation). Produces three files
per the harness contract (tests/replay_incident.py :: load_fixture):

- incident.json  — incident meta + synthetic:false
- evidence/NN_<source>.json — one observation per tool-use step, fields merged from
  diagnosis_trace (tool/tool_args, by session_id) and incident_evidence (summary/payload,
  by incident_id), linked by step order + observation_ref
- truth.json — root_cause_category/final_root_cause from case_profile (human truth) +
  recorded_prediction from incidents.diagnosis_json (what the brain produced)

This is an operational tool, not production code. Lives beside tests/replay_incident.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# When invoked as a CLI (``python3 tests/export_incident.py``), the repo root is
# not on sys.path; tests get it via conftest.py. Match tests/replay_incident.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from toolsets import incident_store


# tool_name -> (source, default ref prefix) — mirrors tests/replay_incident.py::_TOOL_SOURCE
_TOOL_SOURCE: dict[str, tuple[str, str]] = {
    "query_metrics": ("prometheus", "ev_prom"),
    "query_logs": ("loki", "ev_loki"),
    "run_k8s_read": ("k8s_gateway", "ev_k8s"),
    "get_service_topology": ("topology", "ev_topology"),
}


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_json_field(raw: Any) -> Any:
    """case_profile key_evidence_refs/effective_actions are stored as JSON strings."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _build_incident_json(incident: dict[str, Any], session_id: str) -> dict[str, Any]:
    created = incident.get("created_at")
    resolved = incident.get("resolved_at")
    start = _iso(created)
    end = _iso(resolved) or start
    time_range = f"{start}/{end}" if start else None
    return {
        "incident_id": incident.get("id") or incident.get("incident_id"),
        "session_id": session_id,
        "alert_name": incident.get("alert_name", ""),
        "namespace": incident.get("namespace", "default"),
        "cluster": incident.get("cluster", "default"),
        "service": incident.get("service", ""),
        "summary": incident.get("summary", ""),
        **({"time_range": time_range} if time_range else {}),
        "synthetic": False,
    }


def _build_evidence_rows(
    trace: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    incident: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge trace (tool/tool_args per step) with evidence (summary/payload per row).

    Both are written in the same loop in _run_llm_tooluse_session, so step_index order
    matches evidence collection order for the common case (every observation succeeded
    or skipped — both land a row). A *failed* observation writes a trace row but no
    evidence row, which would skew pure positional pairing for all later steps. So:
    link by ``observation_ref`` (trace) ↔ ``source_ref`` (evidence) first — both hold
    the same ``observation.evidence_ref`` value the producer writes — and only fall
    back to positional order for trace steps with no observation_ref. Two passes so a
    no-ref step cannot steal a row that a later keyed step claims. (design.md:
    顺序优先, observation_ref 兜底 — observation_ref is the primary key because it
    survives row skips; positional is the tail fallback.)
    """
    ns = incident.get("namespace", "default")
    svc = incident.get("service", "")

    # index evidence by source_ref for keyed lookup.
    ev_by_ref: dict[str, dict[str, Any]] = {}
    for ev in evidence:
        ref = ev.get("source_ref")
        if ref:
            ev_by_ref.setdefault(ref, ev)
    consumed: set[str] = set()
    matched: dict[int, dict[str, Any]] = {}

    # pass 1: keyed match (observation_ref ↔ source_ref). Survives failed-step skips.
    for idx, tr in enumerate(trace):
        obs_ref = tr.get("observation_ref")
        if obs_ref and obs_ref in ev_by_ref and obs_ref not in consumed:
            matched[idx] = ev_by_ref[obs_ref]
            consumed.add(obs_ref)

    # pass 2: positional fallback for the remaining (no-ref) trace steps, over the
    # evidence rows not already grabbed by a keyed step.
    unmatched_ev = [ev for ev in evidence if not (ev.get("source_ref") in consumed)]
    pos = 0
    for idx, tr in enumerate(trace):
        if idx in matched:
            continue
        if tr.get("observation_ref"):
            continue  # had a ref but it didn't match → genuinely missing, don't steal
        if pos < len(unmatched_ev):
            matched[idx] = unmatched_ev[pos]
            pos += 1

    rows: list[dict[str, Any]] = []
    for idx, tr in enumerate(trace):
        tool = tr.get("tool_name") or ""
        source_tup = _TOOL_SOURCE.get(tool)
        ev = matched.get(idx)
        ref_id = tr.get("observation_ref") or (
            f"{source_tup[1]}_{tool}_{idx}" if source_tup else f"ev_{tool}_{idx}"
        )
        rows.append({
            "tool": tool,
            "status": "succeeded",
            "summary": (ev or {}).get("summary", ""),
            "payload": (ev or {}).get("payload") or {},
            "namespace": ns,
            "service": svc,
            "ref_id": ref_id,
            "tool_args": tr.get("tool_args") or {},
            **({"_trace_only_missing_evidence": True} if ev is None else {}),
        })
    return rows


def _build_truth_json(
    case_profile: dict[str, Any] | None,
    incident: dict[str, Any],
) -> dict[str, Any]:
    truth: dict[str, Any] = {"synthetic": False}
    if case_profile:
        truth["root_cause_category"] = case_profile.get("root_cause_category") or ""
        truth["final_root_cause"] = case_profile.get("final_root_cause") or ""
        truth["key_evidence_refs"] = _parse_json_field(case_profile.get("key_evidence_refs")) or []
        truth["effective_actions"] = _parse_json_field(case_profile.get("effective_actions")) or []
    else:
        truth["root_cause_category"] = ""
        truth["final_root_cause"] = ""
        truth["key_evidence_refs"] = []
        truth["effective_actions"] = []

    # recorded_prediction = what the brain produced (incidents.diagnosis_json), NOT the truth.
    diag_raw = incident.get("diagnosis_json")
    if isinstance(diag_raw, str) and diag_raw.strip():
        try:
            diag = json.loads(diag_raw)
            cands = diag.get("root_cause_candidates") or []
            if cands:
                c0 = cands[0]
                # candidate.confidence is a float in the LLM-tooluse schema, or a
                # {score,level} dict in the keyword fallback — handle both. The
                # guarded confidence/level live on the top-level diagnosis object.
                top_conf = diag.get("confidence") or {}
                top_conf = top_conf if isinstance(top_conf, dict) else {}
                c_conf = c0.get("confidence")
                if isinstance(c_conf, dict):
                    score = c_conf.get("score")
                    level = c_conf.get("level") or top_conf.get("level")
                elif isinstance(c_conf, (int, float)):
                    score = float(c_conf)
                    level = top_conf.get("level")
                else:
                    score = top_conf.get("score")
                    level = top_conf.get("level")
                truth["recorded_prediction"] = {
                    "category": c0.get("category", ""),
                    "cause": c0.get("cause", ""),
                    "confidence": score,
                    "level": level,
                }
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    return truth


def build_fixture(
    incident: dict[str, Any],
    evidence: list[dict[str, Any]],
    trace: list[dict[str, Any]],
    case_profile: dict[str, Any] | None,
    session_id: str,
) -> dict[str, Any]:
    """Pure: assemble the three fixture blobs from store read outputs."""
    return {
        "incident": _build_incident_json(incident, session_id),
        "evidence": _build_evidence_rows(trace, evidence, incident),
        "truth": _build_truth_json(case_profile, incident),
    }


def write_fixture(out_root: Path, fixture: dict[str, Any], *, force: bool) -> Path:
    incident_id = fixture["incident"]["incident_id"]
    dir_ = out_root / incident_id
    if dir_.exists() and not force:
        raise SystemExit(f"fixture dir exists: {dir_} (pass --force to overwrite)")
    if dir_.exists():
        for old in dir_.glob("**/*"):
            if old.is_file():
                old.unlink()
    ev_dir = dir_ / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    (dir_ / "incident.json").write_text(
        json.dumps(fixture["incident"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for idx, row in enumerate(fixture["evidence"], start=1):
        source = _TOOL_SOURCE.get(row["tool"], ("unknown", "ev"))[0]
        (ev_dir / f"{idx:02d}_{source}.json").write_text(
            json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    (dir_ / "truth.json").write_text(
        json.dumps(fixture["truth"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return dir_


async def load_live_incident(
    incident_id: str, session_id: str, db_path: Path
) -> dict[str, Any]:
    """Read store: incident + evidence + trace + case_profile."""
    store = incident_store.IncidentStore(db_path=db_path)
    try:
        # This CLI is read-only and often runs against a copied SQLite snapshot.
        # Use the store's synchronous read helpers directly so the process does not
        # wait on the default executor at shutdown in minimal container shells.
        incident = store._fetchone("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if incident is None:
            raise SystemExit(f"incident not found: {incident_id}")

        evidence = store._fetchall(
            """
            SELECT id, incident_id, source_type, source_ref, summary, payload_json,
                   window_start_ts, window_end_ts, collected_at, collector_version, confidence
            FROM incident_evidence
            WHERE incident_id = ?
            ORDER BY collected_at ASC, id ASC
            """,
            (incident_id,),
        )
        for row in evidence:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")

        trace = store._fetchall(
            """
            SELECT session_id, step_index, tool_name, tool_args_json, observation_ref,
                   duration_ms, model, input_tokens, output_tokens, trace_collected_at
            FROM diagnosis_trace WHERE session_id = ?
            ORDER BY step_index ASC, id ASC
            """,
            (session_id,),
        )
        for row in trace:
            row["tool_args"] = json.loads(row.pop("tool_args_json") or "{}")

        case_profile = store._fetchone(
            """
            SELECT incident_id, incident_signature, symptom_fingerprint, final_scope,
                   final_root_cause, root_cause_category, key_evidence_refs_json,
                   effective_actions_json, invalid_actions_json,
                   metric_delta_summary_json, change_clue_summary, resolution_seconds,
                   similar_incident_ids_json, created_at, updated_at
            FROM incident_case_profiles
            WHERE incident_id = ?
            """,
            (incident_id,),
        )
        if case_profile is not None:
            case_profile["effective_actions"] = json.loads(
                case_profile.pop("effective_actions_json") or "[]"
            )
            case_profile["invalid_actions"] = json.loads(
                case_profile.pop("invalid_actions_json") or "[]"
            )
            case_profile["metric_delta_summary"] = json.loads(
                case_profile.pop("metric_delta_summary_json") or "{}"
            )
            case_profile["similar_incident_ids"] = json.loads(
                case_profile.pop("similar_incident_ids_json") or "[]"
            )
            case_profile["key_evidence_refs"] = json.loads(
                case_profile.pop("key_evidence_refs_json") or "[]"
            )
    finally:
        # store.close() is synchronous (sqlite handle teardown), not a coroutine.
        if hasattr(store, "close"):
            store.close()
    return {
        "incident": incident,
        "evidence": evidence,
        "trace": trace,
        "case_profile": case_profile,
    }


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a live incident into a replay fixture.")
    parser.add_argument("incident_id", help="incident id (incidents.id)")
    parser.add_argument("--session-id", required=True, help="diagnosis session id (drives trace lookup)")
    parser.add_argument("--db", default="/data/aiops/incidents.db", help="incident_store sqlite path")
    parser.add_argument("--out", default="tests/fixtures/incidents", help="fixtures root")
    parser.add_argument("--force", action="store_true", help="overwrite existing fixture dir")
    args = parser.parse_args(argv)

    live = await load_live_incident(args.incident_id, args.session_id, Path(args.db))
    fixture = build_fixture(
        live["incident"], live["evidence"], live["trace"], live["case_profile"], args.session_id
    )
    out_dir = write_fixture(Path(args.out), fixture, force=args.force)
    print(f"exported fixture: {out_dir}")
    print(f"  evidence rows: {len(fixture['evidence'])}")
    print(f"  root_cause_category: {fixture['truth'].get('root_cause_category') or '<missing — backfill case-profile first>'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
