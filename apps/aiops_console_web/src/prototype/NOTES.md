# Incident Workbench UI Prototype

Question: Which information structure best helps an on-call SRE verify evidence and decide the next step?

- A: live command log
- B: hypothesis-centered evidence spine
- C: auditable evidence ledger

## Decision

C, the auditable evidence ledger, is the selected information structure. Keep A's Human Input composer as a separate Investigation Record surface.

Human Input is an immutable User assertion, not Evidence. Corrections and retractions append referencing Investigation Events; they never overwrite history, satisfy Evidence Gate, approve, or execute an action. When verification is needed, Diagnosis creates a separate Evidence Step.

Status: validated. The losing variants and switcher were removed; this prototype now records the interaction decision that T01 will implement against real contracts.
