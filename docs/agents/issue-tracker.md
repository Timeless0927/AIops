# Issue tracker: Local Markdown

Issues and PRDs for this monorepo live as Markdown files under the root `.scratch/`. Cross-application work is recorded once here rather than copied into per-application trackers. External pull requests are not a triage surface.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The PRD is `.scratch/<feature-slug>/PRD.md`
- `/to-tickets` publishes the approved dependency graph as `.scratch/<feature-slug>/tickets.md`
- Standalone implementation issues may use `.scratch/<feature-slug>/issues/<NN>-<slug>.md`
- Triage state is recorded as a `Status:` line near the top of a PRD or issue file
- Comments and conversation history append under a `## Comments` heading

## Skill operations

- To publish a spec, create `.scratch/<feature-slug>/PRD.md`.
- To publish local tracer-bullet tickets, create one dependency-ordered `.scratch/<feature-slug>/tickets.md`.
- To fetch a ticket, read the referenced aggregate ticket or issue file.
- One ticket may change multiple applications in this monorepo when that is the smallest end-to-end slice.

## Wayfinding operations

- **Map**: `.scratch/<effort>/map.md`
- **Child ticket**: `.scratch/<effort>/issues/<NN>-<slug>.md`
- **Blocking**: a `Blocked by: NN, NN` line near the top
- **Frontier**: any open, unclaimed ticket whose blockers are resolved
- **Claim**: set `Status: claimed` before starting work
- **Resolve**: append the result under `## Answer`, set `Status: resolved`, and link the result from the map
