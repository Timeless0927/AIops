# Console Next reports/feedback design

## Scope

This slice delivers HTML-first incident reports and lightweight human feedback.

## Confirmed decisions

- HTML is the primary report format.
- Markdown export is deferred.
- Gateway stores versioned report HTML.
- Agent-generated reports start as drafts.
- Human publish is required.
- Published versions are immutable.
- Editing a published report creates a new version.
- Reports must preserve unknowns and must not invent missing evidence.
- Feedback is stored as structured evaluation data.
- Feedback does not trigger training and does not automatically change prompts.

## Deferred

- Markdown export.
- Public report sharing.
- Automatic model training.
- Report template marketplace or custom template editor.
