# Console Next HTML reports and human feedback

## Parent

Plan AIOps Console Next rebuild.

## What to build

Deliver HTML-first incident reports and lightweight human feedback. Reports are
generated as drafts, reviewed by humans, published as immutable versions, and
linked to evidence and responsibility chains. Feedback is stored on runs and can
inform report generation and later evaluations.

The completed slice should be demoable as: incident reviewer opens a report
draft, sees evidence-backed sections and unknowns preserved, publishes, then
new edits create a new version. Feedback on diagnosis/evidence/action/report is
stored and visible for audit/postmortem review.

## User Stories Covered

- Parent stories 39, 51, 52, and 55.

## Requirements

- `/incidents/:incidentId/report` is a real route.
- Reports are HTML-first.
- Markdown export is secondary.
- Reports have versions.
- Agent-generated reports start as draft.
- Human publishes after review.
- Published reports are immutable; changes create a new version.
- Reports are printable and exportable as HTML.
- Reports are shared only by internal authenticated links.
- Reports include summary, timeline, impact, root cause, trigger, remediation
  process, agent actions, human approvals, evidence references, recovery
  validation, follow-up items, and responsibility chain.
- Reports must not invent missing evidence. Unknowns stay unknown.
- Feedback targets include diagnosis conclusion, evidence, action proposal, and
  report.
- Feedback is stored on the run and available for audit and postmortem review.
- First version does not perform automatic model training.

## Acceptance Criteria

- [ ] Report route renders draft and published report versions.
- [ ] Generated draft includes required sections and evidence refs.
- [ ] Unknown or missing evidence is represented explicitly.
- [ ] Publishing makes a report version immutable.
- [ ] Editing a published report creates a new version.
- [ ] Report is printable and exportable as HTML.
- [ ] Unauthenticated public report links are not available.
- [ ] Feedback can be recorded for diagnosis, evidence, action proposal, and
  report.
- [ ] Feedback is visible in run/audit/postmortem context.
- [ ] Tests cover draft, publish, immutability, versioning, authenticated access,
  export, unknown preservation, and feedback persistence.

## Blocked by

- Console Next incident workbench and run controls.
- Console Next responsibility-chain audit and tombstones.

## Further Notes

Do not add automatic training. Feedback is data capture for review, prompts,
rules, and future evaluation datasets.
