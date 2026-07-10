# Console source uses the monorepo while its artifact remains independent

Status: accepted

AIOps V1 keeps Gateway, Diagnosis, Connector, MCP, Notification Engine, and Console source in one repository because the current team implements narrow product slices across those boundaries and separate source repositories add coordination without adding an ownership boundary. Console source lives under `apps/aiops_console_web` with its own package lock, build, CI job, OCI image, Deployment, and manual digest promotion; Gateway still exposes only API, authentication, and event-stream endpoints and does not bundle Console assets.

Gateway's versioned OpenAPI 3.1 specification remains the integration contract. Console generates TypeScript types from the versioned specification in the same repository, while compatibility checks and independent image promotion preserve safe runtime version skew.

This supersedes ADR-0041's independent-repository decision and the repository-specific wording in ADR-0043, ADR-0044, and ADR-0051. Their decisions about greenfield replacement, independent artifacts, immutable promotion, and an OpenAPI-owned contract remain accepted.
