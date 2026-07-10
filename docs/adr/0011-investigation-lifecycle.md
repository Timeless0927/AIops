# Investigation lifecycle separates state from phase

Status: accepted

An Investigation persists one lifecycle state: `queued`, `running`, `paused`, `human_led`, `completed`, `failed`, or `terminated`. A failed Investigation may return to `queued` when retried; `completed` and `terminated` are immutable, and resuming Agent work after `human_led` requires ending that Investigation and explicitly creating another. Evidence collection, reasoning, and waiting for confirmation are phases rather than lifecycle states. `not_started` and delayed-response indicators are derived Console states and are not persisted.
