"""Replay evaluation harness (ADR-0003 child 3).

Load frozen incident fixtures (incident meta + evidence rows + ground-truth root
cause), replay them through ``run_diagnosis_session`` with a ``ScriptedProvider``
that re-issues the recorded tool-use trajectory, then score the diagnosis against
the ground-truth ``root_cause_category`` with a hand-maintained tolerance matrix
(ADR-0005 §决策 4: 类目带容差,不靠字符串相等).

This is the code-side deliverable. The ≥10 real-fixture campaign is a separate
operational cost (Issue A 真后端采证 + Issue B 真根因回填) and lands in parallel;
the harness ships self-consistent with 1-2 *synthetic* sample fixtures only.

Run:
    python3 tests/replay_incident.py                 # full sweep + hit-rate report
    python3 tests/replay_incident.py --validate-taxonomy   # self-check the matrix
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# When invoked as a CLI (``python3 tests/replay_incident.py``) the repo root is not
# on sys.path; tests get it via conftest.py, but the script entry doesn't. Add it.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes.diagnosis_provider import ScriptedProvider
from toolsets.incident_diagnosis import run_diagnosis_session
from aiops.contracts import EvidenceRef, ToolEnvelope

# --- taxonomy + tolerance (hand-maintained; extend as real fixtures land) -----

# Initial small set. Each ground-truth category that lands via Issue B must be
# added here, and ideally grouped so tolerance can resolve siblings/parents.
ROOT_CAUSE_CATEGORIES: set[str] = {
    "connection_pool_exhaustion",
    "config_error",
    "resource_pressure_memory",
    "resource_pressure_cpu",
    "bad_release_deploy",
    "upstream_dependency_down",
    "certificate_expiry",
    "pvc_disk_full",
    "node_not_ready",
    "undifferentiated",
}

# Upper bucket per category (a category maps to itself if it has no sibling).
CATEGORY_GROUPS: dict[str, str] = {
    "resource_pressure_memory": "resource_pressure",
    "resource_pressure_cpu": "resource_pressure",
    "node_not_ready": "node",
    "pvc_disk_full": "storage",
    "certificate_expiry": "certificate",
    "connection_pool_exhaustion": "connection_pool",
    "upstream_dependency_down": "upstream_dependency",
    "bad_release_deploy": "release",
    "config_error": "config",
    "undifferentiated": "undifferentiated",
}

# Tolerance coefficients.
EXACT = 1.0
SIBLING = 0.5      # same tolerance bucket, different leaf category within it
UNRELATED = 0.0
CONFIDENCE_THRESHOLD = 0.6
CONFIDENCE_BONUS = 0.1  # additive, capped at 1.0
HIT_LINE = 0.5     # score >= HIT_LINE counts as a hit

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "incidents"


def group_of(category: str) -> str:
    if category in CATEGORY_GROUPS:
        return CATEGORY_GROUPS[category]
    # Unknown categories (model emitted something off-list) collapse to undifferentiated.
    return CATEGORY_GROUPS.get("undifferentiated", "undifferentiated")


def score_candidate(
    predicted_category: str | None,
    truth_category: str,
    confidence: float = 0.0,
) -> dict[str, Any]:
    """Score one diagnosis candidate against the ground-truth category.

    Returns ``{score, hit, reason}``. Equal → 1.0; same-group sibling → 0.5;
    parent/child kindred → 0.7; otherwise 0.0. Confidence ≥ threshold adds a
    small bonus (capped at 1.0). An empty/undifferentiated guess against a
    specific truth scores 0.0 — fallback never earns partial credit.
    """
    predicted = (predicted_category or "").strip().lower() or "undifferentiated"
    truth = (truth_category or "").strip().lower() or "undifferentiated"

    if predicted == "undifferentiated" and truth != "undifferentiated":
        return {"score": 0.0, "hit": False, "reason": "fallback_no_credit"}

    if predicted == truth:
        score = EXACT
        reason = "exact"
    elif group_of(predicted) == group_of(truth):
        # same tolerance bucket, different leaf category (e.g. memory vs cpu pressure)
        score = SIBLING
        reason = "sibling"
    else:
        score = UNRELATED
        reason = "unrelated"

    if score > 0.0 and confidence >= CONFIDENCE_THRESHOLD:
        score = min(EXACT, score + CONFIDENCE_BONUS)
        reason = f"{reason}+conf"
    return {"score": score, "hit": score >= HIT_LINE, "reason": reason}


def validate_taxonomy() -> list[str]:
    """Return a list of problems; empty means OK. Used by ``--validate-taxonomy``."""
    problems: list[str] = []
    for cat in ROOT_CAUSE_CATEGORIES:
        if cat not in CATEGORY_GROUPS:
            problems.append(f"category '{cat}' has no group mapping (dangling)")
    # reverse: every group mapping target should be a known category or a shared bucket
    for cat, group in CATEGORY_GROUPS.items():
        if cat not in ROOT_CAUSE_CATEGORIES:
            problems.append(f"category '{cat}' in CATEGORY_GROUPS not in ROOT_CAUSE_CATEGORIES")
    return problems


# --- fixture loading ---------------------------------------------------------

def list_fixtures(root: Path = FIXTURES_ROOT) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "incident.json").exists())


def load_fixture(dir_: Path) -> dict[str, Any]:
    incident = json.loads((dir_ / "incident.json").read_text(encoding="utf-8"))
    evidence_dir = dir_ / "evidence"
    evidence_rows: list[dict[str, Any]] = []
    if evidence_dir.exists():
        for ev in sorted(evidence_dir.glob("*.json")):
            evidence_rows.append(json.loads(ev.read_text(encoding="utf-8")))
    truth = json.loads((dir_ / "truth.json").read_text(encoding="utf-8"))
    return {"dir": dir_, "incident": incident, "evidence": evidence_rows, "truth": truth}


# --- replay plumbing: build ScriptedProvider + adapters from frozen evidence -

# tool_name -> (source, default ref prefix) for building ToolEnvelope evidence_refs
_TOOL_SOURCE: dict[str, tuple[str, str]] = {
    "query_metrics": ("prometheus", "ev_prom"),
    "query_logs": ("loki", "ev_loki"),
    "run_k8s_read": ("k8s_gateway", "ev_k8s"),
    "get_service_topology": ("topology", "ev_topology"),
}


def _envelope(tool_name: str, row: dict[str, Any], idx: int) -> ToolEnvelope:
    source, ref_prefix = _TOOL_SOURCE[tool_name]
    ref_id = row.get("ref_id") or f"{ref_prefix}_{tool_name}_{idx}"
    return ToolEnvelope(
        request_id=f"replay-{tool_name}-{idx}",
        correlation_id="replay",
        tool_name=tool_name,
        status=str(row.get("status") or "succeeded"),
        summary=str(row.get("summary") or ""),
        data=row.get("payload") or {},
        evidence_refs=(
            EvidenceRef(
                ref_id=ref_id,
                source=source,
                cluster_id="prod-a",
                namespace=row.get("namespace") or "default",
                service=row.get("service") or "",
            ),
        ),
        audit={"status": row.get("status") or "succeeded", "tool_name": tool_name, "error_code": None},
        errors=(),
    )


class FrozenAdapter:
    """Yields the next frozen evidence row as a tool observation."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)
        self._idx = 0
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, args: dict[str, Any]) -> ToolEnvelope:
        self.calls.append(args)
        if self._idx >= len(self._rows):
            raise AssertionError(f"adapter exhausted (got {args})")
        row = self._rows[self._idx]
        self._idx += 1
        return _envelope(row["tool"], row, self._idx - 1)


def _resp_tool_call(tool_name: str, call_id: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments or {}, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }


def _resp_final(category: str, cause: str, confidence: float, level: str = "high") -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "root_cause_candidates": [
                                {
                                    "cause": cause,
                                    "category": category,
                                    "confidence": confidence,
                                    "evidence_refs": [],
                                }
                            ],
                            "recommended_actions": [],
                            "confidence": {"score": confidence, "level": level},
                        },
                        ensure_ascii=False,
                    ),
                },
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    }


def build_replay(fixture: dict[str, Any]) -> tuple[ScriptedProvider, dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Assemble (provider, incident, adapters) for a full tool-use replay of this fixture.

    The fixture's ``script`` block (``truth.json`` or incident meta) drives the
    model trajectory: one scripted tool_call per recorded evidence row, then a
    final stop message re-emitting the recorded diagnosis. This is a *replay*
    of what the brain saw — not a fresh live diagnosis — so the score measures
    "does the structured category we recorded match ground truth with tolerance".
    """
    rows = list(fixture["evidence"])
    scripts: list[dict[str, Any]] = []
    adapters_by_tool: dict[str, list[dict[str, Any]]] = {
        "query_metrics": [],
        "query_logs": [],
        "run_k8s_read": [],
        "get_service_topology": [],
    }
    for i, row in enumerate(rows):
        tool = row["tool"]
        if tool not in adapters_by_tool:
            raise ValueError(f"fixture evidence row {i} unknown tool: {tool}")
        adapters_by_tool[tool].append(row)
        scripts.append(_resp_tool_call(tool, call_id=f"call_{i + 1}", arguments=row.get("tool_args")))

    # The final stop message re-uses the recorded predicted category/cause from truth.json.
    truth = fixture["truth"]
    pred = truth.get("recorded_prediction") or {}
    scripts.append(
        _resp_final(
            category=pred.get("category") or truth.get("root_cause_category") or "undifferentiated",
            cause=pred.get("cause") or truth.get("final_root_cause") or "",
            confidence=float(pred.get("confidence") or 0.8),
            level=pred.get("level") or "high",
        )
    )

    provider = ScriptedProvider(scripts)
    incident = dict(fixture["incident"])
    adapters = {tool: None for tool in adapters_by_tool}
    for tool, tool_rows in adapters_by_tool.items():
        if tool_rows:
            adapters[tool] = FrozenAdapter(tool_rows)
    return provider, incident, adapters


class _NullStore:
    """No-op incident store: replay harness only reads diagnosis output, not trace/evidence persist."""

    async def add_evidence(self, *a: Any, **k: Any) -> None:  # noqa: D401
        return None

    async def record_incident_diagnosis(self, *a: Any, **k: Any) -> None:  # noqa: D401
        return None

    async def add_diagnosis_trace(self, *a: Any, **k: Any) -> None:  # noqa: D401
        return None


async def replay_one(fixture: dict[str, Any]) -> dict[str, Any]:
    provider, incident, adapters = build_replay(fixture)
    session = await run_diagnosis_session(
        incident,
        provider=provider,
        metrics_adapter=adapters["query_metrics"],
        logs_adapter=adapters["query_logs"],
        topology_adapter=adapters["get_service_topology"],
        k8s_read_adapter=adapters["run_k8s_read"],
        incident_store=_NullStore(),
    )
    candidates = (session.get("diagnosis") or {}).get("root_cause_candidates") or []
    top = candidates[0] if candidates else {}
    truth = fixture["truth"]
    verdict = score_candidate(
        top.get("category"),
        truth.get("root_cause_category") or "undifferentiated",
        float(top.get("confidence") or 0.0),
    )
    return {
        "fixture": fixture["dir"].name,
        "synthetic": bool(fixture.get("incident", {}).get("synthetic") or truth.get("synthetic")),
        "status": session.get("status"),
        "predicted_category": top.get("category") or "",
        "truth_category": truth.get("root_cause_category") or "",
        "score": verdict["score"],
        "hit": verdict["hit"],
        "reason": verdict["reason"],
    }


async def run_sweep(root: Path = FIXTURES_ROOT) -> dict[str, Any]:
    results = [await replay_one(load_fixture(d)) for d in list_fixtures(root)]
    real = [r for r in results if not r["synthetic"]]
    synth = [r for r in results if r["synthetic"]]
    hit_rate = (sum(1 for r in real if r["hit"]) / len(real)) if real else 0.0
    return {"results": results, "real_count": len(real), "synthetic_count": len(synth), "real_hit_rate": hit_rate}


# --- CLI ---------------------------------------------------------------------

def _format_report(report: dict[str, Any]) -> str:
    lines = ["Replay eval report", "=" * 40, ""]
    for r in report["results"]:
        tag = " [synthetic]" if r["synthetic"] else ""
        lines.append(
            f"- {r['fixture']}{tag}: pred={r['predicted_category']} truth={r['truth_category']} "
            f"score={r['score']:.2f} hit={r['hit']} ({r['reason']}) status={r['status']}"
        )
    lines.append("")
    lines.append(f"Real fixtures: {report['real_count']}")
    lines.append(f"Synthetic fixtures: {report['synthetic_count']}")
    if report["real_count"]:
        lines.append(f"Real hit-rate: {report['real_hit_rate'] * 100:.1f}%")
    else:
        lines.append("Real hit-rate: (no real fixtures yet — operational campaign pending via Issue A/B)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay incident diagnosis harness (ADR-0003 child 3).")
    parser.add_argument("--root", type=Path, default=FIXTURES_ROOT, help="fixtures root directory")
    parser.add_argument("--validate-taxonomy", action="store_true", help="self-check the tolerance matrix")
    parser.add_argument("--json", action="store_true", help="emit report as JSON")
    args = parser.parse_args(argv)

    if args.validate_taxonomy:
        problems = validate_taxonomy()
        if problems:
            print("Taxonomy problems:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("Taxonomy OK: every listed category is grouped, no dangling entries.")
        return 0

    report = asyncio.run(run_sweep(args.root))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(_format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())