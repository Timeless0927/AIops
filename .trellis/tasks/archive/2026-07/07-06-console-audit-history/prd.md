# Console audit history

## Goal

Add a read-only audit/history view for incident timeline, approval audit refs,
and recent Gateway audit events.

## Position

- Order: 4
- Priority: P2
- Parallel: can run with notification center
- Depends on: existing audit/timeline storage; may need one minimal Gateway read API
- Blocks: execution tracking observability

## Confirmed Facts

- Gateway records authorization and approval audit events.
- Diagnosis writeback normalizes timeline/evidence/trace into process payload.

## Requirements

1. Show incident timeline from diagnosis-process when an incident is selected.
2. Show approval audit refs in approval detail when available.
3. Add recent audit list only if Gateway already has or can expose a small read
   API.
4. Keep this read-only.
5. UI text must be Chinese.

## Acceptance Criteria

- [ ] Operators can answer "what happened and when" for an incident.
- [ ] Audit/history view works with missing audit data.
- [ ] Gateway read API, if added, is authorized and tested.
- [ ] Frontend build and targeted tests pass.

## Out of Scope

- Full SIEM search.
- Arbitrary query language.
- Export/report generation.
