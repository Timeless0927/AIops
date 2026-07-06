# Expand real fault replay fixtures

## Goal

Expand ADR-0003 replay coverage beyond the initial V1 graduation set by adding
real fixtures across more fault categories and making the replay report useful
for ongoing diagnosis-quality calibration.

## Confirmed Facts

- ADR-0003 V1 is already closed with 10 `synthetic:false` live fixtures and
  100% replay hit-rate.
- The first real fixture set is concentrated around `bad_release_deploy` /
  PodCrashLooping-style scenarios.
- `tests/export_incident.py` and `tests/replay_incident.py` already provide the
  export and scoring path.

## Requirements

1. Add real incidents from at least three distinct root-cause categories.
2. Each fixture must include incident metadata, evidence rows, trace rows, and
   human-backfilled truth in `incident_case_profiles`.
3. Replay output must make per-category outcomes easy to inspect.
4. New categories must be added to the replay taxonomy and validated.
5. Failing or borderline cases should be captured as calibration input, not
   hidden by changing truth labels.

## Acceptance Criteria

- `tests/replay_incident.py --validate-taxonomy` passes.
- Replay JSON reports real fixture count, synthetic fixture count, hit-rate, and
  enough category detail to identify weak classes.
- Added fixtures are marked `synthetic:false` only when truth was human-filled.
- ADR/docs are updated with the expanded sample distribution.

## Out of Scope

- Rewriting the LLM tool-use architecture.
- Changing the V1 graduation conclusion already recorded in ADR-0003.
