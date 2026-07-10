# Incident resolution follows stabilized signal recovery

Status: accepted

Each Alert Signal records firing and recovery independently, and one recovered Signal cannot resolve an Incident while any other Signal is firing. When all current Signals are recovered, Gateway records a Recovery Observation and starts a configurable stabilization window; any refiring Signal cancels that observation. Gateway automatically resolves the Incident after the window unless an approved mutation, conditional rollback, or its post-check is still active, in which case resolution waits for that work to become terminal. Recovery is new evidence, so unexecuted Recommended Actions and pending Approvals become stale; an active Investigation may finish and persist its findings after Incident resolution. A later firing Signal follows the reopen-window rule in ADR-0024.
