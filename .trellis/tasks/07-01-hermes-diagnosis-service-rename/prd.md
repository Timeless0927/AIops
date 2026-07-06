# Rename Hermes diagnosis service boundary

## Goal

Rename the internal `hermes` diagnosis service boundary to a clearer diagnosis
service name when the project is ready for a broad contract migration.

## Confirmed Facts

- ADR-0003 records the rename as Future Work.
- The old external `hermes-agent` submodule route is no longer the mainline, but
  the repository still uses `hermes/`, `AIOPS_HERMES_*`, service names, docs,
  tests, and response fields.
- ADR-0003 estimates this affects many files and should not be done as a small
  opportunistic cleanup.

## Requirements

1. Pick one service boundary name and apply it consistently across code, deploy,
   tests, docs, and runtime config.
2. Provide a compatibility plan for existing env vars and service response
   fields that external scripts may still read.
3. Avoid changing diagnosis behavior while renaming.
4. Run broad regression across Gateway/Hermes/Connector/MCP packaging and
   diagnosis flows.

## Acceptance Criteria

- New name is reflected consistently in paths/modules/docs/deploy manifests or
  documented aliases.
- Backward compatibility behavior is explicit and tested.
- Existing diagnosis, writeback, replay, and service packaging tests pass.
- ADR-0003 Future Work is updated with the final migration status.

## Out of Scope

- Changing LLM provider behavior.
- Reworking diagnosis quality.
- Mutation execution.
