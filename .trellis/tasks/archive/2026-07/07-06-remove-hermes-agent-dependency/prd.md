# Remove hermes-agent runtime dependency

## Goal

Remove the vendored NousResearch `hermes-agent/` submodule from the AIOps
runtime path before renaming the self-built diagnosis service boundary.

The project now owns the diagnosis brain in `hermes/` / `toolsets/`; the
external agent runtime is no longer part of the product direction.

## Confirmed Facts

- ADR-0003 says to delete `hermes-agent` and remove the editable dependency.
- `Dockerfile.aiops` still copies and installs `hermes-agent[messaging,feishu]`.
- `requirements.txt` still installs `-e ./hermes-agent[cli,feishu,dingtalk]`.
- `runtime/hermes_gateway.py` still starts `hermes_cli.gateway.run_gateway`.
- `hermes/__main__.py` still forwards `python -m hermes` into `hermes_cli.main`.
- Several tests intentionally assert the old dependency exists.
- `HERMES_HOME` / `HERMES_CONFIG` are still used by config compatibility code;
  removing the agent does not require renaming those env vars in this task.

## Requirements

1. Remove production/runtime dependency on the vendored `hermes-agent/` submodule.
2. Keep the split AIOps services working: Gateway, current `hermes/` diagnosis
   service, Connector, MCP services, Console.
3. Remove or replace all tests that require `hermes-agent/` to exist.
4. Do not change diagnosis behavior.
5. Do not start the `hermes -> diagnosis_service` rename in this task.
6. Keep Feishu approval/notification paths only if they are implemented in this
   repo; do not depend on `hermes_cli`.

## Acceptance Criteria

- [ ] `requirements.txt` no longer references `./hermes-agent`.
- [ ] `Dockerfile.aiops` no longer copies or installs `hermes-agent`.
- [ ] No runtime module imports `hermes_cli`.
- [ ] The `hermes-agent` submodule and `.gitmodules` entry are removed.
- [ ] Split service image smoke and compose smoke still pass.
- [ ] Existing diagnosis, Gateway handoff, approval, and K8S manifest tests pass
      or are updated to the new no-agent contract.

## Notes

- This is the prerequisite for `07-01-hermes-diagnosis-service-rename`.
