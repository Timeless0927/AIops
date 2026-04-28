# P1 Proactive Risk Scan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-batch P1 proactive risk scan tool for a single Kubernetes cluster that detects low-noise restart/workload/node risks and enriches them with existing case-profile and repeat-incident history.

**Architecture:** Introduce a new read-only tool module, `toolsets/proactive_risk_scan.py`, that collects current cluster signals from `k8s_read`, derives deterministic risk items from simple rules, enriches them with historical context from `incident_store` and `sre_metrics`, and returns both structured risks and a concise Chinese summary. Keep it manual-call first; do not add scheduling or alert-to-incident integration in this batch. This plan is P1 prework, not a P0 closed-loop deliverable.

**Tech Stack:** Python 3.11, existing Hermes tool registry, pytest, `k8s_read`, `incident_store`, `sre_metrics`, JSON tool outputs.

---

## File Structure

- Create: `toolsets/proactive_risk_scan.py` — proactive scan tool, rule parsing, historical enrichment, summary rendering, and Hermes registry registration.
- Create: `tests/test_proactive_risk_scan.py` — TDD coverage for risk rules, history enrichment, and low-noise empty scan behavior.
- Modify: `toolsets/incident_store.py` — add a narrow read-only helper for recent case profiles by namespace and scope, without changing schema.
- Modify: `tests/test_incident_store.py` — cover the new case-profile query helper.
- Modify: `toolsets/sre_metrics.py` — optional only if needed to keep repeat-baseline reads simple and stable from the scan tool; avoid moving scan logic here.
- Modify: `tests/test_sre_metrics.py` — only if `sre_metrics` gains a small helper.
- Modify: `/home/mao/.hermes/plugins/sre/plugin.yaml` — expose the new `sre_proactive_risk_scan` tool in the manifest.
- Modify: `/home/mao/.hermes/plugins/sre/__init__.py` — import the new tool module during plugin registration.
- Modify: `tests/test_sre_plugin.py` — assert the new tool is present in the manifest and registry.

### Task 1: Add a recent case-profile lookup helper

**Files:**
- Modify: `tests/test_incident_store.py`
- Modify: `toolsets/incident_store.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_incident_store.py`:

```python
@pytest.mark.asyncio
async def test_list_recent_case_profiles_by_namespace_and_scope(tmp_path: Path, **_: object) -> None:
    module, store = _load_module(tmp_path)
    inc_a = await module.create_incident("PodCrashLooping", "default", "prod-a", "older")
    inc_b = await module.create_incident("PodCrashLooping", "default", "prod-a", "newer")
    inc_c = await module.create_incident("NodeNotReady", "kube-system", "prod-a", "node")

    await module.upsert_case_profile(
        inc_a,
        incident_signature="PodCrashLooping|default|workload|resolved",
        final_scope="workload",
        final_root_cause="资源压力可能导致工作负载异常",
        effective_actions=["检查 Pod CPU/内存指标与资源配置"],
        updated_at=100.0,
    )
    await module.upsert_case_profile(
        inc_b,
        incident_signature="PodCrashLooping|default|workload|resolved",
        final_scope="workload",
        final_root_cause="应用日志显示运行时异常",
        effective_actions=["检查相关 Pod 最近错误日志与超时信息"],
        updated_at=200.0,
    )
    await module.upsert_case_profile(
        inc_c,
        incident_signature="NodeNotReady|kube-system|node|resolved",
        final_scope="node",
        final_root_cause="节点状态异常可能扩大影响范围",
        effective_actions=["检查异常 Node 状态与受影响工作负载分布"],
        updated_at=300.0,
    )

    rows = await module.list_recent_case_profiles(namespace="default", final_scope="workload", limit=2)

    assert [item["incident_id"] for item in rows] == [inc_b, inc_a]
    assert rows[0]["final_root_cause"] == "应用日志显示运行时异常"
    assert rows[1]["effective_actions"] == ["检查 Pod CPU/内存指标与资源配置"]

    store.close()
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `rtk pytest tests/test_incident_store.py::test_list_recent_case_profiles_by_namespace_and_scope -q`
Expected: FAIL with `AttributeError` because `list_recent_case_profiles` does not exist.

- [ ] **Step 3: Implement the narrow read-only helper**

In `toolsets/incident_store.py`, add an async helper with this shape:

```python
async def list_recent_case_profiles(
    self,
    *,
    namespace: str,
    final_scope: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    ...
```

Implementation details:

- Join `incident_case_profiles` to `incidents` via `incident_id`.
- Filter on `incidents.namespace = ?`.
- If `final_scope` is provided, also filter on `incident_case_profiles.final_scope = ?`.
- Order by `incident_case_profiles.updated_at DESC, incident_id DESC`.
- Decode `effective_actions_json`, `invalid_actions_json`, `metric_delta_summary_json`, and `similar_incident_ids_json` just like `get_case_profile()` does.
- Add a module-level wrapper:

```python
async def list_recent_case_profiles(
    *,
    namespace: str,
    final_scope: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    return await _STORE.list_recent_case_profiles(namespace=namespace, final_scope=final_scope, limit=limit)
```

- [ ] **Step 4: Run the focused incident-store test**

Run: `rtk pytest tests/test_incident_store.py::test_list_recent_case_profiles_by_namespace_and_scope -q`
Expected: PASS.

- [ ] **Step 5: Run adjacent incident-store regression tests**

Run: `rtk pytest tests/test_incident_store.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_incident_store.py toolsets/incident_store.py
rtk git commit -m "feat: add recent case profile lookup"
```

### Task 2: Add the proactive risk scan tool with empty-scan behavior

**Files:**
- Create: `tests/test_proactive_risk_scan.py`
- Create: `toolsets/proactive_risk_scan.py`

- [ ] **Step 1: Write the failing empty-scan test**

Create `tests/test_proactive_risk_scan.py` with:

```python
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "proactive_risk_scan.py"
    module_name = "test_proactive_risk_scan_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_scan_returns_low_noise_empty_result(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()

    async def _k8s_read(command: str, context: str | None = None):
        del context
        outputs = {
            "kubectl get pods -A": {"ok": True, "stdout": "NAMESPACE NAME READY STATUS RESTARTS AGE\ndefault api-1 1/1 Running 0 10m\n"},
            "kubectl get deploy -A": {"ok": True, "stdout": "NAMESPACE NAME READY UP-TO-DATE AVAILABLE AGE\ndefault api 3/3 3 3 10m\n"},
            "kubectl get nodes": {"ok": True, "stdout": "NAME STATUS ROLES AGE VERSION\nnode-a Ready worker 10d v1.30.0\n"},
        }
        return outputs[command]

    async def _list_recent_case_profiles(**kwargs):
        del kwargs
        return []

    async def _compute_metrics(days: int = 7):
        assert days == 7
        return {"repeat_incident_count": 0, "repeat_incident_rate": 0.0}

    monkeypatch.setattr(module.k8s_read_tool, "k8s_read", _k8s_read)
    monkeypatch.setattr(module.incident_store, "list_recent_case_profiles", _list_recent_case_profiles)
    monkeypatch.setattr(module.sre_metrics, "compute_metrics", _compute_metrics)

    result = await module.sre_proactive_risk_scan()

    assert result["ok"] is True
    assert result["risks"] == []
    assert "未发现高重启 Pod、Unready workload 或 Node Ready 风险" in result["summary"]
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_returns_low_noise_empty_result -q`
Expected: FAIL because `toolsets/proactive_risk_scan.py` does not exist.

- [ ] **Step 3: Implement the minimal tool module and empty-path behavior**

Create `toolsets/proactive_risk_scan.py` with these responsibilities:

- Load `k8s_read.py`, `incident_store.py`, and `sre_metrics.py` using the same file-path import pattern used elsewhere in `toolsets/`.
- Define schema:

```python
SRE_PROACTIVE_RISK_SCAN_SCHEMA = {
    "name": "sre_proactive_risk_scan",
    "description": "执行单集群主动风险扫描，输出高重启 Pod、Unready workload 与 Node 风险摘要。",
    "parameters": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "历史重复 incident 基线统计周期，默认 7 天"},
        },
    },
}
```

- Define a public async function:

```python
async def sre_proactive_risk_scan(days: int = 7) -> dict[str, Any]:
    ...
```

- For the first passing version, implement only:
  - call the three kubectl commands,
  - call `compute_metrics(days=days)`,
  - return `{"ok": True, "cluster_risk_baseline": ..., "risks": [], "summary": ...}` when no rules match.

- Register the tool with `registry.register(...)`.
- Add `_tool_sre_proactive_risk_scan(args, **_)` that returns `json.dumps(await sre_proactive_risk_scan(...), ensure_ascii=False)`.

- [ ] **Step 4: Run the focused empty-scan test**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_returns_low_noise_empty_result -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add tests/test_proactive_risk_scan.py toolsets/proactive_risk_scan.py
rtk git commit -m "feat: add proactive risk scan tool skeleton"
```

### Task 3: Detect high-restart Pod risks

**Files:**
- Modify: `tests/test_proactive_risk_scan.py`
- Modify: `toolsets/proactive_risk_scan.py`

- [ ] **Step 1: Write the failing high-restart test**

Append to `tests/test_proactive_risk_scan.py`:

```python
@pytest.mark.asyncio
async def test_scan_detects_high_restart_pod(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()

    async def _k8s_read(command: str, context: str | None = None):
        del context
        outputs = {
            "kubectl get pods -A": {
                "ok": True,
                "stdout": (
                    "NAMESPACE NAME READY STATUS RESTARTS AGE\n"
                    "default api-7d8c9f6c6f-x2m5q 1/1 Running 12 30m\n"
                ),
            },
            "kubectl get deploy -A": {"ok": True, "stdout": "NAMESPACE NAME READY UP-TO-DATE AVAILABLE AGE\ndefault api 3/3 3 3 10m\n"},
            "kubectl get nodes": {"ok": True, "stdout": "NAME STATUS ROLES AGE VERSION\nnode-a Ready worker 10d v1.30.0\n"},
        }
        return outputs[command]

    async def _list_recent_case_profiles(**kwargs):
        assert kwargs == {"namespace": "default", "final_scope": "workload", "limit": 3}
        return []

    async def _compute_metrics(days: int = 7):
        return {"repeat_incident_count": 1, "repeat_incident_rate": 0.2}

    monkeypatch.setattr(module.k8s_read_tool, "k8s_read", _k8s_read)
    monkeypatch.setattr(module.incident_store, "list_recent_case_profiles", _list_recent_case_profiles)
    monkeypatch.setattr(module.sre_metrics, "compute_metrics", _compute_metrics)

    result = await module.sre_proactive_risk_scan()

    assert len(result["risks"]) == 1
    risk = result["risks"][0]
    assert risk["risk_type"] == "high_restart_pod"
    assert risk["scope"] == "workload"
    assert risk["resource_ref"] == "default/pod/api-7d8c9f6c6f-x2m5q"
    assert risk["severity"] == "warning"
    assert "restart count is high (12)" in risk["summary"]
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_detects_high_restart_pod -q`
Expected: FAIL because the scan still returns `risks == []`.

- [ ] **Step 3: Implement Pod parsing and risk generation**

In `toolsets/proactive_risk_scan.py`:

- Add a small parser for `kubectl get pods -A` table rows.
- Parse columns as:
  - `namespace`
  - `name`
  - `ready`
  - `status`
  - `restarts`
  - `age`
- Create `high_restart_pod` risk items when:
  - `status` is `Running`, `CrashLoopBackOff`, or `Error`
  - numeric `restarts >= 5`
- Set:

```python
risk = {
    "risk_type": "high_restart_pod",
    "severity": "critical" if status in {"CrashLoopBackOff", "Error"} else "warning",
    "scope": "workload",
    "resource_ref": f"{namespace}/pod/{name}",
    "summary": f"Pod {name} restart count is high ({restarts})",
    "supporting_evidence": [
        f"kubectl get pods -A shows STATUS={status}",
        f"kubectl get pods -A shows RESTARTS={restarts}",
    ],
    "historical_context": {},
    "recommended_actions": [
        "检查该 Pod 最近 15 分钟日志",
        "核对该工作负载最近变更与资源配置",
    ],
    "confidence": 0.7,
}
```

- [ ] **Step 4: Run the focused high-restart test**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_detects_high_restart_pod -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add tests/test_proactive_risk_scan.py toolsets/proactive_risk_scan.py
rtk git commit -m "feat: detect high restart pod risks"
```

### Task 4: Detect unready workload and node risks

**Files:**
- Modify: `tests/test_proactive_risk_scan.py`
- Modify: `toolsets/proactive_risk_scan.py`

- [ ] **Step 1: Write the failing workload/node test**

Append to `tests/test_proactive_risk_scan.py`:

```python
@pytest.mark.asyncio
async def test_scan_detects_unready_workload_and_node_risk(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()

    async def _k8s_read(command: str, context: str | None = None):
        del context
        outputs = {
            "kubectl get pods -A": {"ok": True, "stdout": "NAMESPACE NAME READY STATUS RESTARTS AGE\ndefault api-1 1/1 Running 0 10m\n"},
            "kubectl get deploy -A": {
                "ok": True,
                "stdout": (
                    "NAMESPACE NAME READY UP-TO-DATE AVAILABLE AGE\n"
                    "default checkout 1/3 3 1 25m\n"
                ),
            },
            "kubectl get nodes": {
                "ok": True,
                "stdout": (
                    "NAME STATUS ROLES AGE VERSION\n"
                    "node-b NotReady worker 8d v1.30.0\n"
                ),
            },
        }
        return outputs[command]

    async def _list_recent_case_profiles(**kwargs):
        del kwargs
        return []

    async def _compute_metrics(days: int = 7):
        return {"repeat_incident_count": 0, "repeat_incident_rate": 0.0}

    monkeypatch.setattr(module.k8s_read_tool, "k8s_read", _k8s_read)
    monkeypatch.setattr(module.incident_store, "list_recent_case_profiles", _list_recent_case_profiles)
    monkeypatch.setattr(module.sre_metrics, "compute_metrics", _compute_metrics)

    result = await module.sre_proactive_risk_scan()

    risk_types = {item["risk_type"] for item in result["risks"]}
    assert risk_types == {"unready_workload", "node_risk"}
    workload_risk = next(item for item in result["risks"] if item["risk_type"] == "unready_workload")
    node_risk = next(item for item in result["risks"] if item["risk_type"] == "node_risk")
    assert workload_risk["resource_ref"] == "default/deploy/checkout"
    assert workload_risk["severity"] == "warning"
    assert node_risk["resource_ref"] == "node/node-b"
    assert node_risk["severity"] == "critical"
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_detects_unready_workload_and_node_risk -q`
Expected: FAIL because workload/node rule detection is not implemented.

- [ ] **Step 3: Implement deployment and node parsing**

In `toolsets/proactive_risk_scan.py`:

- Add a parser for `kubectl get deploy -A` rows.
- Parse `READY` like `1/3` into `ready_replicas=1`, `desired_replicas=3`.
- Emit `unready_workload` when `ready_replicas < desired_replicas`.
- Set severity to `critical` when `ready_replicas == 0`, else `warning`.
- Use:

```python
{
    "risk_type": "unready_workload",
    "severity": ...,
    "scope": "workload",
    "resource_ref": f"{namespace}/deploy/{name}",
    "summary": f"Deployment {name} is not fully ready ({ready_replicas}/{desired_replicas})",
    ...
}
```

- Add a parser for `kubectl get nodes` rows.
- Emit `node_risk` when `status != "Ready"` or `"NotReady" in status`.
- Use:

```python
{
    "risk_type": "node_risk",
    "severity": "critical",
    "scope": "node",
    "resource_ref": f"node/{name}",
    "summary": f"Node {name} is in risky state ({status})",
    ...
}
```

- [ ] **Step 4: Run the focused workload/node test**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_detects_unready_workload_and_node_risk -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add tests/test_proactive_risk_scan.py toolsets/proactive_risk_scan.py
rtk git commit -m "feat: detect workload and node risks"
```

### Task 5: Enrich workload risks with historical case profiles

**Files:**
- Modify: `tests/test_proactive_risk_scan.py`
- Modify: `toolsets/proactive_risk_scan.py`

- [ ] **Step 1: Write the failing history-enrichment test**

Append to `tests/test_proactive_risk_scan.py`:

```python
@pytest.mark.asyncio
async def test_scan_enriches_workload_risk_with_recent_case_actions(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()

    async def _k8s_read(command: str, context: str | None = None):
        del context
        outputs = {
            "kubectl get pods -A": {
                "ok": True,
                "stdout": (
                    "NAMESPACE NAME READY STATUS RESTARTS AGE\n"
                    "default api-7d8c9f6c6f-x2m5q 1/1 Running 9 30m\n"
                ),
            },
            "kubectl get deploy -A": {"ok": True, "stdout": "NAMESPACE NAME READY UP-TO-DATE AVAILABLE AGE\ndefault api 3/3 3 3 10m\n"},
            "kubectl get nodes": {"ok": True, "stdout": "NAME STATUS ROLES AGE VERSION\nnode-a Ready worker 10d v1.30.0\n"},
        }
        return outputs[command]

    async def _list_recent_case_profiles(**kwargs):
        assert kwargs == {"namespace": "default", "final_scope": "workload", "limit": 3}
        return [
            {
                "incident_id": "inc-older-1",
                "final_root_cause": "应用日志显示运行时异常",
                "effective_actions": ["检查相关 Pod 最近错误日志与超时信息", "检查 Pod CPU/内存指标与资源配置"],
            },
            {
                "incident_id": "inc-older-2",
                "final_root_cause": "资源压力可能导致工作负载异常",
                "effective_actions": ["检查 Pod CPU/内存指标与资源配置"],
            },
        ]

    async def _compute_metrics(days: int = 7):
        return {"repeat_incident_count": 2, "repeat_incident_rate": 0.4}

    monkeypatch.setattr(module.k8s_read_tool, "k8s_read", _k8s_read)
    monkeypatch.setattr(module.incident_store, "list_recent_case_profiles", _list_recent_case_profiles)
    monkeypatch.setattr(module.sre_metrics, "compute_metrics", _compute_metrics)

    result = await module.sre_proactive_risk_scan()

    risk = result["risks"][0]
    assert risk["historical_context"]["recent_case_count"] == 2
    assert "应用日志显示运行时异常" in risk["historical_context"]["recent_root_causes"]
    assert "检查 Pod CPU/内存指标与资源配置" in risk["historical_context"]["common_effective_actions"]
    assert "检查 Pod CPU/内存指标与资源配置" in risk["recommended_actions"]
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_enriches_workload_risk_with_recent_case_actions -q`
Expected: FAIL because risks do not yet include historical context.

- [ ] **Step 3: Implement minimal historical enrichment**

In `toolsets/proactive_risk_scan.py`:

- For each workload risk, call:

```python
recent_cases = await incident_store.list_recent_case_profiles(
    namespace=namespace,
    final_scope="workload",
    limit=3,
)
```

- Add `historical_context` fields:

```python
{
    "recent_case_count": len(recent_cases),
    "recent_root_causes": [case["final_root_cause"] for case in recent_cases if case.get("final_root_cause")],
    "common_effective_actions": deduped_actions,
}
```

- Merge up to 2 deduped `common_effective_actions` into `recommended_actions`, preserving the current rule-based actions first.
- Keep history enrichment best-effort: if the lookup fails, leave `historical_context` empty.

- [ ] **Step 4: Run the focused history-enrichment test**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_enriches_workload_risk_with_recent_case_actions -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add tests/test_proactive_risk_scan.py toolsets/proactive_risk_scan.py
rtk git commit -m "feat: enrich proactive workload risks with case history"
```

### Task 6: Add repeat-baseline summary text and plugin registration

**Files:**
- Modify: `tests/test_proactive_risk_scan.py`
- Modify: `toolsets/proactive_risk_scan.py`
- Modify: `/home/mao/.hermes/plugins/sre/plugin.yaml`
- Modify: `/home/mao/.hermes/plugins/sre/__init__.py`
- Modify: `tests/test_sre_plugin.py`

- [ ] **Step 1: Write the failing summary/plugin tests**

Append to `tests/test_proactive_risk_scan.py`:

```python
@pytest.mark.asyncio
async def test_scan_summary_mentions_repeat_baseline(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()

    async def _k8s_read(command: str, context: str | None = None):
        del context
        outputs = {
            "kubectl get pods -A": {
                "ok": True,
                "stdout": (
                    "NAMESPACE NAME READY STATUS RESTARTS AGE\n"
                    "default api-7d8c9f6c6f-x2m5q 1/1 Running 8 30m\n"
                ),
            },
            "kubectl get deploy -A": {"ok": True, "stdout": "NAMESPACE NAME READY UP-TO-DATE AVAILABLE AGE\ndefault api 3/3 3 3 10m\n"},
            "kubectl get nodes": {"ok": True, "stdout": "NAME STATUS ROLES AGE VERSION\nnode-a Ready worker 10d v1.30.0\n"},
        }
        return outputs[command]

    async def _list_recent_case_profiles(**kwargs):
        del kwargs
        return []

    async def _compute_metrics(days: int = 7):
        return {"repeat_incident_count": 3, "repeat_incident_rate": 0.25}

    monkeypatch.setattr(module.k8s_read_tool, "k8s_read", _k8s_read)
    monkeypatch.setattr(module.incident_store, "list_recent_case_profiles", _list_recent_case_profiles)
    monkeypatch.setattr(module.sre_metrics, "compute_metrics", _compute_metrics)

    result = await module.sre_proactive_risk_scan(days=7)

    assert result["cluster_risk_baseline"]["repeat_incident_count"] == 3
    assert result["cluster_risk_baseline"]["repeat_incident_rate"] == 0.25
    assert "最近 7 天重复 incident 比例为 25.0%" in result["summary"]
```

Update `tests/test_sre_plugin.py`:

```python
    "sre_proactive_risk_scan",
```

Expected in both the manifest set and registered tool set.

- [ ] **Step 2: Run focused tests to verify RED**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_summary_mentions_repeat_baseline tests/test_sre_plugin.py -q`
Expected: FAIL because the summary text and plugin registration are missing.

- [ ] **Step 3: Implement repeat-baseline summary and plugin wiring**

In `toolsets/proactive_risk_scan.py`:

- Add summary rendering rules:
  - If risks exist, mention total risk count and risk categories.
  - If `repeat_incident_rate` is not `None`, append:

```python
f"最近 {days} 天重复 incident 比例为 {repeat_incident_rate * 100:.1f}%"
```

- Preserve the no-risk summary from Task 2 when no risks exist.

In `/home/mao/.hermes/plugins/sre/plugin.yaml`, add:

```yaml
  - sre_proactive_risk_scan
```

In `/home/mao/.hermes/plugins/sre/__init__.py`, add `"proactive_risk_scan"` to `MODULES_TO_IMPORT`.

Update `tests/test_sre_plugin.py` so `EXPECTED_TOOLS` includes `"sre_proactive_risk_scan"`.

- [ ] **Step 4: Run focused tests**

Run: `rtk pytest tests/test_proactive_risk_scan.py::test_scan_summary_mentions_repeat_baseline tests/test_sre_plugin.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full related regression suite**

Run: `rtk pytest tests/test_proactive_risk_scan.py tests/test_incident_store.py tests/test_sre_metrics.py tests/test_sre_plugin.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_proactive_risk_scan.py toolsets/proactive_risk_scan.py tests/test_incident_store.py toolsets/incident_store.py tests/test_sre_plugin.py /home/mao/.hermes/plugins/sre/plugin.yaml /home/mao/.hermes/plugins/sre/__init__.py
rtk git commit -m "feat: add proactive risk scan tooling"
```

## Self-Review Checklist

- [ ] Confirm the implementation never introduces scheduling, auto-remediation, or multi-cluster abstractions.
- [ ] Confirm the risk model stays readable dictionaries, not a heavyweight framework.
- [ ] Confirm history enrichment is best-effort and never blocks real-time risk output.
- [ ] Confirm all new assertions are deterministic with stubbed tool outputs.
- [ ] Confirm the final regression command passes before claiming completion.
