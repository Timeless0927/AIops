# Notification Requests use one channel-neutral event contract

Status: accepted

Every Notification Request carries a version, globally unique event ID, typed event name, occurrence time, normalized `info`, `warning`, `error`, or `critical` severity, versioned subject, Environment and resource scope, concise summary, event-specific validated facts, and an internal relative Console path. The request never chooses a channel, destination, recipient, template, or credential and never embeds arbitrary JSON, raw logs, secrets, or internal run/session identifiers. V1 defines Incident opened/severity-changed/reopened/resolved, Investigation needs-input/partial/failed, Approval required/approved/rejected/expired/blocked, execution succeeded/failed/rollback-required/outcome-unknown, and Connector offline/recovered events; exact duplicate event IDs are idempotent.
