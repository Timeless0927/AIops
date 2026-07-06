# Backend task bookkeeping closeout

## Goal

Remove stale active Trellis entries for backend work that is already merged into
`console-api-base-zh`.

## Requirements

- Archive `controlled-mutation-execution` and `real-fault-replay-expansion`
  after verifying their commits are in HEAD.
- Remove the duplicate active notification migration task when an archived
  completed copy already exists.
- Leave `hermes-diagnosis-service-rename` active.

## Acceptance Criteria

- [x] Only unfinished backend tasks remain active.
- [x] Completed task artifacts are archived or already archived.
- [x] No application code changes are bundled into this cleanup.
