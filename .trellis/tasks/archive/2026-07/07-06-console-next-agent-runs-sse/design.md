# Console Next conversations/agent-runs/SSE design

## Scope

This slice delivers conversations, human-started runs, incident-triggered runs,
side threads, and persisted SSE timeline replay.

## Architecture

- Gateway owns the browser API and persistence for Console-visible
  conversations, runs, and timeline events.
- Use a small Gateway service module with SQLite WAL tables for:
  - conversations
  - agent runs
  - run timeline events
- Keep execution simple in this slice: creating a run appends deterministic
  lifecycle/timeline records and can reference existing incident/evidence data.
  It does not invoke or rebuild the full diagnosis/agent runtime.
- Browser calls only Gateway:
  - `GET /api/agent-runs`
  - `POST /api/agent-runs`
  - `GET /api/agent-runs/{run_id}`
  - `GET /api/agent-runs/{run_id}/events`
  - `GET /api/agent-runs/{run_id}/stream`
  - conversation create/update/archive/delete routes as needed by the slice.

## Authorization and data flow

- All reads and writes require the existing user session.
- Resource authorization uses the run/conversation scope:
  `cluster`, `namespace`, `service`, and `team`.
- Missing scope fails closed for non-admin users.
- Snapshot loads before SSE. The stream replays persisted events after
  `Last-Event-ID` and then ends or idles safely; no memory-only event bus is
  required for the first implementation.
- Event payloads are redacted at write/read boundaries. Raw chain-of-thought,
  hidden prompts, bearer tokens, secrets, and large raw logs are not persisted
  or streamed.
- `/btw` messages are stored as side-thread timeline events. Promotion creates a
  separate explicit mainline event.

## Confirmed decisions

- First implementation reuses existing diagnosis/writeback capability where it
  fits.
- Do not rebuild the full agent runtime in this slice.
- SSE timeline event model covers:
  - run lifecycle
  - agent messages
  - tool calls
  - evidence events
  - risk classification
  - approval events
  - execution events
- Frontend loads a snapshot before opening SSE.
- Events are persisted, authorized per user, and redacted.
- `/btw` starts as a side-thread data model and UI behavior.
- `/btw` does not need complex parallel agent branch execution in the first pass.
- Promotion from `/btw` to mainline is explicit and creates a mainline event.
- The first SSE implementation may be replay-first rather than a long-lived
  runtime event broker. Persisted events are the contract.

## Deferred

- Full multi-agent branch execution.
- Prompt/runtime redesign.
- Raw chain-of-thought display.
- Real background agent orchestration beyond deterministic run/timeline records.
- Cross-process live fanout; add it only when multiple Gateway replicas or real
  long-running agents require it.
