# Domain Docs

This monorepo has one AIOps domain context shared by Gateway, Diagnosis, Connector, MCP, Notification Engine, and Console.

## Before exploring

- Read the root `CONTEXT.md` for canonical domain language.
- Read the relevant decisions under `docs/adr/`.
- Do not create application-specific copies of shared terms or ADRs.

If a required term or decision does not exist, proceed with the current task and use `/domain-modeling` only when a real ambiguity or architectural decision is resolved.

## Use the glossary

Use the exact terms defined in `CONTEXT.md` in tickets, tests, APIs, and UI-facing product concepts. Avoid synonyms explicitly rejected by the glossary.

## Flag conflicts

If proposed work contradicts an accepted ADR, surface the conflict and either follow the ADR or record a superseding decision before implementation.
