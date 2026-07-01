# Testing

> pytest (plain functions + `parametrize`), tests in flat `tests/`. Async tests
> run **without** pytest-asyncio via a `conftest.py` runner. HTTP routes are tested
> by spinning up the **real** `ThreadingHTTPServer` and hitting it with `urllib`.

---

## Conftest — `tests/conftest.py`

```python
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: 运行异步测试用例")

def pytest_pyfunc_call(pyfuncitem):
    test_func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_func):
        return None
    asyncio.run(test_func(**pyfuncitem.funcargs))
    return True
```

Rules:
- **`async def test_X`** just works — `conftest.pytest_pyfunc_call` drives it with
  `asyncio.run`. Do **not** mark async tests with `@pytest.mark.asyncio`; the
  marker exists only for discoverability. If `pytest-asyncio` is later installed
  this stays compatible.
- `tests/conftest.py` also adds the repo root to `sys.path` so `tests/` imports
  project modules directly — no install step needed for tests.

---

## Three testing strategies (pick the right one)

### A. New route / end-to-end authz — real HTTP server

`tests/test_command_gateway_skeleton.py:538`, `tests/test_gateway_identity_rbac.py:491`:

```python
gateway_main._ROUTES.clear(); gateway_main._SESSIONS.clear()
gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
gateway_thread.start()
gateway_url = f"http://127.0.0.1:{gateway_server.server_address[1]}"
monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path / "data"))
monkeypatch.setenv("AIOPS_IDENTITY_CONFIG", str(identity_config))
...
# login over HTTP, then call /k8s/read with Bearer token via urllib.request
```

- Use `ThreadingHTTPServer(("127.0.0.1", 0), Handler)` for an ephemeral port; read
  it from `server.server_address[1]`.
- **Always** reset module globals between tests: `gateway_main._ROUTES.clear()`,
  `gateway_main._SESSIONS.clear()` (they're process-level mutable state).
- Point env at `tmp_path` (`AIOPS_DATA_DIR`, `AIOPS_IDENTITY_CONFIG`,
  `AIOPS_CONNECTOR_URL`) so tests are isolated and don't touch `data/`.
- `subprocess.Popen` / `unittest.mock.patch(...)` is used to fake the connector's
  kubectl child process (`tests/test_command_gateway_skeleton.py:580`).
- Clean up in `finally`: `server.shutdown(); server.server_close(); thread.join()`.
- Prefer this style **for any new route or any authz guarantee** — it is the only
  style that exercises `_authorize` + audit + the real envelope.

### B. Pure domain / store logic — call the module directly

`tests/test_command_gateway_skeleton.py:42` (`CommandTaskStore` state machine),
`tests/test_gateway_identity_rbac.py:170` (wildcard `Scope`, `SQLiteIdentityStore`):

```python
actor = SQLiteIdentityStore(tmp_path / "identity.db").upsert_user({...})
assert actor.can(PERMISSION_VIEW_INCIDENT, resource_scope(service="any-service", ...))
```

Use this for dataclasses, state machines (`Grant.consume`, `CommandTask.transition`),
envelope round-trips (`CommandEnvelope.from_dict(envelope.to_dict()) == envelope`),
and standalone store CRUD pointed at a `tmp_path` db.

### C. Legacy hooks — load by file path (legacy only)

`tests/test_approval_authorization.py:12` loads `hooks/approval_authorization.py`
via `importlib.util.spec_from_file_location`. **Do not add new tests in this
style** — new authorization lives in `aiops/domain/identity.py` and is tested by
strategy A/B. This exists only because the legacy hook hasn't been deleted yet.

---

## Parameterization

`pytest.mark.parametrize` is used heavily for boundary/allow-list exhaustion
(`tests/test_command_gateway_skeleton.py:239` connector argv allow-list). Pattern:

```python
@pytest.mark.parametrize(("overrides", "message"), [
    ({"namespace": "kube-system"}, "namespace_out_of_scope"),
    ({"argv": ("bash", "-lc", "kubectl get pods")}, "command_rejected"),
    ...
])
def test_connector_validation_rejects_invalid_envelope(overrides, message):
    with pytest.raises(ValueError, match=message):
        validate_command_envelope(_command_envelope(**overrides), ...)
```

- Build shared fixtures as a plain helper (`_command_envelope(**overrides)`,
  `_approval(namespace=...)`), not `@pytest.fixture`, for inputs that vary per
  parametrize case.
- Assert exceptions with `pytest.raises(ValueError, match=...)`, never `try/except`.
- `tmp_path` and `monkeypatch` are the standard fixtures; there is **no**
  project-wide shared fixture factory.

## Frontend slice tests (no JS runtime)

`tests/test_aiops_console_incident_detail.py` treats the static slice as text:
it asserts the files exist, that `fetch(`/`XMLHttpRequest` are absent from the JS,
that the README documents the Gateway-only contract, and parses the
`window.AIOPS_INCIDENT_FIXTURES = {...}` object out of the fixture JS via regex.
It does **not** run the JS. (See frontend specs.)

## Replay eval harness (ADR-0003 child 3)

`tests/replay_incident.py` is the diagnosis replay harness: frozen incident
fixtures (`tests/fixtures/incidents/<id>/incident.json` + `evidence/*.json` +
`truth.json`) replayed through `run_diagnosis_session` with a `ScriptedProvider`,
scored against the ground-truth `root_cause_category` with a tolerance matrix
(ADR-0005 §决策 4: 类目带容差,不靠字符串相等).

- The harness is Strategy B (pure module call), not a live diagnosis: the
  `ScriptedProvider` scripts re-issue the recorded tool-use trajectory, and a
  `FrozenAdapter` yields the frozen evidence rows as tool observations.
- Sample fixtures carry `synthetic: true` and **never count** toward the
  ADR-0003 ≥10 real-fixture V1 gate. Real operational fixtures carry
  `synthetic: false`; the report splits real vs synthetic columns, and V1 is
  satisfied only when `real_count >= 10` and the real hit-rate is readable.
- Model output aligns to truth via a `category` field the brain emits in its
  final JSON (`toolsets/incident_diagnosis.py` prompt schema), scored with a
  hand-maintained `ROOT_CAUSE_CATEGORIES` / `CATEGORY_GROUPS` matrix. Extend
  the matrix when a new ground-truth category lands; `--validate-taxonomy`
  self-checks for dangling entries.
- **Tolerance-matrix reachability**: every scoring branch must be reachable
  against the actual truth vocabulary shape. Truths are always **leaf**
  `root_cause_category` values, never an upper bucket — so an "upper-bucket /
  parent" tolerance branch is dead code and must not be shipped. A branch no
  real fixture can ever exercise reads as coverage it doesn't have.

### Scenario: real incident fixture export and replay validation

#### 1. Scope / Trigger

- Trigger: ADR-0003 V1 uses live Kubernetes + Prometheus/Loki + LLM tool-use runs,
  then freezes each incident into `tests/fixtures/incidents/<incident_id>/`.
- Boundary: live `incidents.db` tables (`incidents`, `incident_evidence`,
  `diagnosis_trace`, `incident_case_profiles`) → `tests/export_incident.py` →
  replay fixture files → `tests/replay_incident.py` scoring.

#### 2. Signatures

```bash
python3 tests/export_incident.py <incident_id> \
  --session-id <diagnosis_session_id> \
  [--db /data/aiops/incidents.db] \
  [--out tests/fixtures/incidents] \
  [--force]

python3 tests/replay_incident.py --root tests/fixtures/incidents --json
python3 tests/replay_incident.py --validate-taxonomy
```

Fixture shape:

- `incident.json`: `incident_id`, `session_id`, `alert_name`, `namespace`,
  `cluster`, `service`, `summary`, optional `time_range`, `synthetic:false`.
- `evidence/NN_<source>.json`: one row per diagnosis trace step with `tool`,
  `status`, `summary`, `payload`, `namespace`, `service`, `ref_id`, `tool_args`.
- `truth.json`: `synthetic:false`, `root_cause_category`, `final_root_cause`,
  `key_evidence_refs`, `effective_actions`, optional `recorded_prediction`.

#### 3. Contracts

- `truth.root_cause_category` comes only from human-backfilled
  `incident_case_profiles.root_cause_category`; never infer it from the model.
- `truth.recorded_prediction` comes from `incidents.diagnosis_json`; it records
  what the brain produced, not truth.
- Link trace to evidence by `diagnosis_trace.observation_ref` ↔
  `incident_evidence.source_ref` first. Use positional pairing only for trace
  rows with no `observation_ref`.
- A trace step with no matching evidence becomes an evidence fixture row with
  empty `payload` and `_trace_only_missing_evidence: true`.
- The replay CLI sets `AIOPS_AGENT_MAX_TURNS=90` by default so long recorded
  tool-use trajectories do not accidentally hit the live default turn cap.

#### 4. Validation & Error Matrix

| Condition | Expected behavior |
|---|---|
| Missing incident id | exporter exits with `incident not found: <id>` |
| Existing fixture dir without `--force` | exporter exits and refuses overwrite |
| Case profile missing | fixture writes empty truth fields; do not count as a valid real fixture |
| Trace row has ref but no matching evidence | mark that step `_trace_only_missing_evidence`, do not steal a later evidence row |
| New truth category | add to `ROOT_CAUSE_CATEGORIES` and `CATEGORY_GROUPS`; `--validate-taxonomy` must pass |

#### 5. Good/Base/Bad Cases

- Good: 10 real fixtures have `synthetic:false`, non-empty truth category, replay
  returns `real_count >= 10` and a non-0/0 `real_hit_rate`.
- Base: synthetic sample fixtures still replay, but are excluded from
  `real_count`.
- Bad: position-only trace/evidence pairing after a failed middle tool step
  attaches the next payload to the wrong tool and corrupts replay evidence.

#### 6. Tests Required

- `tests/test_export_incident.py`: exporter maps incident/evidence/truth fields,
  refuses overwrite, loads copied SQLite snapshots, handles float confidence,
  and regression-tests failed-middle-step skew.
- `tests/replay_incident.py --validate-taxonomy`: no dangling categories.
- `tests/replay_incident.py --root tests/fixtures/incidents --json`: real count
  and hit-rate are machine-readable.

#### 7. Wrong vs Correct

Wrong:

```python
# evidence row count can be smaller than trace row count when a tool failed
ev = evidence[trace_index]
```

Correct:

```python
if trace.observation_ref in evidence_by_source_ref:
    ev = evidence_by_source_ref[trace.observation_ref]
elif not trace.observation_ref:
    ev = next_unconsumed_evidence_row()
else:
    ev = None
```

## Live LLM provider regression coverage

Live OpenAI-compatible providers expose bugs that `ScriptedProvider` unit tests
can miss. The provider layer must include at least one test that exercises the
real `ProviderConfig` object shape expected by `run_diagnosis_session`
(`provider.chat_with_tools(messages, tools)`), not only fake providers.

`run_diagnosis_session` also has two live-output contracts:

- Tool-use max turns are runtime configurable. `AIOPS_LLM_TOOLUSE_MAX_TURNS`
  overrides `AIOPS_AGENT_MAX_TURNS`; invalid or non-positive values fall back to
  the code default.
- Final model content may include a short preface or a fenced ```json block.
  The parser extracts the first balanced JSON object and still falls back to the
  keyword path when no valid JSON object exists.

Required regression points:

- `ProviderConfig.chat_with_tools(...)` bound method delegates to the module
  OpenAI-compatible implementation.
- `AIOPS_AGENT_MAX_TURNS` / `AIOPS_LLM_TOOLUSE_MAX_TURNS` can prevent premature
  keyword fallback on longer live tool-use trajectories.
- Fenced or prefaced final JSON parses; non-JSON final content falls back.

## Common mistakes

- Forgetting to reset `_ROUTES`/`_SESSIONS` in a Gateway test → flaky cross-test
  pollution.
- Pointing a store at the real `data/` dir instead of `tmp_path`.
- Using `pytest-asyncio` decorator or `@pytest.fixture` where a plain helper is
  shorter — match the existing flat style.
- A test that mocks `_authorize` to "always allow" — it then proves nothing about
  authz. Exercise the real path through the HTTP server.
- Shipping an unreachable tolerance-matrix branch (e.g. "parent bucket" when
  truth is always a leaf category) — it inflates apparent coverage. Verify each
  branch against the real truth-vocabulary shape, and add a unit test per branch.
- Trusting `ScriptedProvider` coverage as proof of the live provider seam. Also
  test `ProviderConfig.chat_with_tools(...)` because production passes a
  provider config object, not the fake provider used by most tool-use tests.
- Pairing exported trace/evidence rows by position only. A failed middle tool
  writes trace but no evidence, shifting later rows.
