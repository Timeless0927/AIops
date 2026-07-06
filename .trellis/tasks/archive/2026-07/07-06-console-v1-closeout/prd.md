# Console V1 closeout and task archive

## Goal

Close Trellis bookkeeping for Console V1 after the merged frontend and Gateway
work is verified.

## Requirements

- Archive completed Console V1 child tasks only after their code is on
  `console-api-base-zh`.
- Leave unrelated dirty Trellis/skill files untouched.
- Keep this task documentation-only.

## Acceptance Criteria

- [x] Incident workbench, approval center, audit history, notification center,
  and execution tracking are represented as completed archived work.
- [x] Console V1 parent task is archived after children are complete.
- [x] No functional code changes are bundled into archive-only commits.
