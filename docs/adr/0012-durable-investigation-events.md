# Investigation events are durable before streaming

Status: accepted

Gateway owns an append-only InvestigationEvent log and assigns monotonically increasing event IDs within an Investigation. Human messages, Agent output, tool activity, Evidence Step changes, and lifecycle transitions are written idempotently before they are acknowledged or streamed. Incident-scoped SSE replays persisted events from a cursor or `Last-Event-ID`; disconnecting a Console never affects execution or loses progress. The Workbench snapshot reports its latest event ID so snapshot loading and stream continuation have a stable handoff.
