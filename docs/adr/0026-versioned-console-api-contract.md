# Console uses a versioned product API

Status: accepted

The rewritten Console uses `/api/v1/*` as its single product contract, while `/auth/*` remains unversioned and preserves the existing same-origin session and CSRF semantics. Existing unversioned `/api/*` routes are frozen only until the old Console is retired and are then removed rather than maintained as a second API. V1 exposes domain DTOs instead of Agent Run, Diagnosis Session, or persistence objects; compatible fields may be added within V1, while removals, renames, and semantic changes require a new versioned path.
