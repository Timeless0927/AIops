# Rename Hermes diagnosis service boundary - design

## Decision

Rename the production diagnosis service boundary from `hermes` to `diagnosis`.

Canonical names:

- Python package: `diagnosis_service`
- Docker target / matrix service: `diagnosis`
- Image: `registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-diagnosis`
- K8S Deployment / Service / labels: `aiops-diagnosis`
- Runtime env prefix: `AIOPS_DIAGNOSIS_*`
- HTTP response `service`: `diagnosis`

Legacy aliases stay for one compatibility window:

- `AIOPS_HERMES_*` remains accepted when the matching `AIOPS_DIAGNOSIS_*` is unset.
- `AIOPS_HERMES_URL` and `AIOPS_HERMES_DIAGNOSIS_PATH` keep working in Gateway handoff.
- `aiops-hermes` K8S Service remains as an alias Service selecting the new diagnosis pods.
- `aiops-hermes-data` PVC is not renamed in-place; data PVC names are operational state, not code semantics.
- Response payloads may include `legacy_service: "hermes"` only where tests or external callers need a bridge.

Out of scope:

- Renaming the vendored `hermes-agent` submodule or its CLI/config variables (`HERMES_HOME`, `HERMES_CONFIG`).
- Changing diagnosis behavior, provider choice, replay scoring, approval, or notification behavior.
- Changing public Gateway browser contracts.

## Boundary

This task only renames the AIOps diagnosis runtime that currently lives in `hermes/`.
The old external `hermes-agent/` dependency remains a different thing and should not be bulk-renamed.

## Compatibility

Use new names first, old names second:

```text
AIOPS_DIAGNOSIS_URL -> AIOPS_HERMES_URL
AIOPS_DIAGNOSIS_PATH -> AIOPS_HERMES_DIAGNOSIS_PATH
AIOPS_DIAGNOSIS_HOST -> AIOPS_HERMES_HOST
AIOPS_DIAGNOSIS_PORT -> AIOPS_HERMES_PORT
AIOPS_DIAGNOSIS_TOOL_TIMEOUT_SECONDS -> AIOPS_HERMES_TOOL_TIMEOUT_SECONDS
AIOPS_DIAGNOSIS_GATEWAY_SERVICE_TOKEN -> AIOPS_HERMES_GATEWAY_SERVICE_TOKEN
AIOPS_DIAGNOSIS_WRITEBACK_TIMEOUT_SECONDS -> AIOPS_HERMES_WRITEBACK_TIMEOUT_SECONDS
```

Keep `HERMES_HOME` / `HERMES_CONFIG` unchanged because they belong to the vendored Hermes CLI config, not the AIOps diagnosis service boundary.

Gateway handoff should log new event names (`diagnosis_handoff_*`) while preserving old metadata keys where existing console/audit code reads them.

## Migration Shape

1. Add compatibility helpers and tests while code still imports the old package.
2. Rename Python package imports from `hermes` to `diagnosis_service`; keep a tiny `hermes` import shim for one release.
3. Rename service/image/K8S resources to `aiops-diagnosis`; keep an `aiops-hermes` Service alias.
4. Update docs/spec/ADR references to say Hermes is legacy naming.
5. Run broad tests and image/compose smoke before deployment.

Rollback is the old image tag plus old K8S manifests. The alias Service and env fallback are the rollback cushion.

## Risk Points

- Python import collisions with vendored `hermes-agent/hermes`.
- Gateway handoff env and URL defaults.
- K8S Service DNS changes.
- Tests that assert `service == "hermes"`.
- Docs/spec references mixing AIOps diagnosis service with vendored Hermes CLI.
