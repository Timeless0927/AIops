# Console does not duplicate server state

Status: accepted

TanStack Query is the Console's single cache for Gateway-owned data and mutations. Route parameters and search parameters own navigable state such as the selected Incident, filters, pagination, and view; component-local React state owns only transient interaction such as an open dialog or unfinished input. Redux, Zustand, and a client-side Investigation state machine are not used.

The Incident SSE client appends immutable events to the relevant Query cache and invalidates the bounded Workbench snapshot when aggregate state may have changed. Reconnection resumes from the Gateway cursor. Gateway snapshots and events remain authoritative, so the Console does not maintain a second event-reduction model that can diverge after a reconnect, refresh, or concurrent action.
