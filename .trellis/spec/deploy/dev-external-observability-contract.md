# dev-external Observability Backend Contract

> A `dev-external` smoke (ADR-0005 Issue A) does **not** end at "pods Ready +
> env points at real backends". It ends at "an alert → Gateway → Hermes run
> persists four-channel evidence to `incident_evidence` with non-empty
> `aiops-dev` payload and redaction". Three deploy/runtime contracts must all
> hold for that to happen. This spec is the source-backed truth for each.

## Scenario: dev-external end-to-end evidence smoke (ADR-0005 Issue A)

### 1. Scope / Trigger

Trigger: any change that switches a cluster to `dev-external`, points MCP at a
real Prometheus/Loki, or re-runs the alertmanager → Gateway → Hermes evidence
smoke. The contract is cross-layer (deploy overlay → runtime env → MCP HTTP →
Hermes `_collect_evidence` → `incident_evidence` row), so code-spec depth is
mandatory.

### 2. Signatures

- Overlay ConfigMap patch: `deploy/k8s/overlays/dev-external/kustomization.yaml`
  targets ConfigMap `aiops-runtime-config` and replaces three keys.
- MCP services read env at process start: `aiops-mcp-prometheus` reads
  `PROMETHEUS_URL`; `aiops-mcp-loki` reads `LOKI_URL`; `aiops-connector` and
  the MCPs read `AIOPS_NAMESPACE_SCOPE` (comma-separated).
- Hermes persistence: `toolsets/incident_store.IncidentStore.add_evidence`
  (`toolsets/incident_store.py:477`) inserts one row per channel into
  `incident_evidence`. `_collect_evidence` (`toolsets/incident_diagnosis.py:640`)
  is the only caller for the diagnosis path and is invoked per observation step
  inside `run_diagnosis_session` (`toolsets/incident_diagnosis.py:112`).

### 3. Contracts

ConfigMap `aiops-runtime-config` (three values, verbatim):

| Key | dev-external value | Source |
|-----|--------------------|--------|
| `PROMETHEUS_URL` | `http://prometheus-stack-kube-prom-prometheus.loki.svc.cluster.local:9090` | kube-prometheus-stack chart default service `<release>-kube-prom-prometheus` |
| `LOKI_URL` | `http://<loki-svc>.loki.svc.cluster.local:3100` | confirm `<loki-svc>` with `kubectl -n loki get svc` after `helm install`; SingleBinary chart commonly exposes `loki` |
| `AIOPS_NAMESPACE_SCOPE` | `aiops-dev` (or comma-separated business namespaces; never the placeholder `default,prod`) | `deploy/k8s/README.md:17` warns the placeholder makes K8s evidence a no-op |

`incident_evidence` row contract (`toolsets/incident_store.py:162`):

- `incident_id TEXT NOT NULL` (FK → `incidents.id`)
- `source_type TEXT NOT NULL` ∈ `{"metrics","logs","topology","k8s_read"}`
  (`toolsets/incident_diagnosis.py:13 EVIDENCE_SOURCES`)
- `source_ref TEXT`, `summary TEXT NOT NULL`, `payload_json TEXT NOT NULL`
- `confidence REAL`, `collected_at REAL NOT NULL`, `collector_version TEXT`
- `payload_json` is *already redacted* before insert by
  `_redact_payload` (`toolsets/incident_diagnosis.py:680`): k8s_read →
  `redact_k8s_output`, others → `redact_sensitive_text`. No raw secret lands in
  the row regardless of what the backend returned.

Backend reachability + label contract (verified from an `aiops-dev` pod):

- Prometheus `/api/v1/query?query=up` returns `status:"success"`.
- Loki `/ready` returns `ready`; `/loki/api/v1/labels` includes `namespace`;
  `/loki/api/v1/label/namespace/values` includes the target namespace.
- MCP read env matches ConfigMap: `kubectl -n aiops-dev exec deploy/aiops-<svc>
  -- sh -c 'echo $PROMETHEUS_URL $LOKI_URL $AIOPS_NAMESPACE_SCOPE'`.

### 4. Validation & Error Matrix

| Condition | Observable | Status |
|-----------|-----------|--------|
| Env not rolled out (pod still reads old cm) | `exec ... echo $PROMETHEUS_URL` still `monitoring`/placeholder; only `logs` evidence, `line_count:0` | restart core deploys (base has no `rollme`/checksum annotation) |
| Backend unreachable | MCP HTTP error; `audit.error_code=backend_unavailable`; observation `status:failed` | fix Service name / DNS / NetworkPolicy |
| Backend reachable, no scrape target for `aiops-dev` | Prometheus `up{namespace="aiops-dev"}` empty; metrics observation `skipped` | add ServiceMonitor for AIOps (see §5) |
| Backend reachable, app pods emit no stdout | Loki `query_range {namespace="aiops-dev"}` returns only smoke pods; logs observation `line_count:0` | app must log to stdout (see §5) |
| Hard failure in any step (`error_code ∈ TERMINAL_FAILURE_CODES`) | session `status:failed`; `incident_analysis` not written; only channels before the failure have evidence rows | fix the failing channel; do not rely on "logs only" as pass |
| All channels empty but no hard failure | session `status:needs_human` (no `evidence_refs`) or `partial`; evidence rows may still be missing | this is *not* the Issue A pass state |

### 5. Good / Base / Bad Cases

- **Good**: alert fires → four `incident_evidence` rows across
  `metrics/logs/k8s_read/topology`, each `payload_json` non-empty and carrying
  `namespace=aiops-dev` content, no raw secret present; `incident_analysis`
  written; session `status: diagnosed` or `partial`.
- **Base**: backend reachable, ≥1 channel has non-empty `aiops-dev` data, the
  rest are `skipped` with a real `missing_reason`; session `partial`;
  `incident_analysis` written. Acceptable for a logging-only incident per
  ADR-0005, but a *no-op* logs result (`line_count:0` on the alert's target)
  is **not** Base — it is Bad.
- **Bad (three known gaps that each fail the smoke, none fixed by deploy alone)**:
  1. **AIOps apps emit no stdout.** `kubectl -n aiops-dev logs <aioPod>` is empty
     for gateway/connector/hermes/mcp; Loki `namespace=aiops-dev` values contain
     only the smoke pod. alloy cannot scrape what is not written. This violates
     `backends/logging-guidelines` (stdout is the collection surface for Loki).
  2. **No ServiceMonitor covers `aiops-dev`.** kube-prometheus-stack default
     scrapes k8s system components only; `up{namespace="aiops-dev"}` is empty.
     AIOps must ship its own ServiceMonitor/PodMonitor for MCP/Prometheus to see
     it, or expose a metrics port with annotated discovery.
  3. **Diagnosis doesn't tolerate empty backend.** With no pod target in the
     alert and empty backends, `k8s_read`/`topology`/`metrics` go `skipped`,
     `run_diagnosis_session` derives `needs_human` or trips a hard failure, and
     `incident_analysis` is never written (`toolsets/incident_diagnosis.py:149`
     `_persist_diagnosis` is reached but `_derive_session_status` gating +
     `TERMINAL_FAILURE_CODES` can mark the session `failed`). A failed session
     can still have *some* evidence rows, so "row count > 0" is not proof of
     success — read `investigate_end` and `incident_analysis`.

### 6. Tests Required

The smoke is the executable contract. Assertion points (run from inside the
cluster, in `aiops-dev`):

- Env: `kubectl -n aiops-dev exec deploy/aiops-gateway -- sh -c 'echo
  $PROMETHEUS_URL$LOKI_URL$AIOPS_NAMESPACE_SCOPE'` matches the overlay (see §3).
- Backend reachable: Prometheus `up` `status:success`; Loki `/ready` `ready`;
  Loki `label/namespace/values` contains the scope namespace.
- Post a synthetic alert (README smoke) and assert on `incident_evidence`:
  - `SELECT count(*) FROM incident_evidence WHERE incident_id=?` >= 1
    AND at least one row's `payload_json` is non-`{}` and references the scope
    namespace.
  - `SELECT source_type FROM incident_evidence WHERE incident_id=?` covers ≥1
    of the four channels; a logging-only result must have a non-zero
    `line_count` in its `payload_json`.
  - Redaction: no `payload_json` contains raw secret patterns (assert by
    re-running `redact_*` is idempotent on stored payload — stored bytes must
    equal a fresh redact of the fresh payload).
  - Session outcome: `incident_events` `investigate_end` `output_summary`
    must *not* read `status failed` for a pass; `incident_analysis` row must
    exist for a diagnoser pass verdict (`toolsets/incident_store.py:543
    upsert_analysis`).

Command shape (heredoc to stdin, `exec -i` is mandatory — `python3 -` reads
stdin and without `-i` kubectl drops it silently, producing no output with no
error):

```bash
kubectl -n aiops-dev exec -i deploy/aiops-hermes -- python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/data/aiops/incidents.db"); c.row_factory = sqlite3.Row
row = c.execute("SELECT id FROM incidents ORDER BY created_at DESC LIMIT 1").fetchone()
inc = row["id"]
ev = c.execute("SELECT source_type,payload_json FROM incident_evidence WHERE incident_id=?", (inc,)).fetchall()
print("rows", len(ev), "sources", sorted({e["source_type"] for e in ev}))
an = c.execute("SELECT 1 FROM incident_analysis WHERE incident_id=?", (inc,)).fetchone()
print("analysis_written", bool(an))
end = c.execute("SELECT output_summary FROM incident_events WHERE incident_id=? AND event_type='investigate_end'", (inc,)).fetchone()
print("end", end["output_summary"] if end else None)
PY
```

DB path is `$AIOPS_DATA_DIR/incidents.db` (env-driven; container
`AIOPS_DATA_DIR=/data/aiops`, so `/data/aiops/incidents.db`). Determined by
`_default_db_path` (`toolsets/incident_store.py:233`).

### 7. Wrong vs Correct

#### Wrong — treat "evidence row exists" as pass

```bash
# only checks count; a single empty logs row passes this and hides a failed session
kubectl -n aiops-dev exec -i deploy/aiops-hermes -- python3 -c \
  "import sqlite3; print(sqlite3.connect('/data/aiops/incidents.db').execute('SELECT count(*) FROM incident_evidence').fetchone())"
```

Why wrong: `_collect_evidence` writes a `logs` row even when `line_count:0`
(status `skipped`/`partial`), and a `failed` session can still have rows before
the failing step. The row proves the *write path* works, not that *evidence was
collected*.

#### Correct — assert reachable backend + non-empty payload + session status

Validate env is rolled out (§3), backend is reachable (§3), then assert the
`incident_evidence` payload is non-empty on the scope namespace **and**
`investigate_end` does not say `failed` **and** `incident_analysis` was written.
A non-empty `payload_json` with `line_count > 0` (logs) or a real metric series
(metrics) is the Issue A pass signal; a `failed` session or missing
`incident_analysis` is a fail even with rows present.

### Prerequisites tracked as deploy facts (not fixed in the deploy task)

These are the three Bad-case gaps above. They are each independent work items;
the deploy task only surfaces them by making the smoke observably fail:

1. **AIOps app stdout logging** — every AIOps service must emit request/diagnosis
   lifecycle lines to stdout so alloy scrapes them. Spec owner:
   `backends/logging-guidelines.md`. Until fixed, Loki evidence for AIOps pods is
   structurally empty.
2. **AIOps ServiceMonitor** — a ServiceMonitor (or annotated PodMonitor) for the
   AIOps services in `aiops-dev` so kube-prometheus-stack scrapes them. Until
   added, `up{namespace="aiops-dev"}` is empty and metrics evidence is `skipped`.
3. **Diagnosis empty-backend tolerance** — `run_diagnosis_session` /
   `_derive_session_status` should not mark the session `failed` solely because
   a channel returned empty on a no-target alert; an empty-but-reachable backend
   is `partial`/`needs_human` with a real `missing_reason`, and
   `incident_analysis` should still be written. Spec owner:
   `toolsets/incident_diagnosis.py:591`. Until fixed, the Issue A smoke can
   report `failed` even though the deploy + env are correct.

### Rollout note (the silent failure mode)

Base Deployments have no `rollme`/checksum annotation keyed to the ConfigMap.
`kubectl apply -k` updates `aiops-runtime-config` but does **not** restart pods,
so `exec ... echo $PROMETHEUS_URL` can still show the *old* value while
`kubectl kustomize` shows the *new* value. After changing the overlay, always
`rollout restart` the core deploys and re-verify the in-pod env before reading
evidence.