# V1 mutations are three typed Deployment actions

Status: accepted

V1 permits only `restart_deployment`, `scale_deployment`, and `rollback_deployment`. Scaling freezes current and target replica counts and enforces configured bounds; rollback names an existing revision captured at approval time rather than a relative “previous” revision. Gateway constructs commands from typed fields, and Connector accepts only the corresponding typed Command Envelopes. Agent-supplied shell or `argv`, Pod deletion, `apply`, `patch`, `delete`, `exec`, `attach`, and mutations of StatefulSets, DaemonSets, Jobs, or other resource kinds are forbidden until separately designed.
