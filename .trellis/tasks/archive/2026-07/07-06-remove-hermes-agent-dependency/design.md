# Remove hermes-agent runtime dependency - design

## Decision

Delete the vendored `hermes-agent/` runtime dependency before service rename.

Keep the current self-built diagnosis service under `hermes/` for this task.
The next task can rename it to `diagnosis_service` after the external-agent
collision is gone.

## What Goes Away

- `.gitmodules` entry for `hermes-agent`
- `hermes-agent/` submodule directory
- `requirements.txt` editable install of `./hermes-agent`
- `Dockerfile.aiops` `hermes-runtime` stage that installs `hermes-agent`
- `runtime/hermes_gateway.py` dependency on `hermes_cli.gateway`
- `hermes/__main__.py` forwarding to `hermes_cli.main`

## What Stays

- `hermes/` diagnosis service code until the follow-up rename task.
- `HERMES_HOME` / `HERMES_CONFIG` compatibility envs where existing repo-owned
  config readers still use them.
- Gateway, Connector, MCP, Console service boundaries.
- Feishu approval and notification code implemented in this repository.

## Replacement Strategy

Use deletion first.

- If `runtime/hermes_gateway.py` only exists to run the old agent gateway, remove
  the runtime entry and tests instead of recreating it.
- If `python -m hermes` only launches the old CLI, change it to print a short
  unsupported message or remove the tests, whichever is less invasive.
- Keep repo-owned Feishu approval overlay code only if it is still imported by
  current Gateway/worker paths without `hermes_cli`.

## Risk Points

- All-in-one `aiops` image may still start `runtime.hermes_gateway`.
- Tests may be asserting legacy behavior rather than product behavior.
- Some Feishu helpers may have been written as overlays on top of `hermes_cli`;
  delete the overlay path if nothing current calls it.
- Removing a git submodule has index metadata; use normal `git rm`, not `rm -rf`.

## Rollback

Revert the dependency-removal commit. No data migration is involved.
