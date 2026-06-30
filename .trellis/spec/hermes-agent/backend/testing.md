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
  ADR-0003 ≥10 real-fixture V1 gate — that campaign lands separately via Issue
  A 真采证 + Issue B 真根因回填. The report splits real vs synthetic columns so
  the real hit-rate reads 0/0 honestly while the campaign is pending.
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
