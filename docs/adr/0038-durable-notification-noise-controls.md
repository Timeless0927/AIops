# Notification noise controls are durable and critical-biased

Status: accepted

Each Notification Destination may define a timezone, quiet hours, hourly delivery limit, and digest interval. Lower-severity requests may be durably deferred into a digest, while `critical` bypasses quiet hours and rate limits by default; all deferred, summarized, suppressed, and limited requests remain queryable. Administrators may create a scoped, time-bounded Notification Silence that also covers `critical`, but only with fresh authentication, an explicit reason, and full audit. The same event ID creates at most one Notification Delivery per destination; its transport attempts follow ADR-0040. V1 omits acknowledgment-driven escalation and periodic reminders; a Notification Route fans out immediately when broader coverage is required.
