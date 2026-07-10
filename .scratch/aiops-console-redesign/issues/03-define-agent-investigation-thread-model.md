Type: grilling
Status: resolved

## Question

How should the event-scoped Agent investigation thread behave in the first-version UI and API contract?

## Context

The decision so far:

- Do not expose standalone Agent Runs as a primary product concept.
- An incident has a main investigation thread.
- Sending a message must immediately show the user's message, then show Agent response/tool/evidence updates.
- "Start investigation" must not remain repeatedly clickable while the current investigation is running.
- `/btw` side threads are out of first-version scope.

Resolve the visible states, allowed controls, and backend events needed for a credible investigation thread.

## Answer

The first-version UI exposes an event-scoped investigation thread, not standalone Agent Runs. Backend code may still use `run_id`, `agent_runs`, and `run_events`, but the frontend route and product language should be incident-first.

### Lifecycle

- One incident has at most one active/running main investigation thread.
- When an alert enters the system, Gateway may auto-create the main investigation thread if policy allows it. Otherwise the workbench shows "未开始调查".
- Clicking "开始调查" immediately switches the control to "调查中"; repeated clicks are disabled.
- While running, allowed controls are:
  - continue asking a question,
  - pause,
  - terminate,
  - human takeover.
- Human takeover means the human is now leading the event. It stops Agent automatic progression, records the responsible person in the timeline/responsibility summary, and preserves all existing evidence, conclusions, and messages. It is not approval, execution, or incident resolution.
- After a thread finishes or terminates, the user can start "重新调查". This creates a new investigation round while old records remain visible in the incident timeline.
- UI copy should say "调查线程" or "本次调查", not "Agent Run".

### Message Feedback

When a user sends a message:

1. Insert the user message into the message stream immediately with a sent/pending state.
2. Add visible investigation state: "Agent 正在处理你的问题".
3. Update progressively from server events:
   - `agent_message_delta`: Agent response is streaming.
   - `tool_call_started`: show what is being checked.
   - `tool_call_finished`: show what was found.
   - `evidence_step_added` / `evidence_step_updated`: update the investigation steps.
   - `agent_message_done` / `investigation_finished`: clear the waiting state.
4. If no Agent event arrives within 10 seconds, show "仍在等待 Agent 响应".
5. If sending or Agent processing fails, show the failure reason beside the message with a retry action.

The UI must never leave the user with only an input box after sending a message.

### Event-Level API Boundary

The first-version frontend should call incident-level investigation APIs, not `/api/agent-runs/*` directly:

- `GET /api/incidents/:incidentId/workbench`
- `POST /api/incidents/:incidentId/investigation/start`
- `POST /api/incidents/:incidentId/investigation/messages`
- `POST /api/incidents/:incidentId/investigation/pause`
- `POST /api/incidents/:incidentId/investigation/terminate`
- `POST /api/incidents/:incidentId/investigation/takeover`
- `GET /api/incidents/:incidentId/investigation/events`

`POST /api/incidents/:incidentId/investigation/start` returns the current running thread if one already exists; it must not create duplicate running investigations.

`GET /api/incidents/:incidentId/investigation/events` is SSE scoped by incident, so the frontend does not need to know the internal `run_id`.

### Event Types

Minimum event set:

- `investigation_started`
- `investigation_phase_changed`
- `user_message_added`
- `agent_message_delta`
- `agent_message_done`
- `tool_call_started`
- `tool_call_finished`
- `evidence_step_added`
- `evidence_step_updated`
- `action_recommended`
- `human_confirmation_required`
- `investigation_paused`
- `investigation_terminated`
- `human_takeover_started`
- `investigation_failed`
- `investigation_finished`

Out of first-version scope:

- `/btw`
- `promote`
- multiple parallel discussion threads
- token/cost as primary UI elements

### UI State Machine

- `not_started`: show "开始调查", enabled.
- `running`: show "调查中", disable start; show "暂停", "终止", "人工接管".
- `paused`: show "继续调查" and "终止".
- `takeover`: show "人工接管中"; Agent automatic progression is stopped; input becomes human notes/context and does not trigger Agent.
- `terminated`: show "重新调查"; message input disabled.
- `finished`: show "重新调查"; no implicit new run when typing.
- `failed`: show "重试调查" and "查看失败原因".

Message input:

- `running`: enabled; sends to Agent.
- `paused`: enabled, but sending tells the user to continue investigation first.
- `takeover`: enabled as human notes only.
- `terminated`: disabled.
- `finished`: disabled until the user explicitly starts "重新调查".
