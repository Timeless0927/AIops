# Incident Workbench UI Prototype

Question: Which information structure best helps an on-call SRE verify evidence and decide the next step?

- A: live command log
- B: hypothesis-centered evidence spine
- C: auditable evidence ledger

## Decision

C, the auditable evidence ledger, is the selected information structure. Keep A's Human Input composer as a separate Investigation Record surface.

Human Input is an immutable User assertion, not Evidence. Corrections and retractions append referencing Investigation Events; they never overwrite history, satisfy Evidence Gate, approve, or execute an action. When verification is needed, Diagnosis creates a separate Evidence Step.

Status: validated. The losing variants and switcher were removed; this prototype now records the interaction decision that T01 will implement against real contracts.

## Setup And Platform Status Prototype

Question: How should optional setup and persistent platform readiness coexist without creating a false global `setup complete` state?

Compared structures:

- capability rail: persistent cross-capability navigation with focused recovery detail;
- recovery queue: incomplete and failed capabilities ordered as resumable work;
- status matrix: dense daily operations comparison.

Decision: use the capability rail as the primary Platform Status structure. It preserves the four independent capability states, works as both first configuration and later recovery entry, and scales down to a horizontal rail on narrow screens. Keep a compact matrix as a possible secondary view only if daily operational demand is proven; do not make the queue the primary navigation because it hides healthy capabilities and overstates setup as a finite sequence.

The Incident workspace remains reachable in every state. `skipped` is visibly `not ready`, not success. Platform Administrators can configure, verify, retry, or skip optional capabilities; ordinary Users receive safe summaries without configuration or test controls. Platform Status owns the cross-capability readiness overview and recovery entry points, while `/admin` retains detailed domain administration.

Status: validated as a throwaway interaction prototype. No prototype state or reason wording is a backend contract.
