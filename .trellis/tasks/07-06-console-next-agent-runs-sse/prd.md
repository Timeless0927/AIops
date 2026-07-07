# Console Next conversations, agent runs, and SSE timeline

## Parent

Plan AIOps Console Next rebuild.

## What to build

Deliver conversations, human-started and incident-triggered agent runs, `/btw`
side conversations, and persisted SSE timelines. Users can start or continue
runs, see live phases/tool calls/evidence/risk/approval/execution events, and
refresh or reconnect without losing timeline state.

The completed slice should be demoable as: operator starts a run from Console
or incident context, snapshot loads, SSE streams persisted timeline events, and
browser refresh resumes the same run using stored events.

## User Stories Covered

- Parent stories 4, 5, 6, 22, 23, 24, 25, 26, 41, and 42.

## Requirements

- Conversations are user-visible containers with title, tags, status, creator,
  scope, timestamps, and optional incident link.
- Agent runs belong to conversations and have timeline, tool calls, evidence,
  actions, approvals, and execution results.
- Runs can be incident-triggered or human-started.
- Chat supports investigation requests, action requests, and `/btw`.
- Mainline affects run state, evidence, actions, approvals, timeline, and audit.
- `/btw` side discussion does not mutate mainline until explicitly promoted.
- Timeline events are persisted, not memory-only.
- Frontend loads a snapshot before connecting to SSE.
- SSE supports `Last-Event-ID` reconnect.
- SSE emits only events the current user is authorized to view.
- Event payloads are redacted.
- Raw chain-of-thought, hidden prompts, secrets, and unsanitized large raw logs
  are not exposed.
- Conversation chat-layer content can be archived and deleted without deleting
  immutable responsibility records.
- Automatic tags are context-specific and editable.

## Acceptance Criteria

- [ ] Users can create, rename, tag, archive, and delete chat-layer
  conversations.
- [ ] Users can start human runs and view incident-linked runs.
- [ ] Snapshot endpoint returns current run state, timeline, evidence refs,
  actions, approvals, and permissions.
- [ ] SSE endpoint streams persisted authorized redacted events.
- [ ] Refresh and reconnect resume the same run.
- [ ] `Last-Event-ID` replay works.
- [ ] `/btw` messages are separated from mainline.
- [ ] Promoting `/btw` content creates an explicit mainline event.
- [ ] Unauthorized users cannot receive hidden run events.
- [ ] Tests cover snapshot-before-stream, replay, auth filtering, redaction,
  `/btw`, promotion, and conversation lifecycle.

## Blocked by

- Console Next routing, Gateway assets, session, and route guard shell.
- Console Next five-role RBAC and user management.
- Console Next OpenObserve evidence query and panels.

## Further Notes

This slice does not need full action execution. It only needs to carry action
proposal events and refs well enough for the approval/execution slice to attach.
